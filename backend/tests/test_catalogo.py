"""
Tests del catálogo de productos.

Cubren el defecto que motivó el catálogo y las tres piezas que lo resuelven:

  1. **Normalización** (`normalizar_nombre`): arregla lo que el modelo estropea
     por su cuenta —guiones bajos, espacios dobles, minúsculas— sin corregir
     ortografía, porque inventar la palabra correcta es lo que no queremos.
  2. **Resolución** (`resolver_producto`): traduce lo extraído a un producto
     canónico usando alias y claves insensibles a acentos, y ante un desconocido
     rechaza con sugerencias en vez de darlo de alta.
  3. **Unificación del stock**: es el objetivo final. Que «AREPA» y «AREPAS»
     sumen en el mismo saldo y no en dos.

El caso de referencia es el observado el 2026-07-27: «Llegaron 20 panes de yuca»
se guardó como `PANES_DE_YACA`, creando un producto fantasma con su propio saldo.
"""
import pytest
from fastapi import HTTPException

import main
from models import AliasProducto, MovimientoInventario, Producto
from models import clave_busqueda, normalizar_nombre


def contar(sesion):
    return sesion.query(MovimientoInventario).count()


# --- NORMALIZACIÓN ---

@pytest.mark.parametrize(
    "bruto, esperado",
    [
        # El defecto real: el modelo insertó guiones bajos que nadie le pidió.
        ("PANES_DE_YUCA", "PANES DE YUCA"),
        ("panes-de-yuca", "PANES DE YUCA"),
        ("PANES__DE___YUCA", "PANES DE YUCA"),
        ("  panes  de   yuca  ", "PANES DE YUCA"),
        ("arepas", "AREPAS"),
        ("Arepas De Maíz", "AREPAS DE MAÍZ"),
    ],
)
def test_normaliza_separadores_y_mayusculas(bruto, esperado):
    assert normalizar_nombre(bruto) == esperado


def test_normalizacion_no_corrige_la_ortografia():
    """La mitad del defecto original era ortográfica: 'yuca' escrita 'YACA'.

    Normalizar no debe arreglarlo. Si esta función 'corrigiera' palabras, estaría
    inventando datos igual que el modelo. La palabra mal escrita llega intacta a
    la resolución, que sugiere el canónico y deja decidir a una persona.
    """
    assert normalizar_nombre("panes_de_yaca") == "PANES DE YACA"


@pytest.mark.parametrize("bruto", ["", "   ", None, "___", "- -"])
def test_normalizacion_de_entradas_vacias(bruto):
    assert normalizar_nombre(bruto) == ""


def test_normalizacion_respeta_el_limite_de_la_columna():
    assert len(normalizar_nombre("A" * 250)) == 100


def test_clave_de_busqueda_ignora_acentos_y_puntuacion():
    """El LLM transcribe 'MAIZ' tanto como 'MAÍZ'; ambas deben resolver igual
    sin obligar a registrar un alias por cada tilde."""
    assert clave_busqueda("AREPAS DE MAÍZ") == clave_busqueda("arepas de maiz")
    assert clave_busqueda("PAN, DE YUCA.") == clave_busqueda("PAN DE YUCA")


def test_clave_de_busqueda_no_altera_lo_que_se_almacena():
    """La clave es solo para comparar: el catálogo guarda el nombre con acentos
    tal como lo escribió quien lo dio de alta."""
    assert normalizar_nombre("Arepas de Maíz") == "AREPAS DE MAÍZ"


# --- RESOLUCIÓN CONTRA EL CATÁLOGO ---

def catalogo_ficticio():
    """Catálogo en memoria, sin base de datos: resolver_producto es una función
    pura sobre la lista de productos ya cargada."""
    return [
        Producto(id=1, nombre="AREPAS", activo=True, alias=[
            AliasProducto(alias="AREPA"), AliasProducto(alias="AREPITAS"),
        ]),
        Producto(id=2, nombre="PANES DE YUCA", activo=True, alias=[]),
        Producto(id=3, nombre="AREPAS DE MAÍZ", activo=True, alias=[]),
    ]


def test_resuelve_por_nombre_canonico():
    assert main.resolver_producto(catalogo_ficticio(), "AREPAS").id == 1


def test_resuelve_por_alias_al_producto_canonico():
    """El corazón del catálogo: varias formas, un solo producto."""
    catalogo = catalogo_ficticio()
    for variante in ("AREPA", "AREPITAS"):
        assert main.resolver_producto(catalogo, variante).nombre == "AREPAS"


def test_resuelve_ignorando_los_acentos():
    assert main.resolver_producto(catalogo_ficticio(), "AREPAS DE MAIZ").id == 3


def test_resuelve_tras_normalizar_los_guiones_bajos():
    """La mitad mecánica del defecto original queda arreglada sin intervención."""
    producto = main.resolver_producto(
        catalogo_ficticio(), normalizar_nombre("PANES_DE_YUCA")
    )
    assert producto.nombre == "PANES DE YUCA"


def test_rechaza_desconocido_sugiriendo_el_parecido():
    """La mitad ortográfica del defecto: no se adivina, se sugiere.

    Registrarlo automáticamente crearía 'PANES DE YACA' como producto legítimo
    con su propio saldo, que es exactamente el problema que el catálogo resuelve.
    """
    with pytest.raises(HTTPException) as exc:
        main.resolver_producto(catalogo_ficticio(), "PANES DE YACA")

    assert exc.value.status_code == 422
    assert "PANES DE YUCA" in exc.value.detail


def test_rechaza_desconocido_sin_parecido_alguno():
    with pytest.raises(HTTPException) as exc:
        main.resolver_producto(catalogo_ficticio(), "TORNILLOS")

    assert exc.value.status_code == 422
    assert "no se parece a ninguno" in exc.value.detail


def test_las_sugerencias_no_repiten_el_mismo_producto():
    """Varias claves (el canónico y sus alias) apuntan al mismo producto:
    sugerir 'AREPAS' tres veces no ayuda."""
    with pytest.raises(HTTPException) as exc:
        main.resolver_producto(catalogo_ficticio(), "AREPES")

    assert exc.value.detail.count("AREPAS") == 1


def test_un_alias_no_secuestra_el_nombre_canonico_de_otro_producto():
    """Si un alias coincide con el canónico de otro producto, gana el canónico:
    un nombre real no puede quedar capturado por la variante de un tercero."""
    catalogo = [
        Producto(id=1, nombre="QUESO", activo=True, alias=[]),
        Producto(id=2, nombre="QUESO FRESCO", activo=True,
                 alias=[AliasProducto(alias="QUESO")]),
    ]
    assert main.resolver_producto(catalogo, "QUESO").id == 1


# --- EL PROMPT DE SISTEMA ---

def test_el_prompt_incluye_el_catalogo():
    prompt = main.construir_prompt_sistema(["AREPAS", "PANES DE YUCA"])
    assert "'AREPAS'" in prompt and "'PANES DE YUCA'" in prompt


def test_el_prompt_prohibe_los_guiones_bajos_y_corregir_la_ortografia():
    """El prompt reforzado es la primera línea de defensa; la normalización es la
    garantía. Ambas deben estar."""
    prompt = main.construir_prompt_sistema([])
    assert "guiones bajos" in prompt
    assert "no corrijas la ortografía" in prompt.lower()


def test_un_catalogo_enorme_no_se_inyecta_en_el_prompt():
    """Por encima del límite la lista alargaría la inferencia sin mejorarla. La
    resolución sigue aplicándose igual, así que no se pierde integridad."""
    enorme = [f"PRODUCTO {i}" for i in range(main.MAX_PRODUCTOS_EN_PROMPT + 1)]
    assert main.construir_prompt_sistema(enorme) == main.PROMPT_SISTEMA_BASE


# --- EL ENDPOINT DE COMANDOS CONTRA EL CATÁLOGO ---

def test_comando_con_producto_desconocido_no_escribe_nada(
    cliente, auth, simular_llm, sesion_db
):
    """El test más importante del archivo: verifica el estado de la base, no solo
    el código de respuesta."""
    simular_llm({"producto": "PANES DE YACA", "cantidad": 20,
                 "tipo_movimiento": "INGRESO"})

    respuesta = cliente.post(
        "/api/procesar-comando", json={"texto": "Llegaron 20 panes de yaca"}, headers=auth
    )

    assert respuesta.status_code == 422
    assert "PANES DE YUCA" in respuesta.json()["detail"]
    assert contar(sesion_db) == 0


def test_comando_con_guiones_bajos_se_registra_con_el_nombre_canonico(
    cliente, auth, simular_llm
):
    """El defecto completo del 2026-07-27, extremo a extremo."""
    simular_llm({"producto": "PANES_DE_YUCA", "cantidad": 20,
                 "tipo_movimiento": "INGRESO"})

    respuesta = cliente.post(
        "/api/procesar-comando", json={"texto": "Llegaron 20 panes de yuca"}, headers=auth
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["datos_extraidos"]["producto"] == "PANES DE YUCA"


def test_un_alias_registra_el_movimiento_bajo_el_canonico(cliente, auth, simular_llm):
    simular_llm({"producto": "AREPA", "cantidad": 5, "tipo_movimiento": "INGRESO"})

    respuesta = cliente.post(
        "/api/procesar-comando", json={"texto": "Llegó una arepa"}, headers=auth
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["datos_extraidos"]["producto"] == "AREPAS"


def test_las_variantes_suman_en_un_unico_saldo(cliente, auth, simular_llm):
    """La razón de ser del catálogo: antes, cada variante era un producto con su
    propio saldo y el stock se fragmentaba en silencio."""
    for extraido, cantidad in [("AREPAS", 10), ("AREPA", 5), ("AREPITAS", 3)]:
        simular_llm({"producto": extraido, "cantidad": cantidad,
                     "tipo_movimiento": "INGRESO"})
        assert cliente.post(
            "/api/procesar-comando", json={"texto": "llegaron unas arepas"}, headers=auth
        ).status_code == 200

    stock = cliente.get("/api/stock").json()
    arepas = [f for f in stock if f["producto"] == "AREPAS"]
    assert len(arepas) == 1, "las variantes crearon saldos separados"
    assert arepas[0]["stock_actual"] == 18


def test_el_movimiento_queda_vinculado_al_producto_del_catalogo(
    cliente, auth, simular_llm, sesion_db
):
    """producto_id es lo que impide que un movimiento apunte a un producto
    inexistente; la columna de texto es solo el snapshot histórico."""
    simular_llm({"producto": "AREPA", "cantidad": 5, "tipo_movimiento": "INGRESO"})
    cliente.post("/api/procesar-comando", json={"texto": "llegó una arepa"}, headers=auth)

    movimiento = sesion_db.query(MovimientoInventario).one()
    canonico = sesion_db.query(Producto).filter(Producto.nombre == "AREPAS").one()
    assert movimiento.producto_id == canonico.id
    assert movimiento.producto == "AREPAS"


def test_catalogo_vacio_rechaza_el_comando_de_forma_explicativa(
    cliente_sin_catalogo, auth, simular_llm
):
    """Instalación recién migrada: debe decir qué falta, no fallar de forma opaca."""
    simular_llm({"producto": "AREPAS", "cantidad": 45, "tipo_movimiento": "INGRESO"})

    respuesta = cliente_sin_catalogo.post(
        "/api/procesar-comando", json={"texto": "Llegaron 45 arepas"}, headers=auth
    )

    assert respuesta.status_code == 409
    assert "POST /api/productos" in respuesta.json()["detail"]


def test_un_producto_retirado_deja_de_aceptar_comandos(
    cliente, auth, simular_llm, sesion_db, id_de
):
    cliente.delete(f"/api/productos/{id_de('QUESO')}", headers=auth)
    simular_llm({"producto": "QUESO", "cantidad": 2, "tipo_movimiento": "INGRESO"})

    respuesta = cliente.post(
        "/api/procesar-comando", json={"texto": "Llegaron 2 quesos"}, headers=auth
    )

    assert respuesta.status_code == 422
    assert contar(sesion_db) == 0


# --- ENDPOINTS DEL CATÁLOGO ---

def test_listar_productos_devuelve_el_catalogo_con_sus_alias(cliente):
    catalogo = cliente.get("/api/productos").json()
    arepas = next(p for p in catalogo if p["nombre"] == "AREPAS")
    assert arepas["alias"] == ["AREPA", "AREPITAS"]
    assert arepas["activo"] is True


def test_listar_productos_es_lectura_publica(cliente):
    assert cliente.get("/api/productos").status_code == 200


@pytest.mark.parametrize(
    "metodo, ruta, cuerpo",
    [
        ("post", "/api/productos", {"nombre": "MANTEQUILLA"}),
        ("post", "/api/productos/1/alias", {"alias": "AREPITA"}),
        ("delete", "/api/productos/1", None),
    ],
)
def test_los_endpoints_de_escritura_exigen_la_clave(cliente, metodo, ruta, cuerpo):
    respuesta = getattr(cliente, metodo)(ruta, json=cuerpo) if cuerpo else \
        getattr(cliente, metodo)(ruta)
    assert respuesta.status_code == 401


def test_crear_producto_normaliza_el_nombre_y_los_alias(cliente, auth):
    respuesta = cliente.post(
        "/api/productos",
        json={"nombre": "  mantequilla_de_maní ", "alias": ["MANTEQUILLA-DE-MANI"]},
        headers=auth,
    )

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["nombre"] == "MANTEQUILLA DE MANÍ"
    # 'MANTEQUILLA DE MANI' comparte clave de búsqueda con el canónico (los
    # acentos se ignoran al comparar), así que es redundante y se descarta.
    assert cuerpo["alias"] == []


def test_crear_producto_con_alias_los_registra(cliente, auth):
    respuesta = cliente.post(
        "/api/productos",
        json={"nombre": "GASEOSA", "alias": ["REFRESCO", "SODA"]},
        headers=auth,
    )
    assert respuesta.status_code == 201
    assert respuesta.json()["alias"] == ["REFRESCO", "SODA"]


def test_el_producto_creado_ya_resuelve_en_un_comando(cliente, auth, simular_llm):
    """El alta debe surtir efecto de inmediato: el catálogo se lee en cada
    petición, no se cachea en el arranque."""
    cliente.post("/api/productos", json={"nombre": "GASEOSA", "alias": ["REFRESCO"]},
                 headers=auth)
    simular_llm({"producto": "REFRESCO", "cantidad": 6, "tipo_movimiento": "INGRESO"})

    respuesta = cliente.post(
        "/api/procesar-comando", json={"texto": "llegaron 6 refrescos"}, headers=auth
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["datos_extraidos"]["producto"] == "GASEOSA"


def test_crear_producto_duplicado_devuelve_409(cliente, auth):
    respuesta = cliente.post("/api/productos", json={"nombre": "arepas"}, headers=auth)
    assert respuesta.status_code == 409
    assert "AREPAS" in respuesta.json()["detail"]


def test_no_se_puede_crear_un_producto_que_ya_es_alias_de_otro(cliente, auth):
    """Permitirlo dejaría 'AREPA' apuntando a dos productos y la resolución
    tendría que elegir en silencio."""
    respuesta = cliente.post("/api/productos", json={"nombre": "AREPA"}, headers=auth)
    assert respuesta.status_code == 409
    assert "AREPAS" in respuesta.json()["detail"]


def test_crear_producto_con_nombre_que_se_normaliza_a_vacio_devuelve_422(cliente, auth):
    respuesta = cliente.post("/api/productos", json={"nombre": "___"}, headers=auth)
    assert respuesta.status_code == 422


def test_agregar_alias_a_un_producto_existente(cliente, auth, id_de):
    respuesta = cliente.post(
        f"/api/productos/{id_de('QUESO')}/alias", json={"alias": "quesito"}, headers=auth
    )
    assert respuesta.status_code == 200
    assert "QUESITO" in respuesta.json()["alias"]


def test_el_alias_recien_enseñado_resuelve_el_comando_que_antes_fallaba(
    cliente, auth, simular_llm, id_de
):
    """El ciclo completo que hace usable un catálogo cerrado: rechazo, se enseña
    la variante una vez, y a partir de ahí funciona."""
    simular_llm({"producto": "QUESITO", "cantidad": 2, "tipo_movimiento": "INGRESO"})
    comando = {"texto": "llegaron 2 quesitos"}

    assert cliente.post("/api/procesar-comando", json=comando,
                        headers=auth).status_code == 422

    cliente.post(
        f"/api/productos/{id_de('QUESO')}/alias", json={"alias": "QUESITO"}, headers=auth
    )

    respuesta = cliente.post("/api/procesar-comando", json=comando, headers=auth)
    assert respuesta.status_code == 200
    assert respuesta.json()["datos_extraidos"]["producto"] == "QUESO"


def test_agregar_un_alias_ya_usado_por_otro_producto_devuelve_409(cliente, auth, id_de):
    respuesta = cliente.post(
        f"/api/productos/{id_de('QUESO')}/alias", json={"alias": "AREPITAS"}, headers=auth
    )
    assert respuesta.status_code == 409
    assert "AREPAS" in respuesta.json()["detail"]


def test_agregar_como_alias_el_propio_nombre_canonico_devuelve_409(cliente, auth, id_de):
    respuesta = cliente.post(
        f"/api/productos/{id_de('QUESO')}/alias", json={"alias": "queso"}, headers=auth
    )
    assert respuesta.status_code == 409


def test_agregar_alias_a_un_producto_inexistente_devuelve_404(cliente, auth):
    respuesta = cliente.post(
        "/api/productos/9999/alias", json={"alias": "LO QUE SEA"}, headers=auth
    )
    assert respuesta.status_code == 404


def test_retirar_producto_es_baja_logica_y_conserva_el_historico(
    cliente, auth, crear_movimiento, sesion_db, id_de
):
    """Borrar la fila fallaría contra la FK RESTRICT, y con otra configuración se
    llevaría el histórico por delante y falsearía el inventario."""
    crear_movimiento("QUESO", 10, "INGRESO")

    assert cliente.delete(
        f"/api/productos/{id_de('QUESO')}", headers=auth
    ).status_code == 200

    assert sesion_db.query(Producto).filter(Producto.nombre == "QUESO").one().activo is False
    assert contar(sesion_db) == 1
    # El saldo del producto retirado sigue contando: lo que se registró, ocurrió.
    stock = cliente.get("/api/stock").json()
    assert next(f for f in stock if f["producto"] == "QUESO")["stock_actual"] == 10


def test_retirar_un_producto_ya_retirado_devuelve_409(cliente, auth, id_de):
    ruta = f"/api/productos/{id_de('QUESO')}"
    cliente.delete(ruta, headers=auth)
    assert cliente.delete(ruta, headers=auth).status_code == 409


def test_retirar_un_producto_inexistente_devuelve_404(cliente, auth):
    assert cliente.delete("/api/productos/9999", headers=auth).status_code == 404


def test_un_producto_retirado_no_aparece_en_el_catalogo_por_defecto(cliente, auth, id_de):
    cliente.delete(f"/api/productos/{id_de('QUESO')}", headers=auth)

    nombres = [p["nombre"] for p in cliente.get("/api/productos").json()]
    assert "QUESO" not in nombres

    todos = cliente.get("/api/productos?incluir_inactivos=true").json()
    assert "QUESO" in [p["nombre"] for p in todos]


def test_reincorporar_un_producto_retirado(cliente, auth, simular_llm, id_de):
    """Un catálogo del que se puede dar de baja pero no volver a dar de alta
    estaría roto: POST no puede recrearlo porque el nombre sigue ocupado."""
    queso_id = id_de("QUESO")
    cliente.delete(f"/api/productos/{queso_id}", headers=auth)

    respuesta = cliente.patch(f"/api/productos/{queso_id}?activo=true", headers=auth)
    assert respuesta.status_code == 200
    assert respuesta.json()["activo"] is True

    simular_llm({"producto": "QUESO", "cantidad": 2, "tipo_movimiento": "INGRESO"})
    assert cliente.post(
        "/api/procesar-comando", json={"texto": "llegaron 2 quesos"}, headers=auth
    ).status_code == 200
