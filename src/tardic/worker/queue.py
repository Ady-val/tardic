"""La cola ES Postgres (doc 02 §6 y §10): sin Redis, sin broker.

Un `ProcessingJob` en `PENDING` con `available_at <= now()` es tomable. Tomarlo
es un `SELECT ... FOR UPDATE SKIP LOCKED`: dos workers que consultan al mismo
tiempo nunca chocan con la misma fila — el segundo simplemente ve una fila
menos (la que el primero ya bloqueó) y sigue de largo, en vez de esperar.

Ninguna función de aquí hace `commit()`: quien llama decide cuándo. Esto es a
propósito — `claim_job` necesita que el caller haga commit pronto para soltar
el lock de la fila, y `fail_job`/`release_job` normalmente van en la misma
transacción donde también se actualiza el `Recording`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import JobStatus, ProcessingJob, Recording, RecordingStatus

# Backoff exponencial tras un fallo: 30s, 60s, 120s, 240s... El primer
# reintento no debería tardar mucho (pudo ser un hipo de ffmpeg o de red),
# pero si sigue fallando no tiene caso martillar el mismo trabajo cada 30s.
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600  # tope de una hora: no queremos un job dormido un día


def _db_now(session: Session) -> datetime:
    """Hora actual del SERVIDOR de Postgres — NUNCA la del proceso Python.

    Todo lo que se escribe en `available_at` se compara después contra
    `func.now()` en el WHERE de `claim_job`. Si esa comparación mezclara el
    reloj de Postgres con el reloj de Python, cualquier drift entre los dos
    —real en Docker Desktop/WSL2, confirmado con pruebas repetidas— hace que
    un job recién liberado o reintentado a veces no aparezca como tomable
    todavía (o aparezca tomable antes de tiempo). Una sola fuente de verdad
    para "ahora": la base de datos.
    """
    return session.execute(select(func.now())).scalar_one()


def claim_job(session: Session, *, worker_id: str | None = None) -> ProcessingJob | None:
    """Toma el siguiente job tomable, o `None` si no hay ninguno.

    `SELECT ... WHERE status='PENDING' AND available_at <= now() ORDER BY
    available_at FOR UPDATE SKIP LOCKED LIMIT 1`, y en la misma transacción
    lo marca `RUNNING` (`locked_at`, `started_at` si es el primer intento,
    `attempts += 1`) y pone el `Recording` en `PROCESSING`.

    No hace commit — la fila queda bloqueada (lock de Postgres) hasta que el
    caller haga `commit()` o `rollback()`. El worker real hace commit de
    inmediato tras llamar esto, para no tener la fila tomada más tiempo del
    necesario.

    El `now()` del WHERE es el de POSTGRES (`func.now()`), no el de Python:
    `available_at` se pone con `server_default=func.now()` al crear el job, y
    normalmente se reclama segundos después — comparar contra el reloj del
    proceso Python introduce una carrera real si hay el más mínimo drift
    entre ese reloj y el del contenedor de Postgres (visto en pruebas: un
    job recién creado a veces no aparecía como tomable todavía).
    """
    stmt = (
        select(ProcessingJob)
        .where(ProcessingJob.status == JobStatus.PENDING, ProcessingJob.available_at <= func.now())
        .order_by(ProcessingJob.available_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.execute(stmt).scalar_one_or_none()
    if job is None:
        return None

    now = _db_now(session)
    job.status = JobStatus.RUNNING
    job.locked_at = now
    # Firma de quién se lo llevó: es lo que le permite a este mismo worker
    # reconocer sus propios huérfanos al reiniciar (ver `recover_own_jobs`).
    job.locked_by = worker_id
    if job.started_at is None:
        job.started_at = now
    job.attempts += 1
    job.error = None  # el error de un intento previo no debe verse como si fuera del actual

    recording = session.get(Recording, job.recording_id)
    if recording is not None and recording.status != RecordingStatus.PROCESSING:
        recording.status = RecordingStatus.PROCESSING
        if recording.started_at is None:
            recording.started_at = now

    session.flush()
    return job


def release_job(session: Session, job_id: uuid.UUID) -> None:
    """Suelta un job tomado SIN penalizarlo: para el apagado limpio (SIGTERM).

    No es un fallo — no toca `attempts` ni agenda backoff — solo lo regresa a
    `PENDING`, disponible de inmediato, para que el próximo worker (o este
    mismo, al reiniciar) lo retome donde el motor haya dejado el checkpoint
    (RF-10).
    """
    job = session.get(ProcessingJob, job_id)
    if job is None:
        return
    job.status = JobStatus.PENDING
    job.locked_at = None
    job.locked_by = None
    job.available_at = _db_now(session)

    recording = session.get(Recording, job.recording_id)
    if recording is not None and recording.status == RecordingStatus.PROCESSING:
        recording.status = RecordingStatus.QUEUED

    session.flush()


def record_progress(session: Session, job_id: uuid.UUID, progress: dict) -> None:
    """Guarda el avance de un trozo y, en el mismo golpe, RENUEVA el lease.

    Las dos cosas van juntas a propósito: reportar progreso es justamente la
    prueba de que el worker sigue vivo. Sin renovar `locked_at`, un trabajo
    legítimo más largo que `lease_timeout_seconds` (media hora) se vería como
    abandonado y otro worker se lo robaría a media transcripción.

    El sello es la hora de POSTGRES (`_db_now`), no la del proceso: es la misma
    contra la que `recover_zombie_jobs` compara el corte.
    """
    job = session.get(ProcessingJob, job_id)
    if job is None:
        return
    job.progress = progress
    # Solo se renueva si el job sigue siendo nuestro: si mientras tanto se
    # recuperó como zombi (o se liberó), re-sellar `locked_at` sobre un job
    # PENDING dejaría basura confusa en la fila.
    if job.status == JobStatus.RUNNING:
        job.locked_at = _db_now(session)
    session.flush()


def fail_job(session: Session, job_id: uuid.UUID, *, error: str, max_attempts: int) -> None:
    """Registra un fallo real (el motor lanzó, ffmpeg tronó, etc.).

    Si quedan intentos, agenda un reintento con backoff exponencial. Si ya
    se agotaron los `max_attempts`, el job y el `Recording` quedan `FAILED`
    — `error` debe ser ya un mensaje para humanos, sin rutas del servidor ni
    trazas (eso se logea aparte, doc 03 §15); esta función no lo reescribe,
    confía en lo que le pasan.
    """
    job = session.get(ProcessingJob, job_id)
    if job is None:
        return
    now = _db_now(session)
    job.error = error
    job.locked_at = None
    job.locked_by = None

    if job.attempts >= max_attempts:
        job.status = JobStatus.FAILED
        job.completed_at = now
        recording = session.get(Recording, job.recording_id)
        if recording is not None:
            recording.status = RecordingStatus.FAILED
            recording.processing_error = error
            recording.ended_at = now
    else:
        job.status = JobStatus.PENDING
        delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (job.attempts - 1)))
        job.available_at = now + timedelta(seconds=delay)
        # el Recording se queda en PROCESSING mientras haya reintentos
        # pendientes — no tiene caso avisarle al usuario de un fallo que el
        # sistema todavía va a resolver solo.

    session.flush()


def _requeue(session: Session, jobs: list[ProcessingJob], now: datetime) -> None:
    """Devuelve jobs a la cola sin penalizarlos. No hace commit."""
    for job in jobs:
        job.status = JobStatus.PENDING
        job.locked_at = None
        job.locked_by = None
        job.available_at = now
        recording = session.get(Recording, job.recording_id)
        if recording is not None and recording.status == RecordingStatus.PROCESSING:
            recording.status = RecordingStatus.QUEUED
    if jobs:
        session.flush()


def recover_own_jobs(session: Session, *, worker_id: str) -> int:
    """Recupera AL INSTANTE los jobs que este mismo worker traía antes de morir.

    Un job `RUNNING` firmado con MI `worker_id` solo puede venir de una
    encarnación anterior de este proceso: yo estoy arrancando, o sea que no lo
    estoy trabajando. Por definición está abandonado, sin importar hace cuánto
    se tomó — así que no hay ninguna razón para esperar a que expire el lease.

    Ese era el fallo que dejaba un `Recording` congelado en "PROCESSING 62 %"
    durante 8 horas con el worker sano y ocioso al lado: Docker reinicia el
    contenedor en segundos (`restart: unless-stopped`) pero el job seguía
    `RUNNING` hasta que venciera un timeout pensado para otra cosa.

    Es seguro aunque algún día haya varios workers: cada uno solo reclama lo
    que lleva su propia firma, nunca lo de otro.

    NO penaliza `attempts` — el worker no decidió que el trabajo fracasara, se
    murió. El checkpoint por trozo (RF-10) hace que retomarlo sea barato.
    """
    stmt = (
        select(ProcessingJob)
        .where(ProcessingJob.status == JobStatus.RUNNING, ProcessingJob.locked_by == worker_id)
        .with_for_update(skip_locked=True)
    )
    mine = list(session.execute(stmt).scalars().all())
    _requeue(session, mine, _db_now(session))
    return len(mine)


def recover_zombie_jobs(session: Session, *, lease_timeout_seconds: int) -> int:
    """RF-10: red de seguridad para el worker de OTRA máquina que murió y no
    volvió — a ese nadie le va a correr `recover_own_jobs`.

    Cualquier job `RUNNING` cuyo `locked_at` sea más viejo que
    `lease_timeout_seconds` se considera abandonado y vuelve a `PENDING`,
    disponible de inmediato.

    El corte es el LEASE, no `job_timeout_seconds`: son cosas distintas y
    confundirlas costaba horas de recuperación. `job_timeout_seconds` (8 h) es
    el tope de duración de un trabajo; el lease (media hora) es cuánto aguanta
    un job sin señales de vida. Como el worker renueva `locked_at` en cada
    trozo (`record_progress`), un trabajo legítimo de 3 horas nunca cruza ese
    corte aunque el lease sea corto.

    Deliberadamente NO cuenta como fallo (no toca `attempts`): el worker que
    lo traía no llegó a decidir que fracasó, simplemente desapareció. Devuelve
    cuántos jobs se recuperaron, para que el arranque lo pueda logear.
    """
    now = _db_now(session)
    cutoff = now - timedelta(seconds=lease_timeout_seconds)
    stmt = (
        select(ProcessingJob)
        .where(ProcessingJob.status == JobStatus.RUNNING, ProcessingJob.locked_at < cutoff)
        .with_for_update(skip_locked=True)
    )
    zombies = list(session.execute(stmt).scalars().all())
    _requeue(session, zombies, now)
    return len(zombies)


def recover_stuck_processing_jobs(session: Session) -> int:
    """Complemento defensivo de `recover_zombie_jobs`, sin depender del
    tiempo: cubre el caso raro de un `Recording` que se quedó en `PROCESSING`
    sin ningún job `RUNNING` detrás (p. ej. una caída justo entre soltar el
    job y actualizar el recording). Se corre junto con la recuperación de
    zombis al arrancar. Devuelve cuántos recordings se corrigieron.
    """
    running_recording_ids = select(ProcessingJob.recording_id).where(
        ProcessingJob.status == JobStatus.RUNNING
    )
    stmt = (
        update(Recording)
        .where(Recording.status == RecordingStatus.PROCESSING)
        .where(Recording.id.not_in(running_recording_ids))
        .values(status=RecordingStatus.QUEUED)
        .execution_options(synchronize_session=False)
    )
    result = session.execute(stmt)
    return result.rowcount or 0
