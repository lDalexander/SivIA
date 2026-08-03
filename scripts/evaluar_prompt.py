"""Banco de pruebas del prompt del chat contra el modelo real.

**No es un test y no forma parte de `probar.sh`.** La suite automatizada aísla a
Ollama a propósito: prueba qué hace el backend con lo que el modelo devuelva,
que es lo único que controlamos. Esto es lo complementario — mide si el modelo
entiende, y para eso necesita el modelo de verdad, tarda minutos y su resultado
depende de la versión de Qwen que haya instalada. Un test así en CI sería un
test que falla por motivos ajenos al código.

Existe porque hizo falta. La primera versión del prompt del chat pasaba los 239
tests unitarios y aun así se comportaba mal en cuanto había conversación de por
medio: el modelo dejaba `cantidad` a null en «vendimos 5 empanadas», convertía
«crea el producto chicharrón con arepa» en `producto='AREPAS'` y arrastraba el
producto del turno anterior. Ninguno de esos fallos es visible con un doble.
Medir variantes aquí llevó el acierto de 10/14 a 12/14, y los dos restantes se
resolvieron en código (ver `_mencionado_en` en `main.py`).

Uso — desde la raíz del proyecto, con Ollama corriendo:

    docker run --rm --network host -v "$PWD/backend:/app" -v "$PWD/scripts:/s" \\
      -w /app -e DATABASE_URL=sqlite:// -e SIVIA_API_KEY=x \\
      --entrypoint python sivia-api:latest /s/evaluar_prompt.py

Al tocar el prompt del chat: ejecutarlo antes y después, y no dar por buena una
redacción que baje el marcador. Añadir un caso nuevo cada vez que se observe un
fallo real en uso.

Los dos casos que hoy fallan están documentados a propósito, no pendientes de
arreglar:

  - «vendimos 3 chicharrones con arepa» -> el modelo responde 'AREPAS'. La frase
    contiene literalmente «arepa»; es un caso genuinamente ambiguo. Lo atrapa la
    confirmación: la persona lee «salida de 3 AREPAS» y dice que no.
  - «cómo va el stock ahora?» -> el modelo arrastra 'AREPAS' del contexto. El
    backend ya lo descarta con `_mencionado_en`, así que el comportamiento final
    es correcto aunque la interpretación del modelo no lo sea. Este banco mide
    solo la salida cruda del modelo.
"""
import json
import urllib.request

import main

CATALOGO = ["AREPAS", "EMPANADAS", "PANES DE YUCA"]

HIST = (
    "Persona: Hola SivIA, quién eres?\n"
    "SivIA: Hola! Soy SivIA, tu asistente de inventario.\n"
    "Persona: cuántas arepas tengo?\n"
    "SivIA: Tienes 29 de AREPAS (46 entradas y 17 salidas).\n"
)

CASOS = [
    ("Hola SivIA, quién eres?", "", "charla", {}),
    ("gracias, muy amable", HIST, "charla", {}),
    ("llegaron 20 panes de yuca esta mañana", "", "registrar_movimiento",
     {"producto": "llena", "cantidad": "llena", "tipo_movimiento": "llena"}),
    ("llegaron 20 panes de yuca esta mañana", HIST, "registrar_movimiento",
     {"producto": "llena", "cantidad": "llena", "tipo_movimiento": "llena"}),
    ("vendimos 5 empanadas", HIST, "registrar_movimiento",
     {"producto": "llena", "cantidad": "llena", "tipo_movimiento": "llena"}),
    ("crea el producto chicharron con arepa", "", "crear_producto",
     {"producto": ("no", "AREPAS")}),
    ("crea el producto chicharron con arepa", HIST, "crear_producto",
     {"producto": ("no", "AREPAS")}),
    ("vendimos 3 chicharrones con arepa", HIST, "registrar_movimiento",
     {"producto": ("no", "AREPAS"), "cantidad": "llena"}),
    ("cómo va el stock ahora?", HIST, "consultar_stock", {"producto": "vacio"}),
    ("cuántas empanadas hay?", HIST, "consultar_stock", {"producto": "llena"}),
    ("qué productos manejas?", "", "listar_productos", {}),
    ("borra el movimiento 12", HIST, "eliminar_movimiento", {"id": "llena"}),
    ("cuando diga bollos me refiero a panes de yuca", "", "agregar_alias",
     {"producto": "llena", "alias": "llena"}),
    ("muéstrame los últimos movimientos", HIST, "consultar_movimientos", {}),
]


def preguntar(system, prompt):
    cuerpo = json.dumps({
        "model": "qwen2.5:3b", "system": system, "prompt": prompt,
        "stream": False, "format": "json", "options": {"temperature": 0},
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate", cuerpo,
        {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(json.loads(r.read())["response"])


def revisar(d, campo, regla):
    valor = d.get(campo)
    vacio = valor in (None, "", [], "null")
    if regla == "llena":
        return (not vacio, f"{campo}={valor!r}")
    if regla == "vacio":
        return (vacio, f"{campo}={valor!r}")
    _, prohibido = regla
    return (not vacio and str(valor).upper() != prohibido, f"{campo}={valor!r}")


def evaluar(nombre, system, construir_turno):
    print(f"\n{'='*72}\n{nombre}\n{'='*72}")
    aciertos = 0
    for mensaje, historial, esperada, reglas in CASOS:
        d = preguntar(system, construir_turno(historial, mensaje))
        problemas = []
        if d.get("intencion") != esperada:
            problemas.append(f"intencion={d.get('intencion')!r}")
        for campo, regla in reglas.items():
            ok, detalle = revisar(d, campo, regla)
            if not ok:
                problemas.append(detalle)
        aciertos += not problemas
        marca = "✓" if not problemas else "✗"
        ctx = "H" if historial else " "
        print(f"  {marca} [{ctx}] {mensaje[:40]:42}{'  '.join(problemas)}")
    print(f"  --> {aciertos}/{len(CASOS)}")
    return aciertos


if __name__ == "__main__":
    # Para comparar una variante: definir aquí su `system` y su constructor de
    # turno, y llamar a evaluar() una segunda vez. Así se ven los dos marcadores
    # uno debajo del otro sobre exactamente los mismos casos.
    evaluar(
        "Prompt vigente en main.py",
        main.construir_prompt_chat(CATALOGO),
        main.construir_turno,
    )
