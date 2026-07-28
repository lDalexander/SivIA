#!/usr/bin/env bash
# =========================================================
#  Gestión de migraciones de esquema (Alembic) para SivIA.
#
#  Alembic se ejecuta en un contenedor EFÍMERO que se destruye al terminar:
#  no hay proceso residente ni consumo de memoria permanente, y no hace falta
#  instalar Python ni Alembic en el host.
#
#  Uso:
#    bash scripts/migrar.sh estado                  # revisión aplicada actualmente
#    bash scripts/migrar.sh historial               # historial de migraciones
#    bash scripts/migrar.sh upgrade                 # aplicar las pendientes
#    bash scripts/migrar.sh nueva "descripción"     # autogenerar desde models.py
#    bash scripts/migrar.sh downgrade               # revertir la última
#    bash scripts/migrar.sh sql                     # ver el SQL sin ejecutarlo
# =========================================================
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "ERROR: falta el archivo .env (copiar desde .env.example)." >&2
  exit 1
fi

# El directorio de migraciones se monta desde el host para que las revisiones
# autogeneradas persistan en el repositorio y no mueran con el contenedor.
#
# --user con el uid/gid del host: la imagen corre como appuser (uid 1001), que no
# puede escribir en un directorio del host propiedad de otro usuario. Así las
# revisiones generadas quedan a nombre de quien ejecuta el script.
alembic_run() {
  docker compose run --rm \
    --user "$(id -u):$(id -g)" \
    -v "$(pwd)/backend/migrations:/app/migrations" \
    api alembic "$@"
}

# Detecta el estado del esquema sin usar el cliente mysql, para no pasar
# credenciales por la línea de comandos.
detectar_estado() {
  docker compose run --rm --no-TTY api python -c "
import os
from sqlalchemy import create_engine, inspect
tablas = inspect(create_engine(os.environ['DATABASE_URL'])).get_table_names()
if 'alembic_version' in tablas:
    print('GESTIONADA')
elif 'movimientos_inventario' in tablas:
    print('HEREDADA')
else:
    print('VACIA')
" 2>/dev/null | tr -d '\r' | tail -1
}

COMANDO="${1:-estado}"

case "$COMANDO" in
  estado)
    echo "==> Revisión aplicada en la base de datos:"
    alembic_run current --verbose
    ;;

  historial)
    alembic_run history --verbose
    ;;

  upgrade)
    estado="$(detectar_estado)"
    case "$estado" in
      HEREDADA)
        # La tabla existe desde antes de adoptar Alembic. Ejecutar la migración
        # inicial fallaría al intentar crear una tabla ya presente, así que se
        # marca como aplicada sin tocar los datos.
        echo "==> Base de datos preexistente detectada, sin control de versiones."
        echo "    Marcando la migración inicial como aplicada (sin alterar datos)..."
        alembic_run stamp b7e4c1a09f52
        echo "==> Aplicando las migraciones posteriores, si hay alguna..."
        alembic_run upgrade head
        ;;
      VACIA)
        echo "==> Base de datos vacía. Creando el esquema desde cero..."
        alembic_run upgrade head
        ;;
      GESTIONADA)
        echo "==> Aplicando migraciones pendientes..."
        alembic_run upgrade head
        ;;
      *)
        echo "ERROR: no se pudo determinar el estado del esquema." >&2
        echo "       Verificar que el contenedor sivia_db esté saludable." >&2
        exit 1
        ;;
    esac
    echo "==> Revisión resultante:"
    alembic_run current
    ;;

  nueva)
    if [ -z "${2:-}" ]; then
      echo "ERROR: falta la descripción de la migración." >&2
      echo "  Ejemplo: bash scripts/migrar.sh nueva \"agregar unidad de medida\"" >&2
      exit 1
    fi
    echo "==> Comparando models.py contra el esquema real de MySQL..."
    alembic_run revision --autogenerate -m "$2"
    echo
    echo "IMPORTANTE: revisar el archivo generado en backend/migrations/versions/"
    echo "antes de aplicarlo. El autogenerado acierta con altas y bajas de"
    echo "columnas, pero no adivina las migraciones de datos ni los renombrados"
    echo "(los ve como una baja más un alta, lo que borraría los datos)."
    ;;

  downgrade)
    echo "==> Revirtiendo la última migración aplicada..."
    alembic_run downgrade -1
    alembic_run current
    ;;

  sql)
    echo "==> SQL que aplicaría 'upgrade' (no se ejecuta nada):"
    alembic_run upgrade head --sql
    ;;

  *)
    echo "Comando no reconocido: $COMANDO" >&2
    echo "Válidos: estado | historial | upgrade | nueva \"desc\" | downgrade | sql" >&2
    exit 1
    ;;
esac
