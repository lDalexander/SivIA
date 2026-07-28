"""
Tests de los endpoints HTTP, con Ollama simulado y SQLite en memoria.

Verifican el contrato completo de la API: autenticación, códigos de estado y
—lo más importante— que el estado de la base de datos sea el correcto después
de cada operación, incluidos los casos en que NO debe escribirse nada.
"""
import pytest
from fastapi import HTTPException

from models import MovimientoInventario

EXTRACCION_VALIDA = {"producto": "AREPAS", "cantidad": 45, "tipo_movimiento": "INGRESO"}
COMANDO = {"texto": "Llegaron 45 arepas para el inventario"}


def contar(sesion):
    return sesion.query(MovimientoInventario).count()


# --- SISTEMA ---

def test_raiz_responde_con_la_identificacion_del_servicio(cliente):
    respuesta = cliente.get("/")
    assert respuesta.status_code == 200
    assert respuesta.json()["sistema"] == "SivIA Core"


# --- AUTENTICACIÓN ---

def test_procesar_comando_sin_api_key_devuelve_401(cliente, sesion_db):
    respuesta = cliente.post("/api/procesar-comando", json=COMANDO)
    assert respuesta.status_code == 401
    assert contar(sesion_db) == 0


def test_procesar_comando_con_api_key_incorrecta_devuelve_401(cliente, sesion_db):
    respuesta = cliente.post(
        "/api/procesar-comando", json=COMANDO, headers={"X-API-Key": "clave-erronea"}
    )
    assert respuesta.status_code == 401
    assert contar(sesion_db) == 0


def test_eliminar_movimiento_sin_api_key_devuelve_401(cliente, crear_movimiento, sesion_db):
    movimiento = crear_movimiento("AREPAS", 10, "INGRESO")
    respuesta = cliente.delete(f"/api/movimientos/{movimiento.id}")
    assert respuesta.status_code == 401
    assert contar(sesion_db) == 1  # No se borró nada.


def test_los_endpoints_de_lectura_no_requieren_api_key(cliente):
    assert cliente.get("/api/movimientos").status_code == 200
    assert cliente.get("/api/stock").status_code == 200


# --- PROCESAMIENTO DE COMANDOS ---

def test_comando_valido_registra_el_movimiento(cliente, auth, simular_llm, sesion_db):
    simular_llm(EXTRACCION_VALIDA)

    respuesta = cliente.post("/api/procesar-comando", json=COMANDO, headers=auth)

    assert respuesta.status_code == 200
    datos = respuesta.json()["datos_extraidos"]
    assert datos["producto"] == "AREPAS"
    assert datos["cantidad"] == 45
    assert datos["tipo_movimiento"] == "INGRESO"
    assert datos["fecha_utc"].endswith("+00:00")  # Marcado como UTC.

    # La verificación que de verdad importa: la fila está en la base de datos.
    assert contar(sesion_db) == 1
    guardado = sesion_db.query(MovimientoInventario).one()
    assert guardado.producto == "AREPAS"
    assert guardado.cantidad == 45


def test_egreso_se_registra_como_egreso(cliente, auth, simular_llm, sesion_db):
    simular_llm({"producto": "AREPAS", "cantidad": 12, "tipo_movimiento": "EGRESO"})

    cliente.post("/api/procesar-comando", json={"texto": "Vendimos 12 arepas"}, headers=auth)

    assert sesion_db.query(MovimientoInventario).one().tipo_movimiento == "EGRESO"


@pytest.mark.parametrize(
    "extraccion_defectuosa",
    [
        {"producto": "AREPAS", "cantidad": 0, "tipo_movimiento": "INGRESO"},
        {"producto": "AREPAS", "cantidad": -5, "tipo_movimiento": "INGRESO"},
        {"producto": "DESCONOCIDO", "cantidad": 5, "tipo_movimiento": "INGRESO"},
        {"producto": "", "cantidad": 5, "tipo_movimiento": "INGRESO"},
        {"producto": "AREPAS", "cantidad": 5, "tipo_movimiento": "TAL VEZ"},
        {"producto": "AREPAS", "cantidad": "muchas", "tipo_movimiento": "INGRESO"},
        {},
    ],
)
def test_extraccion_defectuosa_devuelve_422_sin_escribir_nada(
    cliente, auth, simular_llm, sesion_db, extraccion_defectuosa
):
    """La regresión más valiosa de la suite.

    Antes de la auditoría, estos casos se guardaban con valores por defecto
    (cantidad=0, tipo_movimiento='INGRESO'). Un EGRESO mal interpretado se
    convertía en un INGRESO fantasma que descuadraba el inventario sin dejar
    rastro. Ahora: 422 y cero escrituras.
    """
    simular_llm(extraccion_defectuosa)

    respuesta = cliente.post("/api/procesar-comando", json=COMANDO, headers=auth)

    assert respuesta.status_code == 422
    assert contar(sesion_db) == 0


@pytest.mark.parametrize("texto", ["", "ab", "   "])
def test_texto_demasiado_corto_lo_rechaza_pydantic(cliente, auth, sesion_db, texto):
    """Se rechaza antes de gastar 6 segundos de inferencia del LLM."""
    respuesta = cliente.post("/api/procesar-comando", json={"texto": texto}, headers=auth)
    assert respuesta.status_code == 422
    assert contar(sesion_db) == 0


@pytest.mark.parametrize("codigo", [502, 503, 504])
def test_los_fallos_de_ollama_se_propagan_con_su_codigo(
    cliente, auth, simular_llm, sesion_db, codigo
):
    """Regresión del bug del manejador de excepciones: el 'except Exception'
    genérico capturaba los HTTPException propios y los convertía en 500,
    dejando el manejo de los errores de Ollama como código muerto."""
    simular_llm(error=HTTPException(status_code=codigo, detail="fallo simulado"))

    respuesta = cliente.post("/api/procesar-comando", json=COMANDO, headers=auth)

    assert respuesta.status_code == codigo
    assert contar(sesion_db) == 0


def test_un_error_inesperado_no_filtra_detalles_internos(
    cliente, auth, simular_llm, sesion_db
):
    """Los detalles de las excepciones van al log, nunca al cliente: filtrarían
    rutas, drivers y potencialmente el prompt completo."""
    simular_llm(error=RuntimeError("mysql://usuario:contraseña@host/base"))

    respuesta = cliente.post("/api/procesar-comando", json=COMANDO, headers=auth)

    assert respuesta.status_code == 500
    assert "contraseña" not in respuesta.text
    assert "mysql" not in respuesta.text.lower()


# --- CONSULTA DE MOVIMIENTOS ---

def test_movimientos_vacio_devuelve_lista_vacia(cliente):
    assert cliente.get("/api/movimientos").json() == []


def test_movimientos_respeta_el_limite(cliente, crear_movimiento):
    for i in range(5):
        crear_movimiento(f"PRODUCTO{i}", i + 1, "INGRESO")

    assert len(cliente.get("/api/movimientos?limit=2").json()) == 2


def test_movimientos_filtra_por_producto(cliente, crear_movimiento):
    crear_movimiento("AREPAS", 10, "INGRESO")
    crear_movimiento("QUESO", 5, "INGRESO")

    resultado = cliente.get("/api/movimientos?producto=arepas").json()

    assert len(resultado) == 1
    assert resultado[0]["producto"] == "AREPAS"


@pytest.mark.parametrize("consulta", ["limit=0", "limit=9999", "offset=-1"])
def test_movimientos_rechaza_parametros_fuera_de_rango(cliente, consulta):
    assert cliente.get(f"/api/movimientos?{consulta}").status_code == 422


# --- STOCK ---

def test_stock_calcula_ingresos_menos_egresos(cliente, crear_movimiento):
    crear_movimiento("AREPAS", 45, "INGRESO")
    crear_movimiento("AREPAS", 12, "EGRESO")

    fila = cliente.get("/api/stock").json()[0]

    assert fila["producto"] == "AREPAS"
    assert fila["stock_actual"] == 33
    assert fila["total_ingresos"] == 45
    assert fila["total_egresos"] == 12
    assert fila["movimientos"] == 2


def test_stock_agrupa_por_producto(cliente, crear_movimiento):
    crear_movimiento("AREPAS", 45, "INGRESO")
    crear_movimiento("QUESO", 10, "INGRESO")
    crear_movimiento("QUESO", 3, "EGRESO")

    resultado = {f["producto"]: f["stock_actual"] for f in cliente.get("/api/stock").json()}

    assert resultado == {"AREPAS": 45, "QUESO": 7}


def test_stock_puede_ser_negativo(cliente, crear_movimiento):
    """Un stock negativo indica movimientos mal registrados o un inventario
    inicial no capturado. Debe mostrarse, no ocultarse: es la señal de que hay
    algo que corregir."""
    crear_movimiento("AREPAS", 5, "EGRESO")

    assert cliente.get("/api/stock").json()[0]["stock_actual"] == -5


def test_stock_vacio_devuelve_lista_vacia(cliente):
    assert cliente.get("/api/stock").json() == []


# --- ELIMINACIÓN ---

def test_eliminar_movimiento_existente(cliente, auth, crear_movimiento, sesion_db):
    movimiento = crear_movimiento("AREPAS", 10, "INGRESO")

    respuesta = cliente.delete(f"/api/movimientos/{movimiento.id}", headers=auth)

    assert respuesta.status_code == 200
    assert respuesta.json()["eliminado"]["producto"] == "AREPAS"
    assert contar(sesion_db) == 0


def test_eliminar_movimiento_inexistente_devuelve_404(cliente, auth):
    assert cliente.delete("/api/movimientos/9999", headers=auth).status_code == 404


def test_eliminar_con_id_invalido_devuelve_422(cliente, auth):
    assert cliente.delete("/api/movimientos/0", headers=auth).status_code == 422


def test_eliminar_corrige_el_stock(cliente, auth, crear_movimiento):
    """El motivo por el que existe este endpoint: una transcripción errónea
    debe poder corregirse desde la API, sin entrar a MySQL a mano."""
    crear_movimiento("AREPAS", 45, "INGRESO")
    erroneo = crear_movimiento("AREPAS", 500, "INGRESO")

    cliente.delete(f"/api/movimientos/{erroneo.id}", headers=auth)

    assert cliente.get("/api/stock").json()[0]["stock_actual"] == 45
