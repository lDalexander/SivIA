"""
Tests de validar_entidades(): la barrera que decide si lo que devolvió el LLM
entra en el inventario o se rechaza.

Es la función más importante de la suite. Antes de la auditoría, los datos
ausentes o absurdos se sustituían por valores por defecto (cantidad=0,
tipo_movimiento='INGRESO'), de modo que un EGRESO mal interpretado se guardaba
como un INGRESO fantasma que descuadraba el inventario sin dejar rastro. Estos
tests existen para que ese comportamiento no vuelva.
"""
import pytest
from fastapi import HTTPException

from main import validar_entidades


# --- CASOS VÁLIDOS ---

def test_extraccion_completa_y_correcta():
    producto, cantidad, tipo = validar_entidades(
        {"producto": "AREPAS", "cantidad": 45, "tipo_movimiento": "INGRESO"}
    )
    assert (producto, cantidad, tipo) == ("AREPAS", 45, "INGRESO")


def test_normaliza_producto_a_mayusculas():
    producto, _, _ = validar_entidades(
        {"producto": "  arepas de maíz ", "cantidad": 3, "tipo_movimiento": "EGRESO"}
    )
    assert producto == "AREPAS DE MAÍZ"


def test_normaliza_tipo_movimiento_a_mayusculas():
    _, _, tipo = validar_entidades(
        {"producto": "QUESO", "cantidad": 1, "tipo_movimiento": "egreso"}
    )
    assert tipo == "EGRESO"


def test_acepta_cantidad_como_cadena_numerica():
    """El LLM devuelve a veces "45" en lugar de 45; es convertible y válido."""
    _, cantidad, _ = validar_entidades(
        {"producto": "PAN", "cantidad": "45", "tipo_movimiento": "INGRESO"}
    )
    assert cantidad == 45


def test_trunca_producto_al_limite_de_la_columna():
    """La columna es VARCHAR(100): un nombre más largo debe truncarse, no
    provocar un error de base de datos al insertar."""
    producto, _, _ = validar_entidades(
        {"producto": "A" * 250, "cantidad": 1, "tipo_movimiento": "INGRESO"}
    )
    assert len(producto) == 100


# --- CANTIDAD INVÁLIDA ---

@pytest.mark.parametrize("cantidad", [0, -5, -1])
def test_rechaza_cantidad_no_positiva(cantidad):
    with pytest.raises(HTTPException) as exc:
        validar_entidades(
            {"producto": "AREPAS", "cantidad": cantidad, "tipo_movimiento": "INGRESO"}
        )
    assert exc.value.status_code == 422


@pytest.mark.parametrize("cantidad", ["muchas", None, "", {"n": 3}])
def test_rechaza_cantidad_no_numerica(cantidad):
    with pytest.raises(HTTPException) as exc:
        validar_entidades(
            {"producto": "AREPAS", "cantidad": cantidad, "tipo_movimiento": "INGRESO"}
        )
    assert exc.value.status_code == 422


def test_rechaza_cantidad_ausente():
    with pytest.raises(HTTPException) as exc:
        validar_entidades({"producto": "AREPAS", "tipo_movimiento": "INGRESO"})
    assert exc.value.status_code == 422


# --- PRODUCTO INVÁLIDO ---

@pytest.mark.parametrize("producto", ["", "   ", None, "DESCONOCIDO", "desconocido"])
def test_rechaza_producto_no_identificado(producto):
    """'DESCONOCIDO' era el valor por defecto de la versión anterior: si el LLM
    lo devuelve, es que no identificó el producto y no debe registrarse nada."""
    with pytest.raises(HTTPException) as exc:
        validar_entidades(
            {"producto": producto, "cantidad": 5, "tipo_movimiento": "INGRESO"}
        )
    assert exc.value.status_code == 422


def test_rechaza_producto_ausente():
    with pytest.raises(HTTPException) as exc:
        validar_entidades({"cantidad": 5, "tipo_movimiento": "INGRESO"})
    assert exc.value.status_code == 422


# --- TIPO DE MOVIMIENTO INVÁLIDO ---

@pytest.mark.parametrize(
    "tipo", ["TAL VEZ", "SALIDA", "ENTRADA", "", None, "INGRESO/EGRESO"]
)
def test_rechaza_tipo_movimiento_no_reconocido(tipo):
    """Regresión del bug más grave: cualquier valor no reconocido caía a
    'INGRESO' en silencio. Ahora es 422."""
    with pytest.raises(HTTPException) as exc:
        validar_entidades(
            {"producto": "AREPAS", "cantidad": 5, "tipo_movimiento": tipo}
        )
    assert exc.value.status_code == 422


def test_rechaza_tipo_movimiento_ausente():
    with pytest.raises(HTTPException) as exc:
        validar_entidades({"producto": "AREPAS", "cantidad": 5})
    assert exc.value.status_code == 422


# --- ESTRUCTURA INVÁLIDA ---

@pytest.mark.parametrize("entrada", [None, [], "texto", 42])
def test_rechaza_respuesta_que_no_es_objeto(entrada):
    """El LLM puede devolver JSON válido que no sea un objeto (una lista, un
    número). Debe rechazarse sin lanzar AttributeError."""
    with pytest.raises(HTTPException) as exc:
        validar_entidades(entrada)
    assert exc.value.status_code == 422
