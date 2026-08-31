"""Engine y sesiones de SQLAlchemy (síncrono, con psycopg 3).

Única fuente de verdad para conectarse a Postgres: API y worker importan de
aquí, nadie construye su propio engine. Nada de lógica de negocio — eso vive
en `repository.py`.
"""
from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings


def make_engine(database_url: str | None = None) -> Engine:
    """Crea un engine nuevo. Con `database_url=None` usa `Settings`.

    Aparte del uso normal (API/worker sobre la BD de `Settings`), esto deja
    a los tests construir su propio engine contra la BD de prueba sin tocar
    variables de entorno ni pelearse con el `lru_cache` de `get_settings`.
    """
    url = database_url or get_settings().sqlalchemy_url
    # pool_pre_ping: una sesión de worker puede quedarse horas viva durante
    # una transcripción larga; sin esto, una conexión caída por timeout del
    # lado de Postgres se descubre hasta el primer INSERT que falla.
    return create_engine(url, pool_pre_ping=True, future=True)


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: el worker sigue leyendo atributos (p. ej.
    # `job.id`) después de hacer commit, en transacciones cortas separadas;
    # sin esto cada acceso dispararía una consulta nueva o un DetachedError.
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


# --- instancia por defecto, perezosa a propósito ---
# `get_settings()` exige `TARDIC_API_KEY` (entre otras). Si `engine` se
# construyera al importar este módulo, cualquier test que solo necesite
# `repository.py` o `worker/queue.py` (que reciben la `Session` ya armada,
# nunca leen `Settings`) tendría que configurar el entorno completo de la app
# solo para poder importar `tardic.db`. Con `lru_cache` la validación de
# `Settings` se paga la primera vez que de verdad se pide una conexión real
# (API o `worker/main.py`), no al importar el módulo.
@lru_cache
def get_engine() -> Engine:
    return make_engine()


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return make_sessionmaker(get_engine())


@contextmanager
def session_scope(session_factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Transacción corta con commit/rollback automático.

    Uso: `with session_scope() as session: ...`. El worker abre y cierra
    muchas de estas a propósito — nunca deja una transacción abierta durante
    los 90 minutos que puede tardar una transcripción.
    """
    session = (session_factory or get_sessionmaker())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Generator[Session, None, None]:
    """Generador para `Depends(get_session)` en FastAPI.

    A diferencia de `session_scope`, aquí el caller decide cuándo hace commit
    (o lo hace un middleware); este generador solo garantiza que la sesión se
    cierre al final de la petición.
    """
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
