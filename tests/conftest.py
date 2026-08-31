"""Fixtures compartidas de la API (agente C).

Nota para quien también escriba tests aquí (doc: varios agentes trabajan en
paralelo): estos nombres son públicos a propósito — `client`, `db_session`,
`test_settings`, `auth_headers`, `synth_wav_bytes` — para no duplicar setup.

Sobre la BD: uso Postgres real en Docker, no SQLite. Lo probé: `models.py`
usa `JSONB` de `sqlalchemy.dialects.postgresql` en `ProcessingJob.progress`,
y `Base.metadata.create_all()` truena contra un engine de SQLite con
`CompileError: ... can't render element of type JSONB`. Como no me toca
tocar `models.py`, todos los tests que usan la BD llevan `@pytest.mark.db`
y requieren un Postgres real.

Mismo contrato que `tests/test_queue.py` y `tests/test_worker.py` (agente
B), para que toda la suite `db` comparta una sola Postgres en vez de pelearse
por el puerto:

    docker run -d --rm -p 55432:5432 -e POSTGRES_PASSWORD=test \\
        -e POSTGRES_USER=tardic -e POSTGRES_DB=tardic_test \\
        --name tardic-test-db postgres:16-alpine

Si ya está corriendo (por ejemplo, porque el/la dev la levantó a mano antes
de correr toda la suite), se reusa tal cual. Si no, y hay Docker disponible,
este `conftest` la levanta ella misma con el mismo nombre/puerto/
credenciales y la tira al terminar la sesión. `TARDIC_TEST_DATABASE_URL` en
el entorno pisa todo lo anterior (pensado para CI). Si nada de esto aplica,
los tests `db` se saltan (no fallan) con un mensaje claro.
"""
from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from tardic import db as tardic_db
from tardic.api.deps import get_settings_dep
from tardic.config import Settings
from tardic.models import Base

_TEST_DB_URL = "postgresql+psycopg://tardic:test@127.0.0.1:55432/tardic_test"
_TEST_DB_CONTAINER = "tardic-test-db"


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "version"], capture_output=True, timeout=10, check=True)
    except Exception:
        return False
    return True


def _can_connect(url: str, timeout: float = 2.0) -> bool:
    import psycopg
    from sqlalchemy.engine import make_url

    u = make_url(url)
    try:
        with psycopg.connect(
            host=u.host, port=u.port, user=u.username, password=u.password,
            dbname=u.database, connect_timeout=timeout,
        ):
            return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    env_url = os.environ.get("TARDIC_TEST_DATABASE_URL")
    if env_url:
        yield env_url
        return

    if _can_connect(_TEST_DB_URL):
        # Alguien (dev o `test_queue.py`/`test_worker.py` en la misma
        # corrida) ya la dejó lista: no se levanta una segunda.
        yield _TEST_DB_URL
        return

    if not _docker_available():
        pytest.skip(
            "no hay Postgres de pruebas en localhost:55432 ni Docker disponible para "
            "levantarla; define TARDIC_TEST_DATABASE_URL o corre el `docker run` del "
            "docstring de este módulo para los tests @pytest.mark.db"
        )

    subprocess.run(["docker", "rm", "-f", _TEST_DB_CONTAINER], capture_output=True)
    run = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", _TEST_DB_CONTAINER,
            "-e", "POSTGRES_USER=tardic",
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_DB=tardic_test",
            "-p", "127.0.0.1:55432:5432",
            "postgres:16-alpine",
        ],
        capture_output=True, text=True,
    )
    if run.returncode != 0:
        pytest.skip(f"no se pudo levantar {_TEST_DB_CONTAINER} en Docker: {run.stderr.strip()}")

    try:
        deadline = time.monotonic() + 30
        ready = False
        while time.monotonic() < deadline:
            if _can_connect(_TEST_DB_URL):
                ready = True
                break
            time.sleep(0.5)
        if not ready:
            pytest.skip("Postgres de prueba no respondió a tiempo en localhost:55432")

        yield _TEST_DB_URL
    finally:
        subprocess.run(["docker", "rm", "-f", _TEST_DB_CONTAINER], capture_output=True)


@pytest.fixture(scope="session")
def db_engine(postgres_url: str) -> Iterator[Engine]:
    engine = create_engine(postgres_url, future=True)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def _reset_db(db_engine: Engine) -> None:
    """Aísla cada test: trunca en vez de recrear tablas (más rápido)."""
    with db_engine.begin() as conn:
        conn.execute(
            text(
                'TRUNCATE TABLE "processing_jobs", "segments", "transcripts", '
                '"speakers", "recordings" RESTART IDENTITY CASCADE'
            )
        )


@pytest.fixture()
def db_session(db_engine: Engine, _reset_db: None) -> Iterator[Session]:
    """Sesión de BD de uso libre en los tests, para dejar a mano el estado
    que en producción pondría el worker (p. ej. mover un Recording a
    COMPLETED)."""
    factory = tardic_db.make_sessionmaker(db_engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def test_settings(tmp_path: Path, postgres_url: str) -> Settings:
    return Settings(
        api_key="test-api-key-0123456789",
        database_url=postgres_url,
        data_dir=tmp_path / "data",
    )


@pytest.fixture()
def app(test_settings: Settings, db_engine: Engine, _reset_db: None):
    """La API ya no arma su propia sesión: usa `tardic.db.get_engine` /
    `tardic.db.get_session` (agente B), así que aquí se sobreescriben esos
    dos —no algo mío— para que las rutas usen `db_engine` (Postgres de
    prueba, tablas ya creadas) en vez del `Settings()` real del proceso."""
    from tardic.api.main import app as fastapi_app

    factory = tardic_db.make_sessionmaker(db_engine)

    def _override_settings() -> Settings:
        return test_settings

    def _override_get_engine() -> Engine:
        return db_engine

    def _override_get_session() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_settings_dep] = _override_settings
    fastapi_app.dependency_overrides[tardic_db.get_engine] = _override_get_engine
    fastapi_app.dependency_overrides[tardic_db.get_session] = _override_get_session
    try:
        yield fastapi_app
    finally:
        fastapi_app.dependency_overrides.clear()


@pytest.fixture()
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_headers(test_settings: Settings) -> dict[str, str]:
    return {"X-API-Key": test_settings.api_key}


@pytest.fixture(scope="session")
def synth_wav_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Un wav sintético de ~1s generado con ffmpeg — nunca audio real (regla
    2 de AGENTS.md)."""
    d = tmp_path_factory.mktemp("audio")
    path = d / "synth.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-ar", "16000", "-ac", "1", str(path),
        ],
        capture_output=True, check=True,
    )
    return path


@pytest.fixture()
def synth_wav_bytes(synth_wav_path: Path) -> bytes:
    return synth_wav_path.read_bytes()


@pytest.fixture()
def non_audio_bytes() -> bytes:
    return b"esto no es un archivo de audio, es texto plano.\n" * 200
