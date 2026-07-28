"""
Tests de extraer_json(): la red de seguridad ante los envoltorios de markdown
que los LLM añaden aunque se les pida explícitamente que no lo hagan.

Con format='json' Ollama ya garantiza JSON válido, pero esta función sigue
cubriendo el caso de cambiar de modelo o desactivar esa opción.
"""
import json

import pytest

from main import extraer_json


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        # JSON pelado, el caso normal con format='json'.
        ('{"producto": "AREPAS"}', '{"producto": "AREPAS"}'),
        # Espacios y saltos de línea alrededor.
        ('  \n {"producto": "AREPAS"}  \n ', '{"producto": "AREPAS"}'),
        # Bloque de código etiquetado.
        ('```json\n{"producto": "AREPAS"}\n```', '{"producto": "AREPAS"}'),
        # Bloque de código sin etiqueta.
        ('```\n{"producto": "AREPAS"}\n```', '{"producto": "AREPAS"}'),
        # Con texto explicativo antes, que el modelo añade a veces.
        ('Aquí tienes:\n```json\n{"producto": "AREPAS"}\n```', '{"producto": "AREPAS"}'),
        # Etiqueta en la misma línea que la llave de apertura.
        ('```json {"producto": "AREPAS"}```', '{"producto": "AREPAS"}'),
    ],
)
def test_extrae_json_de_envoltorios_habituales(entrada, esperado):
    assert extraer_json(entrada) == esperado


@pytest.mark.parametrize(
    "entrada",
    [
        '{"producto": "AREPAS", "cantidad": 45, "tipo_movimiento": "INGRESO"}',
        '```json\n{"producto": "AREPAS", "cantidad": 45, "tipo_movimiento": "INGRESO"}\n```',
        '```\n{"producto": "AREPAS", "cantidad": 45, "tipo_movimiento": "INGRESO"}\n```',
    ],
)
def test_el_resultado_es_json_parseable(entrada):
    """La verificación que de verdad importa: lo que sale debe poder pasar por
    json.loads(), que es lo que hace consultar_ollama() a continuación."""
    resultado = json.loads(extraer_json(entrada))
    assert resultado["producto"] == "AREPAS"
    assert resultado["cantidad"] == 45


def test_no_confunde_la_palabra_json_dentro_del_contenido():
    """Solo debe quitarse el prefijo 'json' de la etiqueta del bloque, no una
    aparición de esa palabra en los datos."""
    entrada = '{"producto": "MANUAL DE JSON"}'
    assert extraer_json(entrada) == '{"producto": "MANUAL DE JSON"}'


@pytest.mark.parametrize("entrada", ["", "   ", "no soy json", "```"])
def test_no_lanza_excepcion_ante_entradas_degeneradas(entrada):
    """extraer_json solo limpia; la validación del JSON es de quien la llama.
    Su contrato es no romperse, para que el error se maneje como un 422 y no
    como un 500."""
    assert isinstance(extraer_json(entrada), str)
