"""usuarios con roles, sesiones, conversación persistente y acciones pendientes

Añade dos capacidades que hasta ahora no existían:

  1. **Multiusuario.** Antes había una sola API key compartida: cualquiera que la
     tuviera podía hacer cualquier cosa, y los movimientos no registraban autor.
     Ahora cada persona entra con su cuenta y el rol decide si puede eliminar.
  2. **Conversación.** El asistente deja de ser un extractor de una sola frase y
     pasa a mantener un hilo, con las escrituras propuestas y confirmadas en vez
     de ejecutadas a ciegas.

Escrita a mano y no autogenerada, por dos motivos:

  - El autogenerado no ordena las tablas por dependencia de forma fiable cuando
    se crean varias con claves ajenas entre sí en la misma revisión.
  - `movimientos_inventario.usuario_id` se añade **NULLABLE** a propósito y eso
    es una decisión, no un descuido: los movimientos ya registrados no tienen
    autor y no hay forma honesta de inventárselo. Ver el comentario del modelo.

**No siembra ningún usuario.** Es deliberado: sembrar aquí obligaría a escribir
una contraseña en un archivo versionado, que es justo lo que la §6 de CLAUDE.md
prohíbe sin excepciones. El primer usuario se crea con la API key
(`POST /api/usuarios` con `X-API-Key`), que ya vive solo en `.env`.

Revision ID: d9a2c47b1e63
Revises: c3f1a8b25d47
Create Date: 2026-08-02 20:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d9a2c47b1e63"
down_revision: Union[str, None] = "c3f1a8b25d47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Usuarios ---
    # Primera tabla: todas las demás la referencian.
    op.create_table(
        "usuarios",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("usuario", sa.String(length=50), nullable=False),
        sa.Column("nombre", sa.String(length=100), nullable=False),
        # 255 para que quepa el hash con sus parámetros y la sal, y sobre para
        # endurecer scrypt más adelante sin tener que alterar la columna.
        sa.Column("hash_clave", sa.String(length=255), nullable=False),
        sa.Column(
            "rol", sa.String(length=20), nullable=False, server_default="operador"
        ),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("ultimo_acceso", sa.DateTime(), nullable=True),
        sa.CheckConstraint("usuario <> ''", name="ck_usuario_no_vacio"),
        sa.CheckConstraint(
            "rol IN ('admin', 'operador')", name="ck_usuario_rol_valido"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_usuarios_id", "usuarios", ["id"])
    op.create_index("ix_usuarios_usuario", "usuarios", ["usuario"], unique=True)

    # --- Sesiones ---
    op.create_table(
        "sesiones",
        sa.Column("id", sa.Integer(), nullable=False),
        # 64 caracteres: un SHA-256 en hexadecimal. Se guarda el hash del token,
        # nunca el token, para que leer la tabla no permita suplantar a nadie.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("creada_en", sa.DateTime(), nullable=False),
        sa.Column("expira_en", sa.DateTime(), nullable=False),
        sa.Column("revocada", sa.Boolean(), nullable=False, server_default="0"),
        # CASCADE: una sesión no significa nada sin su usuario.
        sa.ForeignKeyConstraint(
            ["usuario_id"], ["usuarios.id"],
            name="fk_sesion_usuario", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sesiones_id", "sesiones", ["id"])
    op.create_index("ix_sesiones_token_hash", "sesiones", ["token_hash"], unique=True)
    op.create_index("ix_sesiones_usuario_id", "sesiones", ["usuario_id"])
    op.create_index("ix_sesiones_expira_en", "sesiones", ["expira_en"])

    # --- Conversaciones ---
    op.create_table(
        "conversaciones",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("titulo", sa.String(length=120), nullable=False),
        sa.Column("creada_en", sa.DateTime(), nullable=False),
        sa.Column("actualizada_en", sa.DateTime(), nullable=False),
        # RESTRICT, no CASCADE: la conversación es rastro de auditoría. Borrar a
        # una persona no debe llevarse lo que pidió antes de que se registrara.
        sa.ForeignKeyConstraint(
            ["usuario_id"], ["usuarios.id"],
            name="fk_conversacion_usuario", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversaciones_id", "conversaciones", ["id"])
    op.create_index("ix_conversaciones_usuario_id", "conversaciones", ["usuario_id"])
    op.create_index(
        "ix_conversaciones_actualizada_en", "conversaciones", ["actualizada_en"]
    )

    # --- Mensajes ---
    op.create_table(
        "mensajes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversacion_id", sa.Integer(), nullable=False),
        sa.Column("rol", sa.String(length=20), nullable=False),
        sa.Column("contenido", sa.Text(), nullable=False),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "rol IN ('usuario', 'asistente')", name="ck_mensaje_rol_valido"
        ),
        # CASCADE: los mensajes son parte de la conversación, no entidades con
        # vida propia. Si se borra el hilo, se van con él.
        sa.ForeignKeyConstraint(
            ["conversacion_id"], ["conversaciones.id"],
            name="fk_mensaje_conversacion", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mensajes_id", "mensajes", ["id"])
    op.create_index("ix_mensajes_conversacion_id", "mensajes", ["conversacion_id"])

    # --- Acciones pendientes de confirmación ---
    op.create_table(
        "acciones_pendientes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversacion_id", sa.Integer(), nullable=False),
        sa.Column("usuario_id", sa.Integer(), nullable=False),
        sa.Column("tipo", sa.String(length=40), nullable=False),
        # JSON serializado: los parámetros exactos que se ejecutarán al
        # confirmar. No se recalculan reinterpretando el texto, para que lo que
        # se ejecuta sea literalmente lo que se le enseñó a la persona.
        sa.Column("parametros", sa.Text(), nullable=False),
        sa.Column("resumen", sa.Text(), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column("creada_en", sa.DateTime(), nullable=False),
        sa.Column("expira_en", sa.DateTime(), nullable=False),
        sa.Column("resuelta_en", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "estado IN ('pendiente', 'confirmada', 'rechazada', 'caducada')",
            name="ck_accion_estado_valido",
        ),
        sa.ForeignKeyConstraint(
            ["conversacion_id"], ["conversaciones.id"],
            name="fk_accion_conversacion", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"], ["usuarios.id"],
            name="fk_accion_usuario", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_acciones_pendientes_id", "acciones_pendientes", ["id"])
    op.create_index(
        "ix_acciones_pendientes_conversacion_id",
        "acciones_pendientes", ["conversacion_id"],
    )
    op.create_index(
        "ix_acciones_pendientes_usuario_id", "acciones_pendientes", ["usuario_id"]
    )
    op.create_index("ix_acciones_pendientes_estado", "acciones_pendientes", ["estado"])

    # --- Autor del movimiento ---
    # NULLABLE y sin backfill: ver la cabecera de esta revisión.
    op.add_column(
        "movimientos_inventario", sa.Column("usuario_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_movimientos_inventario_usuario_id",
        "movimientos_inventario", ["usuario_id"],
    )
    op.create_foreign_key(
        "fk_movimiento_usuario",
        "movimientos_inventario",
        "usuarios",
        ["usuario_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # La clave ajena se suelta antes que el índice que la respalda: MySQL
    # rechaza borrar ese índice mientras la clave existe. Es exactamente el
    # fallo que se encontró al probar el downgrade de la revisión del catálogo.
    op.drop_constraint(
        "fk_movimiento_usuario", "movimientos_inventario", type_="foreignkey"
    )
    op.drop_index(
        "ix_movimientos_inventario_usuario_id", table_name="movimientos_inventario"
    )
    op.drop_column("movimientos_inventario", "usuario_id")

    # Las tablas se borran de hija a madre. Los índices no se enumeran: DROP
    # TABLE se los lleva, y enumerarlos además fallaría con los que respaldan
    # una clave ajena todavía viva.
    op.drop_table("acciones_pendientes")
    op.drop_table("mensajes")
    op.drop_table("conversaciones")
    op.drop_table("sesiones")
    op.drop_table("usuarios")
