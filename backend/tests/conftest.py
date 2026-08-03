"""
Configuración compartida de la suite de tests de SivIA.

Aísla las dos dependencias externas que harían los tests lentos e inestables:

  - **Ollama**: se sustituye por un doble. No se prueba que Qwen funcione (no es
    nuestro código), sino que la API reaccione correctamente a lo que Qwen
    devuelva, incluidas las respuestas defectuosas que son difíciles de provocar
    a voluntad con el modelo real.
  - **MySQL**: se sustituye por SQLite en memoria, con una base limpia por test
    para que no se contaminen entre sí ni dependan del orden de ejecución.
"""
import os
from datetime import timedelta

import pytest

# Las variables de entorno se fijan ANTES de importar main: el módulo aborta el
# arranque si faltan DATABASE_URL o SIVIA_API_KEY (comportamiento deliberado en
# producción), así que sin esto ni siquiera se podría importar.
API_KEY_PRUEBA = "clave-de-prueba-no-usar-en-produccion"
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SIVIA_API_KEY", API_KEY_PRUEBA)
os.environ.setdefault("CORS_ORIGINS", "*")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import main  # noqa: E402
from models import (  # noqa: E402
    AliasProducto,
    Base,
    MovimientoInventario,
    Producto,
    Sesion,
    Usuario,
    ahora_utc,
    hashear_clave,
    normalizar_nombre,
)

# Catálogo sembrado en cada base de test. Existe porque
# POST /api/procesar-comando rechaza con 409 si el catálogo está vacío: sin esta
# siembra, todos los tests del endpoint comprobarían esa única condición.
#
# Sembrar productos NO altera lo que devuelven /api/stock ni /api/movimientos:
# ambos se derivan de los movimientos, no del catálogo. Por eso los tests que ya
# existían siguen midiendo exactamente lo mismo que antes.
CATALOGO_DE_PRUEBA = {
    "AREPAS": ["AREPA", "AREPITAS"],
    "EMPANADAS": ["EMPANADA"],
    "QUESO": [],
    "PAN": [],
    "PANES DE YUCA": [],
}

# Usuarios sembrados en cada base de test, con los dos roles representados.
# Se siembran siempre, igual que el catálogo: no altera lo que miden los tests
# anteriores (ninguno consulta la tabla de usuarios) y evita que cada test del
# chat tenga que crearse el suyo.
CLAVE_DE_PRUEBA = "clave-de-prueba-1234"
USUARIOS_DE_PRUEBA = {
    "john": ("John García", "admin"),
    "martha": ("Martha Restrepo", "operador"),
}


_HASH_DE_PRUEBA = hashear_clave(CLAVE_DE_PRUEBA)


@pytest.fixture
def sesion_db():
    """Base SQLite en memoria, nueva y vacía para cada test.

    StaticPool y check_same_thread=False son necesarios porque el endpoint de
    escritura delega el commit al threadpool (run_in_threadpool): sin ellos, la
    escritura ocurriría en otra conexión y el test no vería la fila.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # create_all es correcto aquí: base efímera de test, no gestionada por
    # Alembic. En producción el esquema lo aplica scripts/migrar.sh.
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    sesion = Session()
    for nombre, alias in CATALOGO_DE_PRUEBA.items():
        sesion.add(
            Producto(
                nombre=nombre,
                alias=[AliasProducto(alias=a) for a in alias],
            )
        )
    # El hash se calcula una sola vez por proceso y no por usuario y test:
    # scrypt cuesta ~50 ms a propósito, y multiplicarlo por cada test añadiría
    # segundos a una suite que hoy tarda menos de dos.
    for usuario, (nombre_completo, rol) in USUARIOS_DE_PRUEBA.items():
        sesion.add(
            Usuario(
                usuario=usuario,
                nombre=nombre_completo,
                hash_clave=_HASH_DE_PRUEBA,
                rol=rol,
            )
        )
    sesion.commit()
    try:
        yield sesion
    finally:
        sesion.close()
        engine.dispose()


@pytest.fixture
def sesion_sin_catalogo():
    """Base de test con el esquema pero con el catálogo vacío.

    Para el caso de una instalación recién migrada, donde procesar un comando
    debe fallar de forma explicativa en lugar de gastar la inferencia y después
    no tener contra qué validar.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    sesion = Session()
    try:
        yield sesion
    finally:
        sesion.close()
        engine.dispose()


@pytest.fixture
def cliente_sin_catalogo(sesion_sin_catalogo):
    """Cliente HTTP contra una base sin ningún producto dado de alta."""
    def get_db_de_prueba():
        try:
            yield sesion_sin_catalogo
        finally:
            pass

    main.app.dependency_overrides[main.get_db] = get_db_de_prueba
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def cliente(sesion_db):
    """Cliente HTTP contra la app real, con la base de datos sustituida."""
    def get_db_de_prueba():
        try:
            yield sesion_db
        finally:
            pass  # El cierre lo gestiona la fixture sesion_db.

    main.app.dependency_overrides[main.get_db] = get_db_de_prueba
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def auth():
    """Header de autenticación válido."""
    return {"X-API-Key": API_KEY_PRUEBA}


@pytest.fixture
def simular_llm(monkeypatch):
    """Sustituye la llamada a Ollama por una respuesta fija o una excepción.

    Uso:
        simular_llm({"producto": "AREPAS", "cantidad": 45, "tipo_movimiento": "INGRESO"})
        simular_llm(error=HTTPException(status_code=503, detail="..."))
    """
    def configurar(respuesta=None, error=None):
        # La firma acepta el catálogo porque la aplicación lo inyecta en el
        # prompt de sistema. El doble lo ignora: lo que se prueba aquí es cómo
        # reacciona la API a la respuesta del modelo, no cómo se construye el
        # prompt (eso tiene sus propios tests, sin red).
        async def falso_consultar_ollama(texto_usuario, nombres_catalogo=None):
            if error is not None:
                raise error
            return respuesta

        monkeypatch.setattr(main, "consultar_ollama", falso_consultar_ollama)

    return configurar


@pytest.fixture
def crear_movimiento(sesion_db):
    """Inserta movimientos directamente, sin pasar por el LLM.

    Da de alta el producto en el catálogo si hace falta: `producto_id` es NOT
    NULL con clave ajena, así que ya no se puede insertar un movimiento de un
    producto que no existe — que es justamente lo que la FK viene a garantizar.
    """
    def factoria(producto, cantidad, tipo):
        nombre = normalizar_nombre(producto)
        fila = sesion_db.query(Producto).filter(Producto.nombre == nombre).one_or_none()
        if fila is None:
            fila = Producto(nombre=nombre)
            sesion_db.add(fila)
            sesion_db.commit()
            sesion_db.refresh(fila)

        mov = MovimientoInventario(
            producto=fila.nombre,
            producto_id=fila.id,
            cantidad=cantidad,
            tipo_movimiento=tipo,
        )
        sesion_db.add(mov)
        sesion_db.commit()
        sesion_db.refresh(mov)
        return mov

    return factoria


@pytest.fixture
def id_de(sesion_db):
    """Id de un producto por su nombre.

    Los tests no deben escribir el id a mano: dependería del orden de inserción
    de CATALOGO_DE_PRUEBA, y añadir un producto al principio de ese diccionario
    rompería tests que no tienen nada que ver.
    """
    def buscar(nombre):
        return sesion_db.query(Producto).filter(
            Producto.nombre == normalizar_nombre(nombre)
        ).one().id

    return buscar


@pytest.fixture
def token_de(sesion_db):
    """Header 'Authorization: Bearer' de una sesión válida del usuario indicado.

    Crea la fila de sesión directamente y no llamando a `/api/auth/login`: el
    login tiene sus propios tests, y hacer que cada test del chat pase por él
    encadenaría fallos —un error en el login rompería treinta tests que no van
    de eso— además de pagar un scrypt por test.
    """
    def factoria(usuario="john", horas=12):
        fila = sesion_db.query(Usuario).filter(Usuario.usuario == usuario).one()
        token = f"token-de-prueba-{usuario}-{horas}"
        sesion_db.add(
            Sesion(
                token_hash=main._hash_token(token),
                usuario_id=fila.id,
                expira_en=ahora_utc() + timedelta(hours=horas),
            )
        )
        sesion_db.commit()
        return {"Authorization": f"Bearer {token}"}

    return factoria


@pytest.fixture
def auth_admin(token_de):
    """Sesión de John, administrador: puede eliminar y retirar."""
    return token_de("john")


@pytest.fixture
def auth_operador(token_de):
    """Sesión de Martha, operadora: registra y consulta, pero no elimina."""
    return token_de("martha")


@pytest.fixture
def usuario_db(sesion_db):
    """La fila de Usuario, para comprobar autoría y estado desde los tests."""
    def buscar(usuario="john"):
        return sesion_db.query(Usuario).filter(Usuario.usuario == usuario).one()

    return buscar


@pytest.fixture
def simular_chat(monkeypatch):
    """Sustituye el turno de conversación con Ollama por un JSON fijo.

    Es el doble equivalente a `simular_llm` pero para `consultar_ollama_chat`.
    Lo que se prueba con él es qué hace el backend con lo que el modelo entienda
    —incluido cuando entiende mal—, no la calidad de esa interpretación.

    Uso:
        simular_chat({"intencion": "registrar_movimiento", "producto": "AREPAS",
                      "cantidad": 20, "tipo_movimiento": "INGRESO"})
    """
    def configurar(respuesta=None, error=None):
        async def falso_chat(system, prompt):
            if error is not None:
                raise error
            return respuesta

        monkeypatch.setattr(main, "consultar_ollama_chat", falso_chat)

    return configurar


@pytest.fixture
def crear_producto(sesion_db):
    """Da de alta un producto con sus alias, sin pasar por la API."""
    def factoria(nombre, alias=()):
        fila = Producto(
            nombre=normalizar_nombre(nombre),
            alias=[AliasProducto(alias=normalizar_nombre(a)) for a in alias],
        )
        sesion_db.add(fila)
        sesion_db.commit()
        sesion_db.refresh(fila)
        return fila

    return factoria
