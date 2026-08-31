"""App de FastAPI. `uvicorn tardic.api.main:app`.

Aquí vive lo transversal: request-id (regla 9 de AGENTS.md), forma única de
error (`ErrorOut`, regla 7) y logging con el request-id ya inyectado.
"""
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..schemas import ErrorOut
from .routes_health import router as health_router
from .routes_recordings import router as recordings_router

# ContextVar en vez de pasar el request_id a mano por cada log: cualquier
# `logging.getLogger(...)` de este proceso lo recoge solo, vía el filter de
# abajo, sin acoplar el resto del código al request actual.
_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class _RequestIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_ctx.get()
        return True


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [rid=%(request_id)s] %(name)s: %(message)s")
    )
    handler.addFilter(_RequestIdLogFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


_configure_logging()
logger = logging.getLogger("tardic.api")

app = FastAPI(title="Tardic API", version="0.1.0")


@app.middleware("http")
async def reject_unauthenticated_bodies(request: Request, call_next):
    """Rechaza una subida sin credencial ANTES de leer un solo byte del cuerpo.

    Sin esto, Starlette parsea el multipart y lo vuelca a un archivo temporal
    (spill a disco pasado 1 MB) antes de que la dependencia de auth llegue a
    correr: una petición anónima de 200 MB escribía 200 MB en disco para
    después contestar 401. Con varias en paralelo se llena el disco del host
    — y ese disco lo comparten los demás contenedores de
    producción, así que el ataque no cuesta nada y tumba lo de al lado.

    Aquí solo se mira el header. La comparación real de la key sigue estando
    en `require_api_key`: esto es una puerta que ahorra el trabajo caro, no la
    autenticación.
    """
    if request.method in {"POST", "PUT", "PATCH"} and not request.headers.get("X-API-Key"):
        logger.warning(
            "subida rechazada sin credencial (%s %s, content-length=%s)",
            request.method, request.url.path, request.headers.get("Content-Length"),
        )
        # Mismo 401 que devolvería `require_api_key`: quien ataca no puede
        # distinguir si lo paró la puerta o la autenticación de verdad.
        return JSONResponse(
            status_code=401,
            content=ErrorOut(detail="no autorizado", code="unauthorized").model_dump(),
        )
    return await call_next(request)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Genera (o reusa) un request-id, lo mete en el log y en la respuesta.

    Atrapa cualquier excepción no manejada aquí mismo — y no con
    `@app.exception_handler(Exception)` — porque ese handler lo instala
    Starlette en `ServerErrorMiddleware`, que queda POR FUERA de este
    middleware, y entonces un 500 real saldría sin el header X-Request-ID.
    """
    rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    token = _request_id_ctx.set(rid)
    request.state.request_id = rid
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("error no manejado procesando %s %s", request.method, request.url.path)
        response = JSONResponse(
            status_code=500,
            content=ErrorOut(
                detail="error interno del servidor", code="internal_error"
            ).model_dump(),
        )
    finally:
        _request_id_ctx.reset(token)
    response.headers["X-Request-ID"] = rid
    return response


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Normaliza cualquier HTTPException a la forma de `ErrorOut` (regla 7).

    Las rutas levantan `HTTPException(status, detail={"detail": ..., "code":
    ...})`; si algo levanta el `detail` como string plano (p. ej. errores
    que arma FastAPI internamente) también se envuelve igual.
    """
    if isinstance(exc.detail, dict) and "detail" in exc.detail:
        payload = ErrorOut(detail=str(exc.detail["detail"]), code=exc.detail.get("code"))
    else:
        payload = ErrorOut(detail=str(exc.detail))
    return JSONResponse(status_code=exc.status_code, content=payload.model_dump())


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 con forma de `ErrorOut` — p. ej. un `{recording_id}` que no es UUID."""
    errors = exc.errors()
    detail = "parámetros de la solicitud inválidos"
    if errors:
        first = errors[0]
        loc = ".".join(str(part) for part in first.get("loc", []) if part != "body")
        detail = f"parámetro inválido en {loc}: {first.get('msg')}" if loc else first.get(
            "msg", detail
        )
    return JSONResponse(
        status_code=422,
        content=ErrorOut(detail=detail, code="validation_error").model_dump(),
    )


app.include_router(health_router)
app.include_router(recordings_router, prefix="/v1")
