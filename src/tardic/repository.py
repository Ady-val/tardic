"""Acceso a datos compartido entre la API y el worker.

Nada de HTTP aquí, nada de rutas en disco (eso es `storage.py`), y nada del
contrato del motor STT: las funciones que guardan resultados reciben tipos
propios y desacoplados (`SegmentData`) en vez de importar `core.stt`, para que
este módulo no dependa de cómo transcribe el motor — solo de qué se persiste.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .models import (
    JobStatus,
    ProcessingJob,
    Recording,
    RecordingStatus,
    Segment,
    Speaker,
    Transcript,
)


@dataclass(frozen=True)
class SegmentData:
    """Forma mínima para persistir un segmento transcrito.

    El worker traduce cada `SttSegment` (contrato de `core.stt`) a esto antes
    de llamar a `save_transcript_result`; `speaker_label` es la etiqueta
    ("SPEAKER_00"), no un id — aquí se resuelve contra `Speaker`.
    """

    start_time: float
    end_time: float
    text: str
    speaker_label: str | None = None
    confidence: float | None = None


# --------------------------------------------------------------------------
# Recording + ProcessingJob
# --------------------------------------------------------------------------
def create_recording_with_job(
    session: Session,
    *,
    filename: str,
    storage_path: str,
    mime_type: str | None = None,
    size_bytes: int | None = None,
    duration_seconds: float | None = None,
    diarize: bool = False,
    status: RecordingStatus = RecordingStatus.QUEUED,
) -> tuple[Recording, ProcessingJob]:
    """Crea el `Recording` y su `ProcessingJob` en UNA transacción.

    Que exista el uno sin el otro deja al sistema inconsistente (un audio
    que nunca se procesa, o un job huérfano): por eso van juntos, con un solo
    commit al final.
    """
    recording = Recording(
        filename=filename,
        storage_path=storage_path,
        mime_type=mime_type,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
        status=status,
    )
    session.add(recording)
    session.flush()  # necesitamos recording.id para el FK del job

    job = ProcessingJob(recording_id=recording.id, job_type="transcribe", diarize=diarize)
    session.add(job)
    session.commit()
    session.refresh(recording)
    session.refresh(job)
    return recording, job


def get_recording(session: Session, recording_id: uuid.UUID) -> Recording | None:
    return session.get(Recording, recording_id)


def get_latest_job(session: Session, recording_id: uuid.UUID) -> ProcessingJob | None:
    """El job más reciente de un recording.

    En el MVP normalmente hay uno solo (una transcripción, un job); se ordena
    por lo más parecido a "creado" que tiene la tabla, porque `ProcessingJob`
    no lleva `created_at` (ver `models.py`) — `available_at` se rellena al
    crearse y `started_at` cuando se toma, así que el más reciente de los dos
    que exista es la mejor aproximación.
    """
    stmt = (
        select(ProcessingJob)
        .where(ProcessingJob.recording_id == recording_id)
        .order_by(func.coalesce(ProcessingJob.started_at, ProcessingJob.available_at).desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def list_recordings(
    session: Session, *, limit: int = 20, offset: int = 0
) -> tuple[list[Recording], int]:
    """Listado paginado, más nuevo primero."""
    total = session.execute(select(func.count()).select_from(Recording)).scalar_one()
    stmt = select(Recording).order_by(Recording.created_at.desc()).limit(limit).offset(offset)
    items = list(session.execute(stmt).scalars().all())
    return items, total


def delete_recording(session: Session, recording_id: uuid.UUID) -> bool:
    """Borra el Recording y todo lo que cuelga de él (cascade en el modelo:
    transcript, segments, speakers, jobs). NO toca el disco — eso le toca a
    quien llame, con `storage.delete_recording`, porque este módulo no hace
    I/O de archivos."""
    recording = session.get(Recording, recording_id)
    if recording is None:
        return False
    session.delete(recording)
    session.commit()
    return True


# --------------------------------------------------------------------------
# Transcript + Segments (+ Speakers)
# --------------------------------------------------------------------------
def get_transcript(session: Session, recording_id: uuid.UUID) -> Transcript | None:
    stmt = (
        select(Transcript)
        .where(Transcript.recording_id == recording_id)
        .options(selectinload(Transcript.segments).selectinload(Segment.speaker))
    )
    return session.execute(stmt).scalar_one_or_none()


def save_transcript_result(
    session: Session,
    *,
    recording_id: uuid.UUID,
    text: str,
    language: str | None,
    model: str,
    processing_time_seconds: float | None,
    segments: Sequence[SegmentData],
    speaker_labels: Sequence[str] = (),
) -> Transcript:
    """Guarda Transcript + Segments (y Speakers si vienen) en bloque.

    Si ya existía una transcripción para el recording (reproceso o reintento
    que alcanzó a persistir algo antes de fallar) se reemplaza entera:
    `transcripts.recording_id` es único, así que no puede haber dos a la vez.
    """
    existing = session.execute(
        select(Transcript).where(Transcript.recording_id == recording_id)
    ).scalar_one_or_none()
    if existing is not None:
        session.delete(existing)
        session.flush()

    transcript = Transcript(
        recording_id=recording_id,
        text=text,
        language=language,
        model=model,
        processing_time_seconds=processing_time_seconds,
    )
    session.add(transcript)
    session.flush()  # para tener transcript.id antes de crear los segmentos

    speakers_by_label: dict[str, Speaker] = {}
    if speaker_labels:
        for label in speaker_labels:
            speaker = Speaker(recording_id=recording_id, label=label)
            session.add(speaker)
            speakers_by_label[label] = speaker
        session.flush()  # para tener speaker.id antes de referenciarlos

    for seg in segments:
        speaker = speakers_by_label.get(seg.speaker_label) if seg.speaker_label else None
        session.add(
            Segment(
                transcript_id=transcript.id,
                start_time=seg.start_time,
                end_time=seg.end_time,
                text=seg.text,
                speaker_id=speaker.id if speaker is not None else None,
                confidence=seg.confidence,
            )
        )

    session.commit()
    session.refresh(transcript)
    return transcript


# --------------------------------------------------------------------------
# Helpers de estado — los usa el worker para marcar avance/fin sin repetir
# la misma actualización de tres líneas en cada callsite.
# --------------------------------------------------------------------------
def mark_recording_failed(session: Session, recording_id: uuid.UUID, message: str) -> None:
    """`message` ya debe venir sin rutas ni trazas (eso va al log, no aquí:
    doc 03 §15 — los errores que ve el usuario son para humanos)."""
    recording = session.get(Recording, recording_id)
    if recording is None:
        return
    recording.status = RecordingStatus.FAILED
    recording.processing_error = message
    recording.ended_at = datetime.now(UTC)


def count_jobs_by_status(session: Session, status: JobStatus) -> int:
    """Diagnóstico rápido (p. ej. para el endpoint de salud o un panel)."""
    stmt = select(func.count()).select_from(ProcessingJob).where(ProcessingJob.status == status)
    return session.execute(stmt).scalar_one()
