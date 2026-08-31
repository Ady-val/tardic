"""Endpoints de `/v1/recordings`.

Reparto (AGENTS.md): el agente C (yo) solo toca `src/tardic/api/`. El acceso
a BD pasa por `tardic.db` (sesión/engine) y `tardic.repository` (queries) —
ambos del agente B, que llegaron mientras se escribía este módulo. La única
excepción es el POST: `repository.create_recording_with_job` genera su
propio UUID de `Recording` recién al insertar, pero la API necesita el UUID
ANTES de tocar disco (regla 1: el archivo se guarda en
`audio/<recording_id>/...`, y ese nombre tiene que existir antes de que
haya fila en BD). Por eso el POST sigue construyendo `Recording` +
`ProcessingJob` a mano. Se reporta como hueco de contrato: lo limpio sería
que `create_recording_with_job` aceptara un `recording_id` opcional.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import repository
from ..config import Settings
from ..db import get_session
from ..models import ProcessingJob, Recording, RecordingStatus
from ..schemas import (
    Progress,
    RecordingCreated,
    RecordingList,
    RecordingListItem,
    RecordingStatusOut,
    SegmentOut,
    TranscriptOut,
)
from ..storage import Storage, safe_display_name, safe_suffix
from .deps import get_settings_dep, get_storage, require_api_key

logger = logging.getLogger("tardic.api.recordings")

router = APIRouter(
    prefix="/recordings", tags=["recordings"], dependencies=[Depends(require_api_key)]
)

# 1 MiB: ni el archivo completo en RAM (regla 5 de AGENTS.md) ni un read() por byte.
_CHUNK_SIZE = 1024 * 1024
_UNSAFE_HEADER_CHARS = re.compile(r'[\r\n"]')


# --------------------------------------------------------------------------
# Errores comunes
# --------------------------------------------------------------------------
class _UploadTooLarge(Exception):
    pass


def _not_found() -> HTTPException:
    # Regla: los 404 no filtran información. No decimos "existe pero..." ni nada
    # que distinga "no existe" de "no es tuyo" (no hay ownership en el MVP, pero
    # el hábito es el correcto).
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"detail": "grabación no encontrada", "code": "not_found"},
    )


def _not_ready(current_status: RecordingStatus) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "detail": (
                "la transcripción todavía no está lista "
                f"(estado actual: {current_status.value})"
            ),
            "code": "not_ready",
        },
    )


def _internal_error(log_message: str, recording_id: uuid.UUID) -> HTTPException:
    logger.error("%s (recording_id=%s)", log_message, recording_id)
    return HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"detail": "error interno del servidor", "code": "internal_error"},
    )


# --------------------------------------------------------------------------
# Subida a disco y validación de audio
# --------------------------------------------------------------------------
def _stream_to_disk(src, dest: Path, max_bytes: int) -> int:
    """Copia `src` (file-like, `.read(n)`) a `dest` por trozos, contando
    bytes. Aborta y borra lo escrito en cuanto se pasa de `max_bytes` — sin
    confiar en Content-Length, que el cliente puede mentir (regla 1)."""
    written = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = src.read(_CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise _UploadTooLarge
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return written


def _probe_audio(path: Path) -> tuple[bool, float | None]:
    """Verifica con ffprobe que el archivo ya guardado tenga una pista de
    audio de verdad (regla 3): nunca por extensión ni por el content-type
    que declaró el cliente."""
    ffprobe_bin = shutil.which("ffprobe")
    if ffprobe_bin is None:
        logger.error("ffprobe no está instalado en el PATH del contenedor/host")
        return False, None
    try:
        # Lista de argumentos fija (nada de shell=True, nada armado con
        # input del cliente salvo la ruta ya resuelta en disco): S603 es un
        # falso positivo aquí.
        result = subprocess.run(  # noqa: S603
            [
                ffprobe_bin, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("ffprobe no se pudo ejecutar: %s", exc)
        return False, None

    if result.returncode != 0:
        return False, None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, None

    streams = data.get("streams", [])
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    duration: float | None = None
    fmt_duration = data.get("format", {}).get("duration")
    if fmt_duration is not None:
        try:
            duration = float(fmt_duration)
        except (TypeError, ValueError):
            duration = None

    return has_audio, duration


def _safe_download_name(original_filename: str) -> str:
    """Nombre para el header Content-Disposition. No toca el disco (regla 2
    del filename, storage.py) pero igual se limpia para no meter CR/LF/comillas
    en un header HTTP."""
    stem = Path(original_filename).stem or "transcript"
    stem = _UNSAFE_HEADER_CHARS.sub("", stem)[:100] or "transcript"
    return f"{stem}.txt"


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@router.post("", response_model=RecordingCreated, status_code=status.HTTP_201_CREATED)
def create_recording(
    db: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    storage: Annotated[Storage, Depends(get_storage)],
    file: UploadFile = File(...),  # noqa: B008 — patrón estándar de FastAPI para multipart
    diarize: bool = Form(False),
) -> RecordingCreated:
    # La diarización (saber quién habló) está medida y su código existe, pero el
    # worker todavía NO la ejecuta. Aceptar `diarize=true` en silencio dejaría a
    # quien la pide esperando hablantes que nunca van a llegar, así que se
    # rechaza de frente. El campo se conserva en el job para el día que entre.
    if diarize:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "detail": (
                    "la diarización todavía no está disponible: la transcripción "
                    "se entrega sin separar hablantes. Vuelve a enviar sin diarize."
                ),
                "code": "diarization_not_implemented",
            },
        )

    recording_id = uuid.uuid4()
    suffix = safe_suffix(file.filename)
    storage.ensure_dir(recording_id)
    dest = storage.original_path(recording_id, suffix)

    try:
        written = _stream_to_disk(file.file, dest, settings.max_upload_bytes)
    except _UploadTooLarge:
        storage.delete_recording(recording_id)
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "detail": "el archivo excede el tamaño máximo permitido",
                "code": "upload_too_large",
            },
        ) from None
    finally:
        file.file.close()

    if written == 0:
        storage.delete_recording(recording_id)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"detail": "el archivo está vacío", "code": "invalid_audio"},
        )

    has_audio, duration = _probe_audio(dest)
    if not has_audio:
        storage.delete_recording(recording_id)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "detail": "el archivo no contiene una pista de audio válida",
                "code": "invalid_audio",
            },
        )

    # Un WAV válido sin una sola muestra son 44 bytes de cabecera: pasa el
    # `written == 0` y pasa `_probe_audio` (sí tiene pista de audio, solo que
    # vacía). Sin este corte, se encolaba y reventaba noventa segundos después,
    # tras gastar los tres reintentos, con un "no se pudo transcribir" que no
    # explica nada. Aquí la duración ya está calculada: se rechaza de inmediato.
    if duration is None or duration <= 0:
        storage.delete_recording(recording_id)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "detail": "el audio no tiene duración: está vacío o corrupto",
                "code": "invalid_audio",
            },
        )

    try:
        recording = Recording(
            id=recording_id,
            filename=safe_display_name(file.filename),
            mime_type=file.content_type,
            size_bytes=written,
            storage_path=storage.relative(dest),
            status=RecordingStatus.QUEUED,
            duration_seconds=duration,
        )
        db.add(recording)
        db.flush()
        # RF: un job de transcripción por grabación; el worker lo toma con
        # SELECT ... FOR UPDATE SKIP LOCKED sobre esta misma tabla.
        db.add(ProcessingJob(recording_id=recording.id, job_type="transcribe", diarize=diarize))
        db.commit()
        db.refresh(recording)
    except Exception:
        db.rollback()
        storage.delete_recording(recording_id)
        raise

    return RecordingCreated.model_validate(recording)


@router.get("/{recording_id}", response_model=RecordingStatusOut, name="get_recording_status")
def get_recording_status(
    recording_id: uuid.UUID,
    request: Request,
    db: Annotated[Session, Depends(get_session)],
) -> RecordingStatusOut:
    recording = repository.get_recording(db, recording_id)
    if recording is None:
        raise _not_found()

    job = repository.get_latest_job(db, recording_id)
    progress = Progress.model_validate(job.progress) if job and job.progress else Progress()
    stage = job.stage if job else None
    attempts = job.attempts if job else 0

    transcript_url: str | None = None
    transcript_txt_url: str | None = None
    warning: str | None = None
    if recording.status == RecordingStatus.COMPLETED:
        transcript_url = str(request.url_for("get_transcript", recording_id=recording.id))
        transcript_txt_url = str(
            request.url_for("download_transcript_txt", recording_id=recording.id)
        )
        # Terminó bien, pero sin una sola palabra: pasa cuando el detector de
        # voz no encuentra habla (una grabación de puro ruido o de silencio).
        # No es un fallo, pero callarlo deja al usuario con un archivo vacío
        # sin saber si el sistema se rompió.
        if not (recording.transcript and recording.transcript.text.strip()):
            warning = "no se detectó voz en la grabación: la transcripción quedó vacía"

    error = recording.processing_error if recording.status == RecordingStatus.FAILED else None

    return RecordingStatusOut(
        id=recording.id,
        status=recording.status,
        stage=stage,
        progress=progress,
        filename=recording.filename,
        duration_seconds=recording.duration_seconds,
        language=recording.language,
        created_at=recording.created_at,
        attempts=attempts,
        error=error,
        warning=warning,
        transcript_url=transcript_url,
        transcript_txt_url=transcript_txt_url,
    )


@router.get("/{recording_id}/transcript", response_model=TranscriptOut, name="get_transcript")
def get_transcript(
    recording_id: uuid.UUID,
    db: Annotated[Session, Depends(get_session)],
) -> TranscriptOut:
    recording = repository.get_recording(db, recording_id)
    if recording is None:
        raise _not_found()
    if recording.status != RecordingStatus.COMPLETED:
        raise _not_ready(recording.status)
    transcript = repository.get_transcript(db, recording_id)
    if transcript is None:
        raise _internal_error("recording COMPLETED sin fila de transcript en BD", recording_id)

    segments = [
        SegmentOut(
            start_time=s.start_time,
            end_time=s.end_time,
            text=s.text,
            speaker=s.speaker.label if s.speaker else None,
            confidence=s.confidence,
        )
        for s in transcript.segments
    ]
    return TranscriptOut(
        recording_id=recording.id,
        text=transcript.text,
        language=transcript.language,
        model=transcript.model,
        duration_seconds=recording.duration_seconds,
        processing_time_seconds=transcript.processing_time_seconds,
        created_at=transcript.created_at,
        segments=segments,
    )


@router.get("/{recording_id}/transcript.txt", name="download_transcript_txt")
def download_transcript_txt(
    recording_id: uuid.UUID,
    db: Annotated[Session, Depends(get_session)],
    storage: Annotated[Storage, Depends(get_storage)],
) -> FileResponse:
    recording = repository.get_recording(db, recording_id)
    if recording is None:
        raise _not_found()
    if recording.status != RecordingStatus.COMPLETED:
        raise _not_ready(recording.status)

    path = storage.transcript_txt_path(recording.id)
    if not path.exists():
        raise _internal_error(
            "transcript.txt ausente en disco pese a estar COMPLETED", recording_id
        )

    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        filename=_safe_download_name(recording.filename),
    )


@router.get("", response_model=RecordingList)
def list_recordings(
    db: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RecordingList:
    items, total = repository.list_recordings(db, limit=limit, offset=offset)
    return RecordingList(
        items=[RecordingListItem.model_validate(r) for r in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.delete("/{recording_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recording(
    recording_id: uuid.UUID,
    db: Annotated[Session, Depends(get_session)],
    storage: Annotated[Storage, Depends(get_storage)],
) -> Response:
    found = repository.delete_recording(db, recording_id)
    if not found:
        raise _not_found()
    # `repository.delete_recording` ya quitó la fila (y en cascada transcript/
    # segments/speakers/jobs); el archivo en disco es aparte, nunca lo toca
    # `repository.py` (no hace I/O de archivos, según su propio docstring).
    storage.delete_recording(recording_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
