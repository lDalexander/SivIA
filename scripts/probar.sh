#!/usr/bin/env bash
# =========================================================
#  Suite de tests automatizados de SivIA.
#
#  Uso:
#    bash scripts/probar.sh                       # toda la suite
#    bash scripts/probar.sh -k validar            # solo los que coincidan
#    bash scripts/probar.sh tests/test_endpoints.py  # un archivo concreto
#
#  Los tests corren en un contenedor efímero y NO necesitan MySQL ni Ollama:
#  usan SQLite en memoria y un doble del LLM. Por eso la suite tarda segundos y
#  no depende de que los servicios estén levantados.
#
#  pytest vive en requirements-dev.txt y solo se instala en la etapa 'tests' de
#  la imagen, así que la imagen de producción no lo incluye.
# =========================================================
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGEN="sivia-api:tests"

echo "==> Construyendo la imagen de tests (se reutiliza la caché en ejecuciones sucesivas)..."
# El log del build se silencia salvo que falle: buildx escribe en stderr, así que
# se captura y solo se muestra si hay error.
if ! LOG_BUILD="$(docker build --target tests -t "$IMAGEN" ./backend 2>&1)"; then
  echo "$LOG_BUILD" >&2
  echo "ERROR: falló la construcción de la imagen de tests." >&2
  exit 1
fi

echo "==> Ejecutando la suite..."
# --network none: garantiza que ningún test dependa de la red por accidente.
# Un test que necesite red es un test que no está aislado.
docker run --rm --network none "$IMAGEN" pytest "$@"
