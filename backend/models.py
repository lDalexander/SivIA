"""
Modelo de datos de SivIA.

Vive en su propio módulo para que Alembic pueda importar el esquema sin
arrastrar la configuración de la aplicación (`main.py` aborta el arranque si
faltan DATABASE_URL o SIVIA_API_KEY, lo que impediría generar migraciones).
"""
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, DateTime, CheckConstraint
from sqlalchemy.orm import declarative_base

Base = declarative_base()

TIPOS_MOVIMIENTO_VALIDOS = ("INGRESO", "EGRESO")


def ahora_utc() -> datetime:
    """UTC como datetime naive: MySQL DATETIME no almacena zona horaria,
    así que se guarda UTC explícitamente y se marca como tal en la salida."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MovimientoInventario(Base):
    __tablename__ = "movimientos_inventario"
    __table_args__ = (
        # La integridad no depende solo de la validación en Python: si algún día
        # se escribe por SQL directo, el motor sigue rechazando datos imposibles.
        CheckConstraint("cantidad > 0", name="ck_cantidad_positiva"),
        CheckConstraint(
            "tipo_movimiento IN ('INGRESO', 'EGRESO')",
            name="ck_tipo_movimiento_valido",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    producto = Column(String(100), nullable=False, index=True)
    cantidad = Column(Integer, nullable=False)
    tipo_movimiento = Column(String(20), nullable=False)  # 'INGRESO' o 'EGRESO'
    fecha = Column(DateTime, nullable=False, default=ahora_utc, index=True)
