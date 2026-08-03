"""catálogo de productos con alias, y FK desde movimientos_inventario

Cierra el pendiente del catálogo: `producto` era texto libre, así que «AREPA» y
«AREPAS» eran dos productos distintos con dos saldos separados y el stock se
fragmentaba en silencio.

Esta revisión está escrita a mano, no autogenerada, porque incluye **migración de
datos** y el autogenerado no la adivina. El orden importa y no es intercambiable:

  1. Crear las tablas del catálogo.
  2. Sembrar `productos` con los nombres normalizados ya presentes en el
     histórico. Sin este paso, al terminar la migración el catálogo estaría
     vacío y **todos** los comandos serían rechazados por producto desconocido.
  3. Normalizar la columna `producto` del histórico (aquí es donde se arregla el
     `PANES_DE_YACA` que motivó el trabajo: los guiones bajos desaparecen).
  4. Añadir `producto_id` como NULLABLE y rellenarlo.
  5. Solo entonces ponerlo NOT NULL y crear la clave ajena.

Invertir 4 y 5 fallaría en cuanto hubiera una sola fila en el histórico.

Revision ID: c3f1a8b25d47
Revises: b7e4c1a09f52
Create Date: 2026-07-28 01:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

# La normalización se importa del modelo en lugar de reimplementarse: si esta
# migración normalizara distinto que la aplicación, el backfill dejaría filas
# que la API ya no sabría resolver contra el catálogo.
from models import normalizar_nombre

revision: str = "c3f1a8b25d47"
down_revision: Union[str, None] = "b7e4c1a09f52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _migrar_datos() -> None:
    """Siembra el catálogo y rellena producto_id a partir del histórico."""
    conexion = op.get_bind()

    # Cada nombre bruto del histórico se agrupa por su forma canónica: si
    # 'PANES_DE_YUCA' y 'PANES DE YUCA' convivían como dos productos, ambos
    # colapsan a uno solo y sus saldos se unifican, que es el objetivo.
    brutos = [
        fila[0]
        for fila in conexion.execute(
            sa.text("SELECT DISTINCT producto FROM movimientos_inventario")
        )
    ]
    canonicos: dict[str, list[str]] = {}
    for bruto in brutos:
        canonicos.setdefault(normalizar_nombre(bruto), []).append(bruto)

    # Un nombre que se normalice a cadena vacía no puede entrar al catálogo (lo
    # prohíbe la CHECK) y tampoco podría resolverse nunca. No debería existir:
    # validar_entidades rechazaba el producto vacío desde antes de esta revisión.
    vacios = canonicos.pop("", None)
    if vacios:
        raise RuntimeError(
            "El histórico contiene movimientos cuyo producto se normaliza a una "
            f"cadena vacía: {vacios!r}. Corregir o eliminar esas filas antes de "
            "migrar; asignarles un producto arbitrario falsearía el inventario."
        )

    # CURRENT_TIMESTAMP en lugar de una fecha calculada en Python: la fecha de
    # alta de un producto sembrado es el momento de la migración, y así se evita
    # depender de la función de fecha propia de cada motor.
    if canonicos:
        conexion.execute(
            sa.text(
                "INSERT INTO productos (nombre, activo, creado_en) "
                "VALUES (:nombre, 1, CURRENT_TIMESTAMP)"
            ),
            [{"nombre": nombre} for nombre in canonicos],
        )

    for canonico, variantes in canonicos.items():
        producto_id = conexion.execute(
            sa.text("SELECT id FROM productos WHERE nombre = :nombre"),
            {"nombre": canonico},
        ).scalar()

        for bruto in variantes:
            # Se reescribe también el texto: el snapshot histórico pasa a ser la
            # forma canónica. Es una corrección, no una pérdida — 'PANES_DE_YACA'
            # nunca fue un nombre legítimo, sino un artefacto del modelo.
            conexion.execute(
                sa.text(
                    "UPDATE movimientos_inventario "
                    "SET producto = :canonico, producto_id = :pid "
                    "WHERE producto = :bruto"
                ),
                {"canonico": canonico, "pid": producto_id, "bruto": bruto},
            )


def upgrade() -> None:
    op.create_table(
        "productos",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("nombre", sa.String(length=100), nullable=False),
        sa.Column("activo", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("nombre <> ''", name="ck_producto_nombre_no_vacio"),
    )
    op.create_index("ix_productos_id", "productos", ["id"], unique=False)
    op.create_index("ix_productos_nombre", "productos", ["nombre"], unique=True)

    op.create_table(
        "alias_producto",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("alias", sa.String(length=100), nullable=False),
        sa.Column("producto_id", sa.Integer(), nullable=False),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["producto_id"],
            ["productos.id"],
            name="fk_alias_producto",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("alias <> ''", name="ck_alias_no_vacio"),
    )
    op.create_index("ix_alias_producto_id", "alias_producto", ["id"], unique=False)
    op.create_index("ix_alias_producto_alias", "alias_producto", ["alias"], unique=True)
    op.create_index(
        "ix_alias_producto_producto_id", "alias_producto", ["producto_id"], unique=False
    )

    # Nullable primero: si ya hay movimientos, una columna NOT NULL sin valor por
    # defecto no se puede añadir.
    op.add_column(
        "movimientos_inventario", sa.Column("producto_id", sa.Integer(), nullable=True)
    )

    if context.is_offline_mode():
        # 'migrar.sh sql' genera el SQL sin conectarse, así que no puede leer el
        # histórico para sembrar el catálogo. Se avisa en el propio script en vez
        # de emitir un SQL incompleto que parecería aplicable.
        print(
            "-- AVISO: la siembra del catálogo y el backfill de producto_id "
            "consultan\n-- los datos existentes y no pueden generarse en modo "
            "offline (--sql).\n-- El SQL de abajo omite esos pasos: aplicar con "
            "'migrar.sh upgrade'."
        )
    else:
        _migrar_datos()

    op.alter_column(
        "movimientos_inventario",
        "producto_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.create_index(
        "ix_movimientos_inventario_producto_id",
        "movimientos_inventario",
        ["producto_id"],
        unique=False,
    )
    # RESTRICT: borrar un producto del catálogo nunca debe llevarse por delante
    # los movimientos que lo mencionan. Es la tercera capa de defensa del
    # sistema: ni un script ni un 'mysql' manual pueden inventar un producto.
    op.create_foreign_key(
        "fk_movimiento_producto",
        "movimientos_inventario",
        "productos",
        ["producto_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # Se revierte el esquema, no los datos: la normalización de la columna
    # `producto` se queda. Volver a escribir 'PANES_DE_YACA' sería restaurar un
    # error, y el esquema anterior acepta perfectamente el nombre corregido.
    op.drop_constraint(
        "fk_movimiento_producto", "movimientos_inventario", type_="foreignkey"
    )
    op.drop_index(
        "ix_movimientos_inventario_producto_id", table_name="movimientos_inventario"
    )
    op.drop_column("movimientos_inventario", "producto_id")

    # No se borran los índices de estas dos tablas uno por uno: DROP TABLE se
    # lleva sus índices por delante. Y MySQL rechaza borrar el índice que
    # respalda una clave ajena mientras la clave existe ('Cannot drop index
    # ix_alias_producto_producto_id: needed in a foreign key constraint'), así
    # que hacerlo explícitamente además de fallar sería innecesario.
    #
    # El orden entre las dos tablas sí importa: alias_producto referencia a
    # productos, así que la hija se borra primero.
    op.drop_table("alias_producto")
    op.drop_table("productos")
