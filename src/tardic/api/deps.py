"""Piezas compartidas por los endpoints: almacenamiento y auth.

La sesión de BD YA NO se arma aquí: `db.py` y `repository.py` (agente B)
llegaron mientras se escribía este módulo, así que las rutas usan
`tardic.db.get_session` / `tardic.db.get_engine` directamente — un solo
engine para toda la app, sin duplicar el pool que este módulo tenía antes
(ver el reporte final del agente C para el detalle del cambio).
"""
from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from ..config import Settings, get_settings
from ..storage import Storage


def get_settings_dep() -> Settings:
    """Punto único de inyección de settings — sobreescribible en tests con
    `app.dependency_overrides[get_settings_dep] = ...`."""
    return get_settings()


def get_storage(settings: Annotated[Settings, Depends(get_settings_dep)]) -> Storage:
    return Storage(settings.data_dir)


def require_api_key(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Regla 4 de AGENTS.md: toda ruta salvo /health exige `X-API-Key`.

    Comparación con `secrets.compare_digest` para no filtrar la key por
    tiempo de respuesta. El valor de la key JAMÁS se loguea, ni siquiera en
    el mensaje de error.

    Se compara sobre BYTES, no sobre str: `compare_digest` lanza `TypeError`
    con cadenas que traen caracteres no-ASCII, y un header `X-API-Key: ñ`
    devolvía 500 en vez de 401 — un intento fallido de autenticación no debe
    verse distinto de otro según lo que mande el atacante.
    """
    if not x_api_key or not secrets.compare_digest(
        x_api_key.encode("utf-8", "surrogateescape"),
        settings.api_key.encode("utf-8", "surrogateescape"),
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"detail": "no autorizado", "code": "unauthorized"},
        )
