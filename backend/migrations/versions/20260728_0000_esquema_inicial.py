"""esquema inicial: movimientos_inventario

Refleja el esquema que ya existía en producción en el momento de adoptar
Alembic, incluidas las constraints CHECK y el índice de 'fecha' que en su día se
aplicaron con ALTER TABLE manual porque create_all() no altera tablas existentes.

Sobre una base de datos que ya tiene esta tabla, NO se ejecuta esta migración:
se marca como aplicada con 'alembic stamp head' (lo hace scripts/migrar.sh
automáticamente). En una instalación nueva y vacía sí se ejecuta y crea la tabla.

Revision ID: b7e4c1a09f52
Revises:
Create Date: 2026-07-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7e4c1a09f52"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "movimientos_inventario",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("producto", sa.String(length=100), nullable=False),
        sa.Column("cantidad", sa.Integer(), nullable=False),
        sa.Column("tipo_movimiento", sa.String(length=20), nullable=False),
        sa.Column("fecha", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("cantidad > 0", name="ck_cantidad_positiva"),
        sa.CheckConstraint(
            "tipo_movimiento IN ('INGRESO', 'EGRESO')",
            name="ck_tipo_movimiento_valido",
        ),
    )
    op.create_index(
        "ix_movimientos_inventario_id", "movimientos_inventario", ["id"], unique=False
    )
    op.create_index(
        "ix_movimientos_inventario_producto",
        "movimientos_inventario",
        ["producto"],
        unique=False,
    )
    op.create_index(
        "ix_movimientos_inventario_fecha",
        "movimientos_inventario",
        ["fecha"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_movimientos_inventario_fecha", table_name="movimientos_inventario")
    op.drop_index("ix_movimientos_inventario_producto", table_name="movimientos_inventario")
    op.drop_index("ix_movimientos_inventario_id", table_name="movimientos_inventario")
    op.drop_table("movimientos_inventario")
