# =========================================================
#  SivIA Core - Imagen del backend FastAPI (ARM64 / Raspberry Pi 5)
#  Build multi-etapa: las herramientas de compilación se quedan en el
#  builder y no viajan en la imagen final.
# =========================================================

# ---------- ETAPA 1: BUILDER ----------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential solo aquí: necesario si alguna dependencia no trae wheel
# precompilado para arm64. No acaba en la imagen de ejecución.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Entorno virtual aislado, fácil de copiar a la etapa final.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ---------- ETAPA 2: RUNTIME ----------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Usuario sin privilegios: un fallo en la app no implica root en el contenedor.
RUN groupadd --system --gid 1001 sivia \
    && useradd --system --uid 1001 --gid sivia --create-home appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=appuser:sivia . .

USER appuser

EXPOSE 8000

# Healthcheck con la stdlib de Python para no tener que instalar curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]


# ---------- ETAPA 3: TESTS (no forma parte de la imagen de producción) ----------
# Se construye solo con --target tests (ver scripts/probar.sh). El servicio 'api'
# del compose fija 'target: runtime', así que esta etapa nunca llega a la Pi y la
# imagen de producción no carga con pytest ni sus dependencias.
FROM runtime AS tests

USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
USER appuser

# La suite no necesita MySQL ni Ollama: usa SQLite en memoria y un doble del LLM.
CMD ["pytest"]
