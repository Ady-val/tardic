"""GET /health — sin auth (lo pega el healthcheck de Docker, doc 03).

No tiene `X-API-Key`: si el healthcheck necesitara la key habría que
metérsela al propio contenedor, y es justo lo que este endpoint evita.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import Engine, text

from ..config import Settings
from ..db import get_engine
from ..schemas import HealthOut
from ..worker.main import HEARTBEAT_FILENAME
from .deps import get_settings_dep

router = APIRouter(tags=["health"])

# Coincide con [project].version de pyproject.toml (no lo edito: A/B/D lo
# leen igual). Si el paquete se versiona con `hatch`/`uv` en el futuro,
# esto se puede sacar de `importlib.metadata.version("tardic")`.
API_VERSION = "0.1.0"


def _worker_seen_seconds_ago(settings: Settings) -> float | None:
    """Segundos desde el último latido del worker, o None si nunca latió.

    El worker escribe `worker_heartbeat.txt` bajo `data_dir` de forma atómica.
    Se lee el CONTENIDO (marca ISO-8601 en UTC) y no el mtime del archivo,
    porque api y worker son contenedores distintos sobre el mismo volumen y el
    mtime depende del reloj del sistema de archivos; la marca que el worker
    escribe es la que él mismo considera su último latido.

    Que devuelva None o un número grande NO tumba el healthcheck de la API: la
    API está sana aunque el worker esté caído. Sirve para diagnosticar, que es
    justo lo que hoy obliga a entrar por SSH a mirar `docker compose ps`.
    """
    path = settings.data_dir / HEARTBEAT_FILENAME
    try:
        stamp = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max(0.0, round((datetime.now(UTC) - stamp).total_seconds(), 1))


@router.get("/health", response_model=HealthOut)
def health(
    engine: Annotated[Engine, Depends(get_engine)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> HealthOut:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        # El healthcheck no necesita saber por qué falló, solo que falló.
        database_ok = False

    return HealthOut(
        version=API_VERSION,
        database=database_ok,
        worker_seen_seconds_ago=_worker_seen_seconds_ago(settings),
    )
