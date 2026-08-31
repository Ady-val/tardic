"""Entidades del dominio — exactamente las cinco del doc 03 §3.

Los agentes NO deben renombrar campos ni inventar tablas: la API, el worker y
las migraciones se ensamblan sobre estos nombres. Agregar una columna nueva es
válido si algo lo exige; quitar o renombrar, no.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Estados — RF-09 al pie de la letra. No agregar valores sin cambiar el doc.
# --------------------------------------------------------------------------
class RecordingStatus(str, enum.Enum):
    UPLOADING = "UPLOADING"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class Stage(str, enum.Enum):
    """Etapas del pipeline del doc 03 §2, las que el MVP recorre."""

    PREPROCESS = "PREPROCESS"
    TRANSCRIBE = "TRANSCRIBE"
    DIARIZE = "DIARIZE"
    MERGE = "MERGE"
    PERSIST = "PERSIST"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    filename: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    # BigInteger, no Integer: int4 topa en 2 GiB y `max_upload_bytes` es libre.
    # Con un tope mayor, el INSERT reventaba DESPUES de subir el archivo entero.
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # Relativa a settings.data_dir. NUNCA absoluta ni proveniente del cliente:
    # el nombre que sube el usuario no toca el disco (path traversal).
    storage_path: Mapped[str] = mapped_column(String(1024))

    status: Mapped[RecordingStatus] = mapped_column(
        Enum(RecordingStatus, native_enum=False, length=16),
        default=RecordingStatus.UPLOADING, index=True,
    )
    language: Mapped[str | None] = mapped_column(String(16))
    processing_error: Mapped[str | None] = mapped_column(Text)

    transcript: Mapped[Transcript | None] = relationship(
        back_populates="recording", cascade="all, delete-orphan", uselist=False)
    speakers: Mapped[list[Speaker]] = relationship(
        back_populates="recording", cascade="all, delete-orphan")
    jobs: Mapped[list[ProcessingJob]] = relationship(
        back_populates="recording", cascade="all, delete-orphan")


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    recording_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), unique=True, index=True)
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processing_time_seconds: Mapped[float | None] = mapped_column(Float)

    recording: Mapped[Recording] = relationship(back_populates="transcript")
    segments: Mapped[list[Segment]] = relationship(
        back_populates="transcript", cascade="all, delete-orphan",
        order_by="Segment.start_time")


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (
        CheckConstraint("end_time >= start_time", name="ck_segment_times"),
        Index("ix_segments_transcript_start", "transcript_id", "start_time"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    transcript_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transcripts.id", ondelete="CASCADE"))
    # Segundos desde el inicio del audio COMPLETO, no del trozo. Al unir los
    # trozos hay que devolver los timestamps a la línea de tiempo original.
    start_time: Mapped[float] = mapped_column(Float)
    end_time: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text)
    speaker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("speakers.id", ondelete="SET NULL"))
    confidence: Mapped[float | None] = mapped_column(Float)

    transcript: Mapped[Transcript] = relationship(back_populates="segments")
    speaker: Mapped[Speaker | None] = relationship(back_populates="segments")


class Speaker(Base):
    """RF-08: primero "Speaker 1"; person_id se llena mucho después, cuando
    exista gente en el sistema. Que esté vacío no bloquea nada."""

    __tablename__ = "speakers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    recording_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(64))  # "SPEAKER_00"
    person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    recording: Mapped[Recording] = relationship(back_populates="speakers")
    segments: Mapped[list[Segment]] = relationship(back_populates="speaker")


class ProcessingJob(Base):
    """Fila de trabajo y, a la vez, la cola.

    No hay Redis ni broker: el worker toma trabajo con
    `SELECT ... FOR UPDATE SKIP LOCKED` sobre esta tabla (doc 02 §6 y §10).
    `progress` guarda el avance real por trozo, que es lo que el endpoint de
    estado le muestra al usuario.
    """

    __tablename__ = "processing_jobs"
    __table_args__ = (
        Index("ix_jobs_claimable", "status", "available_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    recording_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), index=True)
    job_type: Mapped[str] = mapped_column(String(32), default="transcribe")
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=16), default=JobStatus.PENDING, index=True)
    stage: Mapped[Stage | None] = mapped_column(Enum(Stage, native_enum=False, length=16))

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Cuándo puede volver a tomarse: ahora al crearse, en el futuro tras fallar
    # (backoff). El worker filtra por available_at <= now().
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    # Cuándo se tomó el job Y hasta cuándo vale el lease: el worker lo renueva
    # en cada trozo, así un trabajo legítimo de 3 h nunca se ve abandonado.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # QUIÉN lo tiene (hostname del worker). Sin esto, un worker que revive tras
    # un SIGKILL no puede distinguir "mi propio job huérfano de la encarnación
    # anterior" (recuperable YA) de "el job vivo de otra máquina" (hay que
    # esperar a que expire su lease). Con `restart: unless-stopped` el
    # contenedor vuelve en segundos y el hostname es el mismo.
    locked_by: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    diarize: Mapped[bool] = mapped_column(default=False)
    # {"chunks_done": 4, "chunks_total": 13, "percent": 31, "eta_seconds": 1680}
    progress: Mapped[dict | None] = mapped_column(JSONB)

    recording: Mapped[Recording] = relationship(back_populates="jobs")
