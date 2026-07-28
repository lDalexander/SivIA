"""
Entorno de ejecución de Alembic para SivIA.

Importa el esquema desde models.py (no desde main.py) para que generar
migraciones no exija tener configurada la aplicación completa.
"""
import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

from models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# La URL viene del entorno, nunca de alembic.ini: las credenciales viven
# únicamente en .env.
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL no está definida. Ejecutar las migraciones mediante "
        "scripts/migrar.sh, que la inyecta desde .env."
    )
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Genera el SQL sin conectarse a la base de datos ('alembic upgrade --sql')."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Aplica las migraciones conectándose a la base de datos."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Detecta también cambios de tipo de columna, no solo altas y bajas.
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
