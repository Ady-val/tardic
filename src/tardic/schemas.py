"""Esquemas de la API — el contrato con Postman y con cualquier cliente.

Cambiar un nombre de campo aquí rompe al consumidor: los agentes se ciñen a
esto. RF-11: la salida no se limita a texto plano, conserva timestamps,
segmentos, hablantes, idioma, estado y relación con el audio original.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .models import JobStatus, RecordingStatus, Stage


class Progress(BaseModel):
    """Avance real, no estimado a ojo: sale de los trozos ya transcritos."""

    chunks_done: int = 0
    chunks_total: int = 0
    percent: int = Field(0, ge=0, le=100)
    eta_seconds: int | None = None


class RecordingCreated(BaseModel):
    """Respuesta del POST. Devuelve rápido: el trabajo pesado es del worker."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: RecordingStatus
    filename: str
    size_bytes: int | None = None
    duration_seconds: float | None = None


class RecordingStatusOut(BaseModel):
    """Lo que consulta Postman mientras espera."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: RecordingStatus
    stage: Stage | None = None
    progress: Progress = Field(default_factory=Progress)
    filename: str
    duration_seconds: float | None = None
    language: str | None = None
    created_at: datetime
    attempts: int = 0
    # Poblado solo cuando status == FAILED. Mensaje para humanos, sin rutas del
    # servidor ni trazas: eso va al log (doc 03 §15).
    error: str | None = None
    # Aviso para un final raro pero legítimo: terminó bien y no hay texto. Un
    # TXT de 0 bytes sin explicación deja al usuario sin saber si falló algo o
    # si de verdad no había nada que transcribir.
    warning: str | None = None
    # Atajo para el cliente: aparece cuando status == COMPLETED.
    transcript_url: str | None = None
    transcript_txt_url: str | None = None


class SegmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    start_time: float
    end_time: float
    text: str
    speaker: str | None = None  # la etiqueta, no el uuid
    confidence: float | None = None


class TranscriptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    recording_id: uuid.UUID
    text: str
    language: str | None = None
    model: str
    duration_seconds: float | None = None
    processing_time_seconds: float | None = None
    created_at: datetime
    segments: list[SegmentOut]


class RecordingListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    status: RecordingStatus
    duration_seconds: float | None = None
    created_at: datetime


class RecordingList(BaseModel):
    items: list[RecordingListItem]
    total: int
    limit: int
    offset: int


class JobOut(BaseModel):
    """Diagnóstico. No lo necesita el flujo feliz, sí cuando algo falla."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_type: str
    status: JobStatus
    stage: Stage | None = None
    attempts: int
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class ErrorOut(BaseModel):
    """Toda respuesta de error tiene esta forma. Sin excepciones."""

    detail: str
    code: str | None = None


class HealthOut(BaseModel):
    status: str = "ok"
    version: str
    database: bool
    # El worker escribe un latido; si lleva mucho callado, algo pasa.
    worker_seen_seconds_ago: float | None = None
