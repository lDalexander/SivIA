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
from models import Base, MovimientoInventario  # noqa: E402


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
    try:
        yield sesion
    finally:
        sesion.close()
        engine.dispose()


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
        async def falso_consultar_ollama(texto_usuario):
            if error is not None:
                raise error
            return respuesta

        monkeypatch.setattr(main, "consultar_ollama", falso_consultar_ollama)

    return configurar


@pytest.fixture
def crear_movimiento(sesion_db):
    """Inserta movimientos directamente, sin pasar por el LLM."""
    def factoria(producto, cantidad, tipo):
        mov = MovimientoInventario(
            producto=producto, cantidad=cantidad, tipo_movimiento=tipo
        )
        sesion_db.add(mov)
        sesion_db.commit()
        sesion_db.refresh(mov)
        return mov

    return factoria
