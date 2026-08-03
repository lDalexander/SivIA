"""
Tests del motor conversacional.

La propiedad central que se defiende aquí, y de la que cuelga todo lo demás:

    **Una escritura solo ocurre después de una confirmación humana explícita.**

Es lo que permite que el chat dé de alta productos sin reabrir el problema que
cerró el catálogo (la decisión 6 de CLAUDE.md prohíbe el alta automática). Si
esta propiedad se rompiera, el catálogo volvería a llenarse de 'PANES DE YACA'
creados por un modelo de 3B que entendió mal, cada uno con su propio saldo.

Por eso casi todos los tests de escritura comprueban dos cosas y no una: que la
respuesta sea la esperada, **y que la base no haya cambiado** hasta el sí.

La segunda propiedad, igual de importante: los datos de una consulta los pone la
base, nunca el modelo. Los tests de consulta le dan al doble del LLM una
`respuesta` deliberadamente falsa y verifican que no aparece por ningún lado.
"""
from datetime import timedelta

import pytest

from models import (
    AccionPendiente,
    AliasProducto,
    Conversacion,
    Mensaje,
    MovimientoInventario,
    Producto,
    ahora_utc,
)


def _contar(sesion, modelo):
    return sesion.query(modelo).count()


# --- CHARLA ---

def test_charla_responde_sin_tocar_nada(cliente, auth_operador, simular_chat, sesion_db):
    simular_chat({"intencion": "charla", "respuesta": "¡Hola! ¿En qué te ayudo?"})
    antes = _contar(sesion_db, MovimientoInventario)

    r = cliente.post("/api/conversar", json={"texto": "hola"}, headers=auth_operador)

    assert r.status_code == 200
    assert r.json()["respuesta"] == "¡Hola! ¿En qué te ayudo?"
    assert r.json()["requiere_confirmacion"] is False
    assert _contar(sesion_db, MovimientoInventario) == antes


def test_charla_sin_respuesta_del_modelo_no_deja_el_chat_mudo(
    cliente, auth_operador, simular_chat
):
    """Un 3B a veces devuelve el JSON con la respuesta vacía."""
    simular_chat({"intencion": "charla", "respuesta": ""})
    r = cliente.post("/api/conversar", json={"texto": "hola"}, headers=auth_operador)
    assert r.status_code == 200
    assert len(r.json()["respuesta"]) > 20


def test_una_intencion_inventada_se_trata_como_charla(
    cliente, auth_operador, simular_chat, sesion_db
):
    """Un modelo pequeño se inventa intenciones. Caer en charla es lo seguro:
    se responde con palabras y no se escribe nada."""
    simular_chat({"intencion": "borrar_todo_el_inventario", "respuesta": "Vale."})
    r = cliente.post("/api/conversar", json={"texto": "haz algo"}, headers=auth_operador)

    assert r.status_code == 200
    assert r.json()["intencion"] == "charla"
    assert _contar(sesion_db, AccionPendiente) == 0


def test_el_chat_exige_sesion_de_usuario(cliente):
    assert cliente.post("/api/conversar", json={"texto": "hola"}).status_code == 401


# --- CONSULTAS: LOS DATOS LOS PONE LA BASE ---

def test_el_stock_no_lo_redacta_el_modelo(
    cliente, auth_operador, simular_chat, crear_movimiento
):
    """El doble devuelve una cifra falsa a propósito. No debe salir en la respuesta.

    Un modelo que redacte «tienes 999 arepas» acierta a veces y a veces se lo
    inventa, y para quien lo lee el error es indistinguible del acierto.
    """
    crear_movimiento("AREPAS", 45, "INGRESO")
    crear_movimiento("AREPAS", 5, "EGRESO")
    simular_chat({
        "intencion": "consultar_stock",
        "producto": "AREPAS",
        "respuesta": "Tienes 999 arepas.",
    })

    r = cliente.post(
        "/api/conversar", json={"texto": "cuántas arepas tengo"}, headers=auth_operador
    )

    assert r.status_code == 200
    assert "999" not in r.json()["respuesta"]
    assert "40" in r.json()["respuesta"]


def test_la_consulta_devuelve_los_datos_en_bruto(
    cliente, auth_operador, simular_chat, crear_movimiento
):
    """Para que la app pueda pintar una tabla en vez de leer el texto."""
    crear_movimiento("AREPAS", 45, "INGRESO")
    simular_chat({"intencion": "consultar_stock", "producto": "AREPAS"})

    r = cliente.post(
        "/api/conversar", json={"texto": "stock de arepas"}, headers=auth_operador
    )

    datos = r.json()["datos"]
    assert datos[0]["producto"] == "AREPAS"
    assert datos[0]["stock_actual"] == 45


def test_el_stock_del_chat_coincide_con_el_del_endpoint(
    cliente, auth_operador, simular_chat, crear_movimiento
):
    """Las dos vías comparten `_consultar_stock`. Si divergieran, no habría forma
    de saber cuál de las dos cifras es la buena."""
    crear_movimiento("AREPAS", 30, "INGRESO")
    crear_movimiento("EMPANADAS", 12, "INGRESO")
    simular_chat({"intencion": "consultar_stock"})

    del_chat = cliente.post(
        "/api/conversar", json={"texto": "cómo va el stock"}, headers=auth_operador
    ).json()["datos"]
    del_endpoint = cliente.get("/api/stock").json()

    assert [(f["producto"], f["stock_actual"]) for f in del_chat] == [
        (f["producto"], f["stock_actual"]) for f in del_endpoint
    ]


def test_consultar_stock_sin_movimientos(cliente, auth_operador, simular_chat):
    simular_chat({"intencion": "consultar_stock"})
    r = cliente.post(
        "/api/conversar", json={"texto": "qué stock hay"}, headers=auth_operador
    )
    assert r.status_code == 200
    assert r.json()["datos"] == []


def test_consultar_un_producto_que_no_esta_devuelve_el_stock_completo(
    cliente, auth_operador, simular_chat, crear_movimiento
):
    """Más útil que un vacío confuso cuando el modelo transcribió mal el nombre."""
    crear_movimiento("AREPAS", 20, "INGRESO")
    simular_chat({"intencion": "consultar_stock", "producto": "XILOFONOS"})

    r = cliente.post(
        "/api/conversar", json={"texto": "cuántos xilófonos hay"}, headers=auth_operador
    )
    assert len(r.json()["datos"]) == 1


# --- EL FILTRO NO SE ARRASTRA DEL CONTEXTO ---
#
# Fallo medido contra el modelo real: tras preguntar «cuántas arepas tengo»,
# la siguiente pregunta «cómo va el stock ahora?» devolvía producto='AREPAS' y
# el chat respondía el stock de arepas **como si fuera el inventario entero**.
# Quien lo lee concluye que solo tiene arepas.

def test_un_producto_que_no_se_nombro_no_filtra_la_consulta(
    cliente, auth_operador, simular_chat, crear_movimiento
):
    crear_movimiento("AREPAS", 20, "INGRESO")
    crear_movimiento("EMPANADAS", 30, "INGRESO")
    # El modelo arrastra AREPAS del turno anterior, pero el mensaje no lo dice.
    simular_chat({"intencion": "consultar_stock", "producto": "AREPAS"})

    r = cliente.post(
        "/api/conversar", json={"texto": "cómo va el stock ahora?"}, headers=auth_operador
    )

    productos = {f["producto"] for f in r.json()["datos"]}
    assert productos == {"AREPAS", "EMPANADAS"}


def test_un_producto_si_nombrado_si_filtra(
    cliente, auth_operador, simular_chat, crear_movimiento
):
    crear_movimiento("AREPAS", 20, "INGRESO")
    crear_movimiento("EMPANADAS", 30, "INGRESO")
    simular_chat({"intencion": "consultar_stock", "producto": "EMPANADAS"})

    r = cliente.post(
        "/api/conversar", json={"texto": "cuántas empanadas hay?"}, headers=auth_operador
    )

    assert [f["producto"] for f in r.json()["datos"]] == ["EMPANADAS"]


def test_el_filtro_tolera_el_singular(
    cliente, auth_operador, simular_chat, crear_movimiento
):
    """El catálogo guarda 'AREPAS' y la gente dice «una arepa»."""
    crear_movimiento("AREPAS", 20, "INGRESO")
    crear_movimiento("EMPANADAS", 30, "INGRESO")
    simular_chat({"intencion": "consultar_stock", "producto": "AREPAS"})

    r = cliente.post(
        "/api/conversar", json={"texto": "cuánta arepa me queda?"}, headers=auth_operador
    )
    assert [f["producto"] for f in r.json()["datos"]] == ["AREPAS"]


def test_el_filtro_reconoce_por_alias(
    cliente, auth_operador, simular_chat, crear_movimiento
):
    """'AREPITAS' es alias de 'AREPAS' en el catálogo de prueba: si la persona usa
    el alias, el filtro es legítimo aunque el canónico no aparezca."""
    crear_movimiento("AREPAS", 20, "INGRESO")
    crear_movimiento("EMPANADAS", 30, "INGRESO")
    simular_chat({"intencion": "consultar_stock", "producto": "AREPAS"})

    r = cliente.post(
        "/api/conversar", json={"texto": "cuántas arepitas hay?"}, headers=auth_operador
    )
    assert [f["producto"] for f in r.json()["datos"]] == ["AREPAS"]


def test_el_arrastre_tampoco_recorta_los_movimientos(
    cliente, auth_operador, simular_chat, crear_movimiento
):
    crear_movimiento("AREPAS", 20, "INGRESO")
    crear_movimiento("EMPANADAS", 30, "INGRESO")
    simular_chat({"intencion": "consultar_movimientos", "producto": "AREPAS"})

    r = cliente.post(
        "/api/conversar", json={"texto": "muéstrame los últimos movimientos"},
        headers=auth_operador,
    )
    assert len(r.json()["datos"]) == 2


def test_un_producto_de_nombre_corto_se_reconoce(sesion_db):
    """'PAN' no tiene ninguna palabra de más de tres letras: sin el respaldo de
    `_raices`, nunca se consideraría mencionado y jamás podría filtrarse."""
    from main import _mencionado_en

    pan = sesion_db.query(Producto).filter(Producto.nombre == "PAN").one()
    assert _mencionado_en("cuánto pan queda", pan)


def test_una_palabra_que_contiene_a_otra_no_cuenta(sesion_db):
    """'pan' aparece dentro de 'empanadas'. Sin límites de palabra, preguntar por
    las empanadas devolvería el stock del pan."""
    from main import _mencionado_en

    pan = sesion_db.query(Producto).filter(Producto.nombre == "PAN").one()
    assert not _mencionado_en("cuántas empanadas hay", pan)


def test_el_plural_del_catalogo_casa_con_el_singular_dicho(sesion_db):
    from main import _mencionado_en

    arepas = sesion_db.query(Producto).filter(Producto.nombre == "AREPAS").one()
    assert _mencionado_en("me queda alguna arepa?", arepas)
    assert _mencionado_en("cuántas arepas hay", arepas)
    assert not _mencionado_en("cómo va el stock", arepas)


def test_listar_productos(cliente, auth_operador, simular_chat):
    simular_chat({"intencion": "listar_productos"})
    r = cliente.post(
        "/api/conversar", json={"texto": "qué productos manejas"}, headers=auth_operador
    )
    assert r.status_code == 200
    assert "AREPAS" in r.json()["respuesta"]
    assert len(r.json()["datos"]) == 5


def test_consultar_movimientos_muestra_el_autor(
    cliente, auth_operador, simular_chat, crear_movimiento
):
    crear_movimiento("AREPAS", 8, "INGRESO")
    simular_chat({"intencion": "consultar_movimientos"})

    r = cliente.post(
        "/api/conversar", json={"texto": "últimos movimientos"}, headers=auth_operador
    )
    assert r.status_code == 200
    assert "AREPAS" in r.json()["respuesta"]


# --- REGISTRAR UN MOVIMIENTO: PROPONER, NO ESCRIBIR ---

def test_registrar_solo_propone(cliente, auth_operador, simular_chat, sesion_db):
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": 20,
        "tipo_movimiento": "INGRESO",
    })

    r = cliente.post(
        "/api/conversar", json={"texto": "llegaron 20 arepas"}, headers=auth_operador
    )

    assert r.status_code == 200
    assert r.json()["requiere_confirmacion"] is True
    assert r.json()["accion"]["tipo"] == "registrar_movimiento"
    # Lo que importa: todavía no hay nada escrito.
    assert _contar(sesion_db, MovimientoInventario) == 0


def test_confirmar_registra_de_verdad(cliente, auth_operador, simular_chat, sesion_db):
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": 20,
        "tipo_movimiento": "INGRESO",
    })
    propuesta = cliente.post(
        "/api/conversar", json={"texto": "llegaron 20 arepas"}, headers=auth_operador
    ).json()

    r = cliente.post(
        "/api/conversar/confirmar",
        json={"id_accion": propuesta["accion"]["id"], "confirmar": True},
        headers=auth_operador,
    )

    assert r.status_code == 200
    assert _contar(sesion_db, MovimientoInventario) == 1
    movimiento = sesion_db.query(MovimientoInventario).one()
    assert movimiento.producto == "AREPAS"
    assert movimiento.cantidad == 20
    assert movimiento.autor == "martha"


def test_cancelar_no_registra_nada(cliente, auth_operador, simular_chat, sesion_db):
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": 20,
        "tipo_movimiento": "INGRESO",
    })
    propuesta = cliente.post(
        "/api/conversar", json={"texto": "llegaron 20 arepas"}, headers=auth_operador
    ).json()

    r = cliente.post(
        "/api/conversar/confirmar",
        json={"id_accion": propuesta["accion"]["id"], "confirmar": False},
        headers=auth_operador,
    )

    assert r.status_code == 200
    assert _contar(sesion_db, MovimientoInventario) == 0
    assert sesion_db.query(AccionPendiente).one().estado == "rechazada"


def test_un_si_escrito_confirma_igual_que_el_boton(
    cliente, auth_operador, simular_chat, sesion_db
):
    """Por voz nadie pulsa un botón; con el móvil en la mano nadie quiere dictar.
    Los dos caminos deben llevar al mismo sitio."""
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": 20,
        "tipo_movimiento": "INGRESO",
    })
    primera = cliente.post(
        "/api/conversar", json={"texto": "llegaron 20 arepas"}, headers=auth_operador
    ).json()

    # El doble se deja configurado para explotar: si el camino rápido llamara al
    # modelo, este test fallaría, que es exactamente lo que queremos detectar.
    simular_chat(error=AssertionError("el «sí» no debe llegar al modelo"))
    r = cliente.post(
        "/api/conversar",
        json={"texto": "dale", "id_conversacion": primera["id_conversacion"]},
        headers=auth_operador,
    )

    assert r.status_code == 200
    assert _contar(sesion_db, MovimientoInventario) == 1


def test_un_no_escrito_cancela(cliente, auth_operador, simular_chat, sesion_db):
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": 20,
        "tipo_movimiento": "INGRESO",
    })
    primera = cliente.post(
        "/api/conversar", json={"texto": "llegaron 20 arepas"}, headers=auth_operador
    ).json()

    r = cliente.post(
        "/api/conversar",
        json={"texto": "no, cancela", "id_conversacion": primera["id_conversacion"]},
        headers=auth_operador,
    )

    assert r.status_code == 200
    assert _contar(sesion_db, MovimientoInventario) == 0


@pytest.mark.parametrize("texto", ["sí", "Sí!", "dale", "OK", "confirmo", "listo"])
def test_variantes_de_si(texto):
    from main import interpretar_confirmacion
    assert interpretar_confirmacion(texto) is True


@pytest.mark.parametrize("texto", ["no", "No.", "cancela", "olvídalo", "mejor no"])
def test_variantes_de_no(texto):
    from main import interpretar_confirmacion
    assert interpretar_confirmacion(texto) is False


@pytest.mark.parametrize(
    "texto",
    [
        "no, mejor registra 30 arepas",
        "sí pero cámbialo a empanadas",
        "cuántas arepas tengo",
        "",
    ],
)
def test_una_frase_larga_no_es_una_confirmacion(texto):
    """Un mensaje largo que empiece por «no» casi siempre es una petición nueva.
    Ante la duda, None: el turno sigue por el modelo, que es el camino seguro."""
    from main import interpretar_confirmacion
    assert interpretar_confirmacion(texto) is None


def test_sin_cantidad_pregunta_en_vez_de_suponer(
    cliente, auth_operador, simular_chat, sesion_db
):
    """La decisión 1 de CLAUDE.md, ahora conversacional: si hay duda, no se escribe."""
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": None,
        "tipo_movimiento": "INGRESO",
    })

    r = cliente.post(
        "/api/conversar", json={"texto": "llegaron arepas"}, headers=auth_operador
    )

    assert r.status_code == 200
    assert r.json()["requiere_confirmacion"] is False
    assert "cuántas" in r.json()["respuesta"].lower()
    assert _contar(sesion_db, AccionPendiente) == 0


def test_sin_tipo_de_movimiento_pregunta(cliente, auth_operador, simular_chat):
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": 20,
        "tipo_movimiento": None,
    })
    r = cliente.post(
        "/api/conversar", json={"texto": "20 arepas"}, headers=auth_operador
    )
    assert r.json()["requiere_confirmacion"] is False


def test_la_cantidad_como_texto_se_acepta(cliente, auth_operador, simular_chat):
    """Un 3B devuelve tanto 20 como "20" según el día."""
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": "20",
        "tipo_movimiento": "INGRESO",
    })
    r = cliente.post(
        "/api/conversar", json={"texto": "llegaron 20 arepas"}, headers=auth_operador
    )
    assert r.json()["requiere_confirmacion"] is True


def test_la_cantidad_en_letras_no_se_adivina(cliente, auth_operador, simular_chat):
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": "veinte",
        "tipo_movimiento": "INGRESO",
    })
    r = cliente.post(
        "/api/conversar", json={"texto": "llegaron veinte arepas"}, headers=auth_operador
    )
    assert r.json()["requiere_confirmacion"] is False


def test_producto_desconocido_sugiere_y_ofrece_el_alta(
    cliente, auth_operador, simular_chat, sesion_db
):
    """El caso 'PANES DE YACA', ahora en el chat: en vez de un 422 con jerga de
    API, una frase con las dos salidas posibles."""
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "PANES DE YACA",
        "cantidad": 20,
        "tipo_movimiento": "INGRESO",
    })

    r = cliente.post(
        "/api/conversar",
        json={"texto": "llegaron 20 panes de yaca"},
        headers=auth_operador,
    )

    respuesta = r.json()["respuesta"]
    assert "PANES DE YUCA" in respuesta
    assert "crea el producto" in respuesta.lower()
    assert _contar(sesion_db, MovimientoInventario) == 0


def test_producto_sin_parecido_ofrece_crearlo(cliente, auth_operador, simular_chat):
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "XILOFONOS",
        "cantidad": 3,
        "tipo_movimiento": "INGRESO",
    })
    r = cliente.post(
        "/api/conversar", json={"texto": "llegaron 3 xilófonos"}, headers=auth_operador
    )
    assert "crea el producto" in r.json()["respuesta"].lower()


# --- CREAR PRODUCTOS HABLANDO ---

def test_crear_producto_por_chat_requiere_confirmacion(
    cliente, auth_operador, simular_chat, sesion_db
):
    """El alta por voz es lo que se pidió, pero sigue siendo un humano quien la
    autoriza viendo el nombre exacto que se va a crear."""
    simular_chat({
        "intencion": "crear_producto",
        "producto": "CHICHARRON",
        "alias": ["CHICHARRONES"],
    })
    antes = _contar(sesion_db, Producto)

    r = cliente.post(
        "/api/conversar",
        json={"texto": "crea el producto chicharrón"},
        headers=auth_operador,
    )

    assert r.json()["requiere_confirmacion"] is True
    assert "CHICHARRON" in r.json()["accion"]["resumen"]
    assert _contar(sesion_db, Producto) == antes


def test_confirmar_crea_el_producto_con_sus_alias(
    cliente, auth_operador, simular_chat, sesion_db
):
    simular_chat({
        "intencion": "crear_producto",
        "producto": "CHICHARRON",
        "alias": ["CHICHARRONES"],
    })
    propuesta = cliente.post(
        "/api/conversar",
        json={"texto": "crea el producto chicharrón"},
        headers=auth_operador,
    ).json()

    cliente.post(
        "/api/conversar/confirmar",
        json={"id_accion": propuesta["accion"]["id"], "confirmar": True},
        headers=auth_operador,
    )

    producto = sesion_db.query(Producto).filter(Producto.nombre == "CHICHARRON").one()
    assert [a.alias for a in producto.alias] == ["CHICHARRONES"]


def test_crear_un_producto_que_ya_existe_no_propone_nada(
    cliente, auth_operador, simular_chat, sesion_db
):
    simular_chat({"intencion": "crear_producto", "producto": "AREPAS"})
    r = cliente.post(
        "/api/conversar", json={"texto": "crea arepas"}, headers=auth_operador
    )
    assert r.json()["requiere_confirmacion"] is False
    assert _contar(sesion_db, AccionPendiente) == 0


def test_crear_y_registrar_seguido(cliente, auth_operador, simular_chat, sesion_db):
    """El recorrido completo que motivó todo esto: producto nuevo por voz y su
    primer movimiento, sin tocar la API a mano en ningún momento."""
    simular_chat({"intencion": "crear_producto", "producto": "CHICHARRON"})
    alta = cliente.post(
        "/api/conversar", json={"texto": "crea chicharrón"}, headers=auth_operador
    ).json()
    cliente.post(
        "/api/conversar/confirmar",
        json={"id_accion": alta["accion"]["id"], "confirmar": True},
        headers=auth_operador,
    )

    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "CHICHARRON",
        "cantidad": 15,
        "tipo_movimiento": "INGRESO",
    })
    registro = cliente.post(
        "/api/conversar",
        json={"texto": "llegaron 15 chicharrones", "id_conversacion": alta["id_conversacion"]},
        headers=auth_operador,
    ).json()
    cliente.post(
        "/api/conversar/confirmar",
        json={"id_accion": registro["accion"]["id"], "confirmar": True},
        headers=auth_operador,
    )

    movimiento = sesion_db.query(MovimientoInventario).one()
    assert movimiento.producto == "CHICHARRON"
    assert movimiento.cantidad == 15


def test_agregar_alias_por_chat(cliente, auth_operador, simular_chat, sesion_db):
    simular_chat({
        "intencion": "agregar_alias",
        "producto": "PANES DE YUCA",
        "alias": ["BOLLOS"],
    })
    propuesta = cliente.post(
        "/api/conversar",
        json={"texto": "cuando diga bollos me refiero a panes de yuca"},
        headers=auth_operador,
    ).json()
    assert propuesta["requiere_confirmacion"] is True

    cliente.post(
        "/api/conversar/confirmar",
        json={"id_accion": propuesta["accion"]["id"], "confirmar": True},
        headers=auth_operador,
    )

    assert sesion_db.query(AliasProducto).filter(
        AliasProducto.alias == "BOLLOS"
    ).one_or_none() is not None


def test_el_alias_aprendido_resuelve_despues(
    cliente, auth_operador, simular_chat, sesion_db
):
    """Enseñar una variante debe servir para algo: el siguiente comando con ella
    tiene que resolver al canónico."""
    simular_chat({
        "intencion": "agregar_alias",
        "producto": "PANES DE YUCA",
        "alias": ["BOLLOS"],
    })
    propuesta = cliente.post(
        "/api/conversar", json={"texto": "bollos son panes de yuca"}, headers=auth_operador
    ).json()
    cliente.post(
        "/api/conversar/confirmar",
        json={"id_accion": propuesta["accion"]["id"], "confirmar": True},
        headers=auth_operador,
    )

    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "BOLLOS",
        "cantidad": 10,
        "tipo_movimiento": "INGRESO",
    })
    r = cliente.post(
        "/api/conversar", json={"texto": "llegaron 10 bollos"}, headers=auth_operador
    ).json()

    assert r["requiere_confirmacion"] is True
    assert r["accion"]["parametros"]["producto"] == "PANES DE YUCA"


# --- PERMISOS DENTRO DEL CHAT ---

def test_un_operador_no_puede_borrar_hablando(
    cliente, auth_operador, simular_chat, crear_movimiento, sesion_db
):
    """El rol vale igual por chat que por API: si no, bastaría con pedírselo a
    SivIA para saltarse el permiso."""
    movimiento = crear_movimiento("AREPAS", 10, "INGRESO")
    simular_chat({"intencion": "eliminar_movimiento", "id": movimiento.id})

    r = cliente.post(
        "/api/conversar",
        json={"texto": f"borra el movimiento {movimiento.id}"},
        headers=auth_operador,
    )

    assert r.status_code == 200
    assert r.json()["requiere_confirmacion"] is False
    assert "administrador" in r.json()["respuesta"].lower()
    # Ni siquiera se propone: enseñar un botón que va a fallar no ayuda a nadie.
    assert _contar(sesion_db, AccionPendiente) == 0
    assert sesion_db.get(MovimientoInventario, movimiento.id) is not None


def test_un_admin_si_puede_borrar_hablando(
    cliente, auth_admin, simular_chat, crear_movimiento, sesion_db
):
    movimiento = crear_movimiento("AREPAS", 10, "INGRESO")
    simular_chat({"intencion": "eliminar_movimiento", "id": movimiento.id})

    propuesta = cliente.post(
        "/api/conversar",
        json={"texto": f"borra el movimiento {movimiento.id}"},
        headers=auth_admin,
    ).json()
    assert propuesta["requiere_confirmacion"] is True

    cliente.post(
        "/api/conversar/confirmar",
        json={"id_accion": propuesta["accion"]["id"], "confirmar": True},
        headers=auth_admin,
    )
    assert sesion_db.get(MovimientoInventario, movimiento.id) is None


def test_borrar_un_movimiento_inexistente(cliente, auth_admin, simular_chat):
    simular_chat({"intencion": "eliminar_movimiento", "id": 9999})
    r = cliente.post(
        "/api/conversar", json={"texto": "borra el 9999"}, headers=auth_admin
    )
    assert r.json()["requiere_confirmacion"] is False


def test_retirar_un_producto_hablando(cliente, auth_admin, simular_chat, id_de, sesion_db):
    simular_chat({"intencion": "retirar_producto", "producto": "QUESO"})
    propuesta = cliente.post(
        "/api/conversar", json={"texto": "retira el queso"}, headers=auth_admin
    ).json()

    cliente.post(
        "/api/conversar/confirmar",
        json={"id_accion": propuesta["accion"]["id"], "confirmar": True},
        headers=auth_admin,
    )

    assert sesion_db.get(Producto, id_de("QUESO")).activo is False


# --- CICLO DE VIDA DE UNA PROPUESTA ---

def test_no_se_puede_confirmar_dos_veces(cliente, auth_operador, simular_chat, sesion_db):
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": 20,
        "tipo_movimiento": "INGRESO",
    })
    propuesta = cliente.post(
        "/api/conversar", json={"texto": "llegaron 20 arepas"}, headers=auth_operador
    ).json()
    cuerpo = {"id_accion": propuesta["accion"]["id"], "confirmar": True}

    assert cliente.post("/api/conversar/confirmar", json=cuerpo, headers=auth_operador).status_code == 200
    segunda = cliente.post("/api/conversar/confirmar", json=cuerpo, headers=auth_operador)

    assert segunda.status_code == 409
    # Lo que de verdad importa: no se registró por duplicado.
    assert _contar(sesion_db, MovimientoInventario) == 1


def test_una_propuesta_caducada_no_se_ejecuta(
    cliente, auth_operador, simular_chat, sesion_db
):
    """Confirmar mañana un «vendimos 20 arepas» de hoy lo registraría con la
    fecha de mañana y descuadraría el inventario del día."""
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": 20,
        "tipo_movimiento": "INGRESO",
    })
    propuesta = cliente.post(
        "/api/conversar", json={"texto": "llegaron 20 arepas"}, headers=auth_operador
    ).json()

    accion = sesion_db.get(AccionPendiente, propuesta["accion"]["id"])
    accion.expira_en = ahora_utc() - timedelta(minutes=1)
    sesion_db.commit()

    r = cliente.post(
        "/api/conversar/confirmar",
        json={"id_accion": accion.id, "confirmar": True},
        headers=auth_operador,
    )

    assert r.status_code == 409
    assert _contar(sesion_db, MovimientoInventario) == 0


def test_no_se_confirma_la_propuesta_de_otra_persona(
    cliente, auth_operador, auth_admin, simular_chat, sesion_db
):
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": 20,
        "tipo_movimiento": "INGRESO",
    })
    propuesta = cliente.post(
        "/api/conversar", json={"texto": "llegaron 20 arepas"}, headers=auth_operador
    ).json()

    r = cliente.post(
        "/api/conversar/confirmar",
        json={"id_accion": propuesta["accion"]["id"], "confirmar": True},
        headers=auth_admin,
    )

    assert r.status_code == 403
    assert _contar(sesion_db, MovimientoInventario) == 0


def test_una_propuesta_nueva_caduca_la_anterior(
    cliente, auth_operador, simular_chat, sesion_db
):
    """Con dos propuestas vivas en el mismo hilo, un «sí» sería ambiguo."""
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": 20,
        "tipo_movimiento": "INGRESO",
    })
    primera = cliente.post(
        "/api/conversar", json={"texto": "llegaron 20 arepas"}, headers=auth_operador
    ).json()

    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "EMPANADAS",
        "cantidad": 5,
        "tipo_movimiento": "EGRESO",
    })
    cliente.post(
        "/api/conversar",
        json={"texto": "vendimos 5 empanadas", "id_conversacion": primera["id_conversacion"]},
        headers=auth_operador,
    )

    assert sesion_db.get(AccionPendiente, primera["accion"]["id"]).estado == "caducada"
    assert sesion_db.query(AccionPendiente).filter(
        AccionPendiente.estado == "pendiente"
    ).count() == 1


def test_el_si_ejecuta_lo_ultimo_propuesto(cliente, auth_operador, simular_chat, sesion_db):
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": 20,
        "tipo_movimiento": "INGRESO",
    })
    primera = cliente.post(
        "/api/conversar", json={"texto": "llegaron 20 arepas"}, headers=auth_operador
    ).json()
    hilo = primera["id_conversacion"]

    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "EMPANADAS",
        "cantidad": 5,
        "tipo_movimiento": "EGRESO",
    })
    cliente.post(
        "/api/conversar",
        json={"texto": "mejor vendimos 5 empanadas", "id_conversacion": hilo},
        headers=auth_operador,
    )

    cliente.post(
        "/api/conversar", json={"texto": "sí", "id_conversacion": hilo}, headers=auth_operador
    )

    movimiento = sesion_db.query(MovimientoInventario).one()
    assert movimiento.producto == "EMPANADAS"


def test_el_producto_retirado_entre_propuesta_y_confirmacion(
    cliente, auth_operador, simular_chat, sesion_db, id_de
):
    """Entre la propuesta y el sí pasa tiempo real: las comprobaciones se repiten."""
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "QUESO",
        "cantidad": 4,
        "tipo_movimiento": "INGRESO",
    })
    propuesta = cliente.post(
        "/api/conversar", json={"texto": "llegaron 4 quesos"}, headers=auth_operador
    ).json()

    sesion_db.get(Producto, id_de("QUESO")).activo = False
    sesion_db.commit()

    r = cliente.post(
        "/api/conversar/confirmar",
        json={"id_accion": propuesta["accion"]["id"], "confirmar": True},
        headers=auth_operador,
    )

    assert r.status_code == 200
    assert "no registré nada" in r.json()["respuesta"]
    assert _contar(sesion_db, MovimientoInventario) == 0


# --- HILOS Y PERSISTENCIA ---

def test_el_historial_se_guarda(cliente, auth_operador, simular_chat, sesion_db):
    simular_chat({"intencion": "charla", "respuesta": "Hola."})
    r = cliente.post("/api/conversar", json={"texto": "buenas"}, headers=auth_operador)

    mensajes = sesion_db.query(Mensaje).order_by(Mensaje.id).all()
    assert [(m.rol, m.contenido) for m in mensajes] == [
        ("usuario", "buenas"),
        ("asistente", "Hola."),
    ]


def test_el_hilo_se_reutiliza(cliente, auth_operador, simular_chat, sesion_db):
    simular_chat({"intencion": "charla", "respuesta": "Hola."})
    primera = cliente.post(
        "/api/conversar", json={"texto": "buenas"}, headers=auth_operador
    ).json()
    cliente.post(
        "/api/conversar",
        json={"texto": "qué tal", "id_conversacion": primera["id_conversacion"]},
        headers=auth_operador,
    )

    assert _contar(sesion_db, Conversacion) == 1
    assert _contar(sesion_db, Mensaje) == 4


def test_el_titulo_sale_del_primer_mensaje(cliente, auth_operador, simular_chat, sesion_db):
    """Sin pedírselo al modelo: 14-18 s de inferencia para una etiqueta de lista."""
    simular_chat({"intencion": "charla", "respuesta": "Hola."})
    cliente.post(
        "/api/conversar", json={"texto": "cuántas arepas tengo"}, headers=auth_operador
    )
    assert sesion_db.query(Conversacion).one().titulo == "cuántas arepas tengo"


def test_no_se_escribe_en_el_hilo_de_otra_persona(
    cliente, auth_operador, auth_admin, simular_chat
):
    """Ser admin da permiso para borrar movimientos, no para hacerse pasar por
    otra persona dentro de su conversación."""
    simular_chat({"intencion": "charla", "respuesta": "Hola."})
    ajena = cliente.post(
        "/api/conversar", json={"texto": "buenas"}, headers=auth_operador
    ).json()["id_conversacion"]

    r = cliente.post(
        "/api/conversar",
        json={"texto": "hola", "id_conversacion": ajena},
        headers=auth_admin,
    )
    assert r.status_code == 403


def test_listar_conversaciones_solo_muestra_las_propias(
    cliente, auth_operador, auth_admin, simular_chat
):
    simular_chat({"intencion": "charla", "respuesta": "Hola."})
    cliente.post("/api/conversar", json={"texto": "de martha"}, headers=auth_operador)
    cliente.post("/api/conversar", json={"texto": "de john"}, headers=auth_admin)

    de_martha = cliente.get("/api/conversaciones", headers=auth_operador).json()
    assert len(de_martha) == 1
    assert de_martha[0]["titulo"] == "de martha"


def test_recuperar_un_hilo_completo(cliente, auth_operador, simular_chat):
    simular_chat({"intencion": "charla", "respuesta": "Hola."})
    hilo = cliente.post(
        "/api/conversar", json={"texto": "buenas"}, headers=auth_operador
    ).json()["id_conversacion"]

    r = cliente.get(f"/api/conversaciones/{hilo}", headers=auth_operador)
    assert r.status_code == 200
    assert len(r.json()["mensajes"]) == 2


def test_borrar_un_hilo_no_borra_el_inventario(
    cliente, auth_operador, simular_chat, sesion_db
):
    """Los movimientos son hechos del negocio, no parte de una charla."""
    simular_chat({
        "intencion": "registrar_movimiento",
        "producto": "AREPAS",
        "cantidad": 20,
        "tipo_movimiento": "INGRESO",
    })
    propuesta = cliente.post(
        "/api/conversar", json={"texto": "llegaron 20 arepas"}, headers=auth_operador
    ).json()
    cliente.post(
        "/api/conversar/confirmar",
        json={"id_accion": propuesta["accion"]["id"], "confirmar": True},
        headers=auth_operador,
    )

    r = cliente.delete(
        f"/api/conversaciones/{propuesta['id_conversacion']}", headers=auth_operador
    )

    assert r.status_code == 200
    assert _contar(sesion_db, Conversacion) == 0
    assert _contar(sesion_db, Mensaje) == 0
    assert _contar(sesion_db, MovimientoInventario) == 1


# --- FALLOS DEL MOTOR DE IA ---

def test_ollama_caido_no_deja_a_medias(cliente, auth_operador, simular_chat, sesion_db):
    from fastapi import HTTPException

    simular_chat(error=HTTPException(status_code=503, detail="no disponible"))
    r = cliente.post("/api/conversar", json={"texto": "hola"}, headers=auth_operador)

    assert r.status_code == 503
    # El mensaje del usuario sí queda; la respuesta no, porque no la hubo.
    assert _contar(sesion_db, Mensaje) == 1
    assert _contar(sesion_db, MovimientoInventario) == 0


def test_una_respuesta_del_modelo_que_no_es_objeto(cliente, auth_operador, simular_chat):
    simular_chat("esto no es un diccionario")
    r = cliente.post("/api/conversar", json={"texto": "hola"}, headers=auth_operador)
    # No debe reventar con un 500: se trata como cualquier interpretación mala.
    assert r.status_code in (200, 422)


# --- FORMA DEL PROMPT ---
#
# Estos tests no comprueban que el modelo acierte (eso no es nuestro código),
# sino que el prompt conserve las tres propiedades que lo llevaron de 6/10 a
# 10/10 contra el modelo real. El fallo original era silencioso: con historial
# de por medio, el modelo dejaba `cantidad` o `producto` a null y la
# conversación pedía una aclaración que la persona ya había dado.

def test_el_mensaje_actual_va_separado_del_contexto():
    from main import construir_turno

    turno = construir_turno("Persona: hola\nSivIA: buenas", "vendimos 5 empanadas")

    assert "NO es la petición actual" in turno
    assert "### Mensaje a interpretar AHORA:\nvendimos 5 empanadas" in turno


def test_el_mensaje_actual_va_despues_del_contexto():
    """Un modelo pequeño atiende sobre todo al final del prompt."""
    from main import construir_turno

    turno = construir_turno("Persona: cuántas arepas tengo", "vendimos 5 empanadas")
    assert turno.index("cuántas arepas tengo") < turno.index("vendimos 5 empanadas")


def test_el_turno_recuerda_las_llaves_al_final():
    """Es el cambio que más pesó: sin este recordatorio, las reglas del `system`
    quedan demasiado lejos para un 3B una vez hay conversación de por medio."""
    from main import construir_turno

    turno = construir_turno("Persona: hola", "vendimos 5 empanadas")
    cola = turno[turno.index("vendimos 5 empanadas"):]
    for llave in ("intencion", "producto", "cantidad", "tipo_movimiento", "alias", "id"):
        assert llave in cola


def test_sin_historial_no_se_inventa_una_seccion_de_contexto():
    from main import construir_turno

    turno = construir_turno("", "hola")
    assert "Contexto de la conversación" not in turno


def test_el_historial_no_repite_el_mensaje_en_curso(
    cliente, auth_operador, simular_chat, sesion_db, monkeypatch
):
    """El mensaje actual va en su propia sección; duplicarlo en el contexto le
    daría al modelo dos peticiones donde solo hay una."""
    import main

    capturado = {}

    async def espia(system, prompt):
        capturado["prompt"] = prompt
        return {"intencion": "charla", "respuesta": "Hola."}

    monkeypatch.setattr(main, "consultar_ollama_chat", espia)

    hilo = cliente.post(
        "/api/conversar", json={"texto": "primer mensaje"}, headers=auth_operador
    ).json()["id_conversacion"]
    cliente.post(
        "/api/conversar",
        json={"texto": "segundo mensaje", "id_conversacion": hilo},
        headers=auth_operador,
    )

    prompt = capturado["prompt"]
    assert prompt.count("segundo mensaje") == 1
    assert "primer mensaje" in prompt


def test_mensaje_vacio_se_rechaza(cliente, auth_operador):
    assert cliente.post(
        "/api/conversar", json={"texto": "   "}, headers=auth_operador
    ).status_code == 422
