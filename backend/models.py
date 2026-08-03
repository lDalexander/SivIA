"""
Modelo de datos de SivIA.

Vive en su propio módulo para que Alembic pueda importar el esquema sin
arrastrar la configuración de la aplicación (`main.py` aborta el arranque si
faltan DATABASE_URL o SIVIA_API_KEY, lo que impediría generar migraciones).

Las funciones de normalización también viven aquí, y no en `main.py`, porque las
migraciones de datos necesitan aplicar exactamente la misma normalización que la
aplicación. Si divergieran, el backfill dejaría filas que la API ya no sabría
resolver.
"""
import hashlib
import re
import secrets
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

TIPOS_MOVIMIENTO_VALIDOS = ("INGRESO", "EGRESO")

# Roles de usuario. Deliberadamente solo dos: la diferencia que el negocio pidió
# es «quién puede borrar», no una matriz de permisos. Un tercer rol se añade el
# día que exista una tercera respuesta a esa pregunta, no antes.
#
#   - 'operador': conversa, consulta y registra (movimientos, productos, alias).
#   - 'admin':    todo lo anterior, y además eliminar movimientos, retirar
#                 productos y gestionar usuarios.
ROLES_VALIDOS = ("admin", "operador")

# Autores posibles de un mensaje de la conversación.
ROLES_MENSAJE = ("usuario", "asistente")

# Ciclo de vida de una acción propuesta por el asistente.
ESTADOS_ACCION = ("pendiente", "confirmada", "rechazada", "caducada")

# Longitud máxima de un nombre de producto, compartida por el modelo y por la
# normalización para que un nombre nunca se trunque después de validarse.
LONGITUD_MAX_PRODUCTO = 100

# Longitud máxima de un nombre de usuario, con el mismo criterio.
LONGITUD_MAX_USUARIO = 50

# Parámetros de scrypt para el hash de contraseñas. n=2^14 con r=8 consume unos
# 16 MB y ~50 ms por derivación en el Pi 5: imperceptible en un login, caro de
# multiplicar por millones para quien intente romper la tabla por fuerza bruta.
# Subirlos más castigaría al Pi sin que el modelo de amenaza lo justifique (la
# API no está expuesta a internet, solo al tailnet).
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def ahora_utc() -> datetime:
    """UTC como datetime naive: MySQL DATETIME no almacena zona horaria,
    así que se guarda UTC explícitamente y se marca como tal en la salida."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalizar_nombre(bruto: object) -> str:
    """Forma canónica de un nombre de producto.

    Existe porque el LLM devuelve el mismo producto escrito de varias maneras.
    El defecto que motivó el catálogo fue «Llegaron 20 panes de yuca» guardado
    como `PANES_DE_YACA`: el modelo insertó guiones bajos por su cuenta. El
    prompt pide mayúsculas, pero un prompt es una sugerencia y esto es una
    garantía — los separadores se arreglan aquí, no confiando en el modelo.

    Deliberadamente NO corrige ortografía: «YACA» sigue siendo «YACA» al salir
    de aquí. Inventar la palabra correcta es precisamente lo que no queremos que
    haga nadie, ni el modelo ni nosotros. De la ortografía se ocupa la
    resolución contra el catálogo, que sugiere en vez de adivinar.
    """
    limpio = str(bruto or "")
    # Guiones bajos y guiones a espacios: son artefactos del modelo, no parte
    # del nombre. Ningún producto de alimentación se llama con guion bajo.
    limpio = re.sub(r"[_\-]+", " ", limpio)
    # Colapsar espacios repetidos: 'PAN  DE   YUCA' y 'PAN DE YUCA' son lo mismo.
    limpio = re.sub(r"\s+", " ", limpio).strip().upper()
    return limpio[:LONGITUD_MAX_PRODUCTO]


def clave_busqueda(bruto: object) -> str:
    """Clave insensible a acentos y puntuación, para comparar nombres.

    Solo se usa para *buscar*, nunca para almacenar: el catálogo guarda el
    nombre tal como lo escribió la persona que lo dio de alta, con sus acentos.
    Pero el LLM transcribe «MAIZ» tanto como «MAÍZ», y ambas deben resolver al
    mismo producto sin obligar a registrar un alias para cada tilde.
    """
    normalizado = normalizar_nombre(bruto)
    # NFD separa la letra de su acento; descartar las marcas deja la letra base.
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFD", normalizado)
        if not unicodedata.combining(c)
    )
    return re.sub(r"[^A-Z0-9 ]+", "", sin_acentos).strip()


def hashear_clave(clave: str) -> str:
    """Deriva el hash de una contraseña con scrypt y una sal aleatoria por usuario.

    scrypt viene en la biblioteca estándar (`hashlib`), así que esto no añade
    ninguna dependencia a la imagen. Se eligió frente a un simple SHA-256 porque
    es deliberadamente costoso en CPU **y en memoria**: un atacante con el
    volcado de la tabla no puede probar millones de contraseñas por segundo con
    una GPU, que es exactamente el escenario contra el que sirve un hash de
    contraseña.

    La sal es por usuario para que dos personas con la misma contraseña tengan
    hashes distintos, y para que no se pueda precomputar una tabla.

    Vive aquí junto al modelo, y no en `main.py`, por la misma razón que
    `normalizar_nombre()`: la migración que siembra el primer administrador
    necesita generar un hash que la aplicación sepa verificar. Si las dos
    implementaciones divergieran, el usuario sembrado no podría entrar nunca.

    Formato almacenado: ``scrypt$n$r$p$sal_hex$hash_hex`` — los parámetros viajan
    con el hash para poder endurecerlos en el futuro sin invalidar las
    contraseñas ya guardadas.
    """
    sal = secrets.token_bytes(16)
    derivado = hashlib.scrypt(
        clave.encode("utf-8"), salt=sal, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${sal.hex()}${derivado.hex()}"


def verificar_clave(clave: str, almacenado: str) -> bool:
    """Comprueba una contraseña contra su hash almacenado.

    Devuelve False —nunca lanza— ante un hash corrupto o con un formato que no
    se reconoce: un registro dañado debe impedir el acceso a esa cuenta, no
    tumbar el endpoint de login para todo el mundo.

    La comparación final usa `compare_digest` para no filtrar por tiempo cuánto
    prefijo del hash era correcto.
    """
    try:
        etiqueta, n, r, p, sal_hex, hash_hex = str(almacenado).split("$")
        if etiqueta != "scrypt":
            return False
        derivado = hashlib.scrypt(
            clave.encode("utf-8"),
            salt=bytes.fromhex(sal_hex),
            n=int(n), r=int(r), p=int(p),
            dklen=len(hash_hex) // 2,
        )
    except (ValueError, TypeError, AttributeError):
        return False
    return secrets.compare_digest(derivado.hex(), hash_hex)


def normalizar_usuario(bruto: object) -> str:
    """Forma canónica de un nombre de usuario: minúsculas, sin espacios sobrantes.

    Que 'Martha', 'martha' y ' MARTHA ' sean la misma cuenta evita el fallo de
    login más tonto posible en un teclado de móvil, donde la primera letra se
    escribe en mayúscula sola.
    """
    return re.sub(r"\s+", "", str(bruto or "")).lower()[:LONGITUD_MAX_USUARIO]


class Usuario(Base):
    """Persona que usa el sistema, con su rol.

    Existe porque el negocio lo maneja más de una persona y no todas deben poder
    borrar. Antes había una sola API key compartida: cualquiera que la tuviera
    podía hacer cualquier cosa, y los movimientos no registraban quién los había
    creado, así que un registro erróneo no tenía autor a quien preguntar.

    La API key **no desaparece**: sigue siendo la credencial de máquina (scripts,
    `curl`, healthchecks) y equivale a un administrador. Lo que cambia es que las
    personas ya no la usan; entran con su cuenta.
    """

    __tablename__ = "usuarios"
    __table_args__ = (
        CheckConstraint("usuario <> ''", name="ck_usuario_no_vacio"),
        CheckConstraint(
            "rol IN ('admin', 'operador')", name="ck_usuario_rol_valido"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    usuario = Column(String(LONGITUD_MAX_USUARIO), nullable=False, unique=True, index=True)
    nombre = Column(String(100), nullable=False)
    hash_clave = Column(String(255), nullable=False)
    rol = Column(String(20), nullable=False, default="operador", server_default="operador")
    # Baja lógica, igual que en el catálogo: los movimientos y las conversaciones
    # de una persona que deja el negocio deben seguir existiendo con su autor.
    activo = Column(Boolean, nullable=False, default=True, server_default="1")
    creado_en = Column(DateTime, nullable=False, default=ahora_utc)
    ultimo_acceso = Column(DateTime, nullable=True)

    @property
    def es_admin(self) -> bool:
        return self.rol == "admin"


class Sesion(Base):
    """Sesión activa de un usuario tras iniciar sesión.

    Se eligió un token opaco en base de datos frente a un JWT autocontenido por
    una razón concreta: **poder revocarlo**. Un JWT firmado es válido hasta que
    expira, aunque se despida a la persona o se pierda el móvil; una fila se
    borra. En un sistema con tres usuarios el coste de la consulta por petición
    es irrelevante, y no añade ninguna dependencia.

    Se guarda el **hash** del token, nunca el token: quien lea la tabla no puede
    suplantar a nadie con lo que ve. Es el mismo criterio que con las
    contraseñas, y por eso aquí basta SHA-256 sin sal —el token ya son 256 bits
    aleatorios, no algo adivinable por fuerza bruta ni por diccionario—.
    """

    __tablename__ = "sesiones"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    usuario_id = Column(
        Integer, ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    creada_en = Column(DateTime, nullable=False, default=ahora_utc)
    # Indexada porque la limpieza de sesiones caducadas barre por esta columna.
    expira_en = Column(DateTime, nullable=False, index=True)
    revocada = Column(Boolean, nullable=False, default=False, server_default="0")

    usuario = relationship("Usuario", lazy="joined")


class Conversacion(Base):
    """Hilo de chat entre una persona y SivIA.

    El historial vive en el servidor y no en el móvil para que la misma
    conversación se vea desde cualquier dispositivo y sobreviva a reinstalar la
    app. También es lo que permite auditar después qué se le pidió al asistente
    antes de que registrara algo.
    """

    __tablename__ = "conversaciones"

    id = Column(Integer, primary_key=True, index=True)
    # RESTRICT: la conversación es parte del rastro de auditoría. Si algún día se
    # borra de verdad a un usuario, esto obliga a decidir explícitamente qué
    # pasa con su historial en lugar de que desaparezca solo.
    usuario_id = Column(
        Integer, ForeignKey("usuarios.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    titulo = Column(String(120), nullable=False, default="Nueva conversación")
    creada_en = Column(DateTime, nullable=False, default=ahora_utc)
    # Indexada: la lista de conversaciones se ordena por actividad reciente.
    actualizada_en = Column(DateTime, nullable=False, default=ahora_utc, index=True)

    usuario = relationship("Usuario", lazy="joined")
    mensajes = relationship(
        "Mensaje",
        back_populates="conversacion",
        cascade="all, delete-orphan",
        order_by="Mensaje.id",
    )


class Mensaje(Base):
    """Un turno de la conversación: lo que dijo la persona o lo que respondió SivIA."""

    __tablename__ = "mensajes"
    __table_args__ = (
        CheckConstraint(
            "rol IN ('usuario', 'asistente')", name="ck_mensaje_rol_valido"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    conversacion_id = Column(
        Integer,
        ForeignKey("conversaciones.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rol = Column(String(20), nullable=False)
    # Text y no String: una respuesta con un listado de stock supera holgadamente
    # los 255 caracteres de un VARCHAR corto.
    contenido = Column(Text, nullable=False)
    creado_en = Column(DateTime, nullable=False, default=ahora_utc)

    conversacion = relationship("Conversacion", back_populates="mensajes")


class AccionPendiente(Base):
    """Escritura que SivIA ha propuesto y espera confirmación para ejecutar.

    Es la pieza central de la conversación: el asistente **nunca** escribe por su
    cuenta. Interpreta, propone en lenguaje natural, y solo actúa cuando la
    persona confirma. Así el chat puede dar de alta productos —lo que antes
    estaba prohibido automáticamente— sin reabrir el agujero que cerró el
    catálogo, porque el alta la autoriza un humano que ve el nombre exacto que se
    va a crear.

    Los parámetros se guardan como JSON y **no se recalculan** al confirmar: lo
    que se ejecuta es exactamente lo que se le enseñó a la persona. Si se
    reinterpretara el texto en la confirmación, el modelo podría entender otra
    cosa la segunda vez y se ejecutaría algo que nadie aprobó.
    """

    __tablename__ = "acciones_pendientes"
    __table_args__ = (
        CheckConstraint(
            "estado IN ('pendiente', 'confirmada', 'rechazada', 'caducada')",
            name="ck_accion_estado_valido",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    conversacion_id = Column(
        Integer,
        ForeignKey("conversaciones.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    usuario_id = Column(
        Integer, ForeignKey("usuarios.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    tipo = Column(String(40), nullable=False)
    parametros = Column(Text, nullable=False)
    # El texto exacto que se le mostró a la persona al pedirle confirmación.
    # Se guarda para que la auditoría pueda responder «¿qué aprobó exactamente?».
    resumen = Column(Text, nullable=False)
    estado = Column(String(20), nullable=False, default="pendiente", index=True)
    creada_en = Column(DateTime, nullable=False, default=ahora_utc)
    # Una propuesta no confirmada caduca: confirmar mañana un «vendimos 20
    # arepas» de la semana pasada registraría el movimiento con la fecha de hoy.
    expira_en = Column(DateTime, nullable=False)
    resuelta_en = Column(DateTime, nullable=True)

    conversacion = relationship("Conversacion")
    usuario = relationship("Usuario", lazy="joined")


class Producto(Base):
    """Catálogo cerrado de productos.

    Sin esto, `producto` era texto libre y cada variante creaba un saldo
    separado: «AREPA» y «AREPAS» eran dos productos distintos, así que el stock
    se fragmentaba en silencio y `GET /api/stock` dejaba de responder la
    pregunta para la que existe.

    No se aplica una CHECK de `nombre = UPPER(nombre)`: la collation por defecto
    de MySQL 8 (utf8mb4_0900_ai_ci) es insensible a mayúsculas, así que esa
    comparación sería siempre cierta y la constraint no garantizaría nada. La
    forma canónica la impone `normalizar_nombre()` en la capa de aplicación.
    Como efecto útil, esa misma collation hace que el UNIQUE de abajo rechace
    'arepas' cuando ya existe 'AREPAS'.
    """

    __tablename__ = "productos"
    __table_args__ = (
        CheckConstraint("nombre <> ''", name="ck_producto_nombre_no_vacio"),
    )

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(LONGITUD_MAX_PRODUCTO), nullable=False, unique=True, index=True)
    # Baja lógica en lugar de borrado: un producto que se deja de vender tiene
    # movimientos históricos que deben seguir existiendo y sumando en el stock.
    activo = Column(Boolean, nullable=False, default=True, server_default="1")
    creado_en = Column(DateTime, nullable=False, default=ahora_utc)

    alias = relationship(
        "AliasProducto",
        back_populates="producto",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class AliasProducto(Base):
    """Formas alternativas que resuelven a un mismo producto canónico.

    Es la pieza que hace usable un catálogo cerrado en un sistema por voz. Sin
    alias, decir «llegaron 20 arepas» fallaría si el canónico es «AREPA», y la
    persona tendría que adivinar la forma exacta registrada. Con alias, cada
    rechazo se enseña una sola vez (`POST /api/productos/{id}/alias`) y queda
    aprendido.

    `alias` es único a nivel global, no por producto: si el mismo alias apuntara
    a dos productos, la resolución sería ambigua y habría que elegir en silencio.
    """

    __tablename__ = "alias_producto"
    __table_args__ = (
        CheckConstraint("alias <> ''", name="ck_alias_no_vacio"),
    )

    id = Column(Integer, primary_key=True, index=True)
    alias = Column(String(LONGITUD_MAX_PRODUCTO), nullable=False, unique=True, index=True)
    # CASCADE: un alias no tiene sentido sin su producto. Contrasta con los
    # movimientos, que usan RESTRICT porque sí deben sobrevivir.
    producto_id = Column(
        Integer,
        ForeignKey("productos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    creado_en = Column(DateTime, nullable=False, default=ahora_utc)

    producto = relationship("Producto", back_populates="alias")


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
    # Texto denormalizado a propósito: es un snapshot histórico. Si el catálogo
    # renombra un producto, lo que se registró en su día no debe mutar. La
    # verdad referencial la lleva producto_id; esta columna cuenta qué se
    # escribió entonces, y evita un JOIN en las consultas de stock e histórico.
    producto = Column(String(LONGITUD_MAX_PRODUCTO), nullable=False, index=True)
    # RESTRICT, no CASCADE: borrar un producto del catálogo nunca debe llevarse
    # por delante los movimientos que lo mencionan. Para retirar un producto
    # está `activo = False`.
    producto_id = Column(
        Integer,
        ForeignKey("productos.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    cantidad = Column(Integer, nullable=False)
    tipo_movimiento = Column(String(20), nullable=False)  # 'INGRESO' o 'EGRESO'
    fecha = Column(DateTime, nullable=False, default=ahora_utc, index=True)
    # Autor del movimiento. NULLABLE a propósito, por dos motivos que no van a
    # desaparecer: los movimientos anteriores a que existieran los usuarios no
    # tienen autor y no hay forma honesta de inventárselo, y la API key de
    # máquina (scripts, `curl`) no está asociada a ninguna persona. Un NULL aquí
    # significa «registrado sin sesión de usuario», que es información cierta;
    # rellenarlo con un usuario «sistema» falsearía la auditoría.
    usuario_id = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    usuario = relationship("Usuario", lazy="joined")

    @property
    def autor(self) -> "str | None":
        """Nombre de usuario de quien registró el movimiento, o None.

        Se expone como propiedad para que la API devuelva el nombre y no el
        objeto entero: hacia fuera, la auditoría solo necesita saber quién, no
        la ficha completa de la persona.
        """
        return self.usuario.usuario if self.usuario else None
