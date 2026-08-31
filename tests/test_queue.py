"""Pruebas de la cola (`worker/queue.py`) contra una Postgres real.

Necesitan una BD viva — se levantan con:

    docker run -d --rm -p 55432:5432 -e POSTGRES_PASSWORD=test \\
        -e POSTGRES_USER=tardic -e POSTGRES_DB=tardic_test \\
        --name tardic-test-db postgres:16-alpine

Por eso van marcadas `@pytest.mark.db` (AGENTS.md regla 8): no corren en el
resto de la suite, que sí debe correr en segundos sin nada externo.

Lo que se prueba viene directo de las instrucciones de este agente: que dos
workers concurrentes no se pisen (`SKIP LOCKED` de verdad, con dos sesiones),
el backoff al fallar, que agotar `max_attempts` deje el Recording en FAILED,
y que la recuperación de jobs zombis funcione.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from tardic.models import Base, JobStatus, ProcessingJob, Recording, RecordingStatus
from tardic.repository import create_recording_with_job
from tardic.worker import queue as job_queue

TEST_DATABASE_URL = os.environ.get(
    "TARDIC_TEST_DATABASE_URL",
    "postgresql+psycopg://tardic:test@127.0.0.1:55432/tardic_test",
)

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DATABASE_URL, future=True)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def SessionFactory(engine) -> sessionmaker[Session]:  # noqa: N802 — se usa como un tipo/factory
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture(autouse=True)
def _clean_tables(engine):
    """Cada test arranca con las tablas vacías, sin importar qué dejó el
    anterior — más simple y más fiel a producción que envolver todo en una
    transacción que se revierte (eso rompería justo lo que se quiere probar:
    dos sesiones/conexiones reales viendo la misma fila)."""
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE recordings, processing_jobs RESTART IDENTITY CASCADE"))
    yield


def _make_job(session: Session, **kwargs) -> tuple[Recording, ProcessingJob]:
    defaults = dict(filename="audio.m4a", storage_path="audio/x/original.m4a")
    defaults.update(kwargs)
    return create_recording_with_job(session, **defaults)


# --------------------------------------------------------------------------
# claim_job + SKIP LOCKED
# --------------------------------------------------------------------------
def test_claim_job_dos_sesiones_no_toman_el_mismo_job(SessionFactory):
    setup = SessionFactory()
    _, job = _make_job(setup)
    job_id = job.id
    setup.close()

    session_a = SessionFactory()
    session_b = SessionFactory()
    try:
        claimed_a = job_queue.claim_job(session_a)
        assert claimed_a is not None
        assert claimed_a.id == job_id
        # session_a todavía NO ha hecho commit: la fila sigue bloqueada
        # (FOR UPDATE) del lado de Postgres. session_b, con SKIP LOCKED, debe
        # ver la cola vacía en vez de quedarse esperando el lock.
        claimed_b = job_queue.claim_job(session_b)
        assert claimed_b is None
    finally:
        session_a.commit()
        session_b.commit()
        session_a.close()
        session_b.close()

    # tras el commit de session_a, el job quedó RUNNING: nadie más lo toma.
    session_c = SessionFactory()
    try:
        assert job_queue.claim_job(session_c) is None
    finally:
        session_c.commit()
        session_c.close()


def test_claim_job_marca_running_y_processing(SessionFactory):
    setup = SessionFactory()
    recording, _job = _make_job(setup)
    recording_id = recording.id
    setup.close()

    session = SessionFactory()
    claimed = job_queue.claim_job(session)
    assert claimed is not None
    assert claimed.status == JobStatus.RUNNING
    assert claimed.attempts == 1
    assert claimed.locked_at is not None
    assert claimed.started_at is not None
    session.commit()
    session.close()

    check = SessionFactory()
    recording_row = check.get(Recording, recording_id)
    assert recording_row.status == RecordingStatus.PROCESSING
    assert recording_row.started_at is not None
    check.close()


def test_claim_job_ignora_jobs_no_disponibles_todavia(SessionFactory):
    session = SessionFactory()
    _, job = _make_job(session)
    job.available_at = datetime.now(UTC) + timedelta(minutes=5)
    session.commit()

    assert job_queue.claim_job(session) is None
    session.close()


# --------------------------------------------------------------------------
# release_job — apagado limpio, no penaliza
# --------------------------------------------------------------------------
def test_release_job_regresa_a_pending_sin_penalizar(SessionFactory):
    session = SessionFactory()
    recording, job = _make_job(session)
    recording_id, job_id = recording.id, job.id
    claimed = job_queue.claim_job(session)
    session.commit()
    assert claimed.attempts == 1

    job_queue.release_job(session, job_id)
    session.commit()

    job_row = session.get(ProcessingJob, job_id)
    assert job_row.status == JobStatus.PENDING
    assert job_row.locked_at is None
    assert job_row.attempts == 1  # release NO cuenta como fallo

    recording_row = session.get(Recording, recording_id)
    assert recording_row.status == RecordingStatus.QUEUED
    session.close()


# --------------------------------------------------------------------------
# fail_job — backoff exponencial y FAILED tras agotar intentos
# --------------------------------------------------------------------------
def test_fail_job_agenda_backoff_exponencial(SessionFactory):
    session = SessionFactory()
    recording, job = _make_job(session)
    job_id = job.id

    claimed = job_queue.claim_job(session)
    session.commit()
    assert claimed.attempts == 1

    before = job_queue._db_now(session)  # noqa: SLF001 — reloj de Postgres, no el de Python (ver queue.py)
    job_queue.fail_job(session, job_id, error="fallo simulado", max_attempts=3)
    session.commit()

    job_row = session.get(ProcessingJob, job_id)
    assert job_row.status == JobStatus.PENDING
    assert job_row.error == "fallo simulado"
    assert job_row.locked_at is None
    # backoff del primer fallo: 30s (BACKOFF_BASE_SECONDS * 2**(1-1))
    delay = (job_row.available_at - before).total_seconds()
    assert 25 <= delay <= 35

    # el Recording sigue vivo: todavía quedan reintentos, no se le avisa al
    # usuario de un fallo que el sistema puede resolver solo.
    recording_row = session.get(Recording, recording.id)
    assert recording_row.status == RecordingStatus.PROCESSING
    session.close()


def test_fail_job_backoff_crece_con_cada_intento(SessionFactory):
    session = SessionFactory()
    _, job = _make_job(session)
    job_id = job.id

    # primer fallo -> ~30s
    job_queue.claim_job(session)
    session.commit()
    before_1 = job_queue._db_now(session)  # noqa: SLF001
    job_queue.fail_job(session, job_id, error="e1", max_attempts=5)
    session.commit()
    delay_1 = (session.get(ProcessingJob, job_id).available_at - before_1).total_seconds()

    # para reintentar antes de tiempo en la prueba, se adelanta available_at
    # con el reloj de Postgres — con el de Python, el drift real visto entre
    # este proceso y el contenedor (Docker Desktop/WSL2) hacía que claim_job
    # no encontrara el job todavía, o lo encontrara antes de lo esperado.
    job_row = session.get(ProcessingJob, job_id)
    job_row.available_at = job_queue._db_now(session)  # noqa: SLF001
    session.commit()

    # segundo fallo -> ~60s (el doble)
    job_queue.claim_job(session)
    session.commit()
    before_2 = job_queue._db_now(session)  # noqa: SLF001
    job_queue.fail_job(session, job_id, error="e2", max_attempts=5)
    session.commit()
    delay_2 = (session.get(ProcessingJob, job_id).available_at - before_2).total_seconds()

    assert delay_1 == pytest.approx(30, abs=5)
    assert delay_2 == pytest.approx(60, abs=5)
    session.close()


def test_fail_job_tras_max_attempts_deja_recording_failed(SessionFactory):
    session = SessionFactory()
    recording, job = _make_job(session)
    recording_id, job_id = recording.id, job.id
    max_attempts = 3

    for _ in range(max_attempts):
        job_row = session.get(ProcessingJob, job_id)
        job_row.available_at = job_queue._db_now(session)  # noqa: SLF001 — salta el backoff
        session.commit()
        claimed = job_queue.claim_job(session)
        session.commit()
        assert claimed is not None
        job_queue.fail_job(session, job_id, error="motor caído", max_attempts=max_attempts)
        session.commit()

    job_row = session.get(ProcessingJob, job_id)
    assert job_row.status == JobStatus.FAILED
    assert job_row.attempts == max_attempts
    assert job_row.completed_at is not None

    recording_row = session.get(Recording, recording_id)
    assert recording_row.status == RecordingStatus.FAILED
    # mensaje para humanos: nada de rutas ni trazas (lo garantiza quien llama
    # a fail_job, no la función en sí; aquí solo se comprueba que se guardó).
    assert recording_row.processing_error == "motor caído"
    assert recording_row.ended_at is not None
    session.close()


# --------------------------------------------------------------------------
# recover_zombie_jobs — RF-10
# --------------------------------------------------------------------------
def test_recover_zombie_jobs_regresa_a_pending(SessionFactory):
    session = SessionFactory()
    recording, job = _make_job(session)
    recording_id, job_id = recording.id, job.id

    job_queue.claim_job(session)
    session.commit()

    # simula un worker que murió hace mucho: locked_at viejo, status RUNNING
    job_row = session.get(ProcessingJob, job_id)
    job_row.locked_at = datetime.now(UTC) - timedelta(hours=2)
    session.commit()

    recovered = job_queue.recover_zombie_jobs(session, lease_timeout_seconds=3600)
    session.commit()
    assert recovered == 1

    job_row = session.get(ProcessingJob, job_id)
    assert job_row.status == JobStatus.PENDING
    assert job_row.locked_at is None
    assert job_row.attempts == 1  # tampoco es un fallo: no se penaliza

    recording_row = session.get(Recording, recording_id)
    assert recording_row.status == RecordingStatus.QUEUED
    session.close()


def test_recover_zombie_jobs_no_toca_jobs_recientes(SessionFactory):
    session = SessionFactory()
    _, job = _make_job(session)
    job_id = job.id
    job_queue.claim_job(session)  # locked_at = ahora
    session.commit()

    recovered = job_queue.recover_zombie_jobs(session, lease_timeout_seconds=3600)
    session.commit()
    assert recovered == 0

    job_row = session.get(ProcessingJob, job_id)
    assert job_row.status == JobStatus.RUNNING
    session.close()


# --------------------------------------------------------------------------
# identidad del worker: recover_own_jobs y el lease
# --------------------------------------------------------------------------
def test_claim_job_firma_el_job_con_el_worker_id(SessionFactory):
    session = SessionFactory()
    _make_job(session)

    claimed = job_queue.claim_job(session, worker_id="worker-a")
    session.commit()
    assert claimed.locked_by == "worker-a"
    session.close()


def test_recover_own_jobs_recupera_de_inmediato_sin_esperar_el_lease(SessionFactory):
    """EL FALLO ORIGINAL: el worker moría, Docker lo reiniciaba en segundos y
    el job seguía RUNNING hasta que venciera un timeout de 8 horas.

    Un job RUNNING firmado por MÍ, cuando yo apenas estoy arrancando, está
    abandonado por definición: vuelve a la cola YA, con el lease recién puesto
    y todo."""
    session = SessionFactory()
    recording, job = _make_job(session)
    recording_id, job_id = recording.id, job.id

    # el worker tomó el job hace un instante... y lo mataron con SIGKILL.
    job_queue.claim_job(session, worker_id="worker-a")
    session.commit()
    assert session.get(ProcessingJob, job_id).locked_at is not None  # lease FRESCO

    # el mismo contenedor revive: mismo hostname, misma firma.
    recovered = job_queue.recover_own_jobs(session, worker_id="worker-a")
    session.commit()
    assert recovered == 1

    job_row = session.get(ProcessingJob, job_id)
    assert job_row.status == JobStatus.PENDING
    assert job_row.locked_at is None
    assert job_row.locked_by is None
    assert job_row.attempts == 1  # no se penaliza: el worker no falló, se murió
    assert job_row.available_at <= job_queue._db_now(session)  # noqa: SLF001 — tomable YA

    assert session.get(Recording, recording_id).status == RecordingStatus.QUEUED
    session.close()


def test_recover_own_jobs_no_toca_los_de_otro_worker(SessionFactory):
    """Con varios workers, cada uno solo reclama lo suyo: robarle a un worker
    vivo el trabajo que está transcribiendo sería peor que el bug original."""
    session = SessionFactory()
    _, job = _make_job(session)
    job_id = job.id
    job_queue.claim_job(session, worker_id="worker-b")
    session.commit()

    recovered = job_queue.recover_own_jobs(session, worker_id="worker-a")
    session.commit()
    assert recovered == 0

    job_row = session.get(ProcessingJob, job_id)
    assert job_row.status == JobStatus.RUNNING
    assert job_row.locked_by == "worker-b"
    session.close()


def test_lease_fresco_de_otro_worker_no_se_roba_pero_vencido_si(SessionFactory):
    """La otra mitad: al worker de OTRA máquina que murió y no volvió nadie le
    corre `recover_own_jobs`, así que lo rescata el lease vencido — media hora,
    no ocho horas."""
    session = SessionFactory()
    _, job = _make_job(session)
    job_id = job.id
    job_queue.claim_job(session, worker_id="worker-b")
    session.commit()

    # lease fresco: intocable, ese worker puede estar transcribiendo ahora mismo
    assert job_queue.recover_zombie_jobs(session, lease_timeout_seconds=1800) == 0
    session.commit()
    assert session.get(ProcessingJob, job_id).status == JobStatus.RUNNING

    # el lease vence (nadie renovó `locked_at` en media hora)
    job_row = session.get(ProcessingJob, job_id)
    job_row.locked_at = job_queue._db_now(session) - timedelta(seconds=1801)  # noqa: SLF001
    session.commit()

    assert job_queue.recover_zombie_jobs(session, lease_timeout_seconds=1800) == 1
    session.commit()

    job_row = session.get(ProcessingJob, job_id)
    assert job_row.status == JobStatus.PENDING
    assert job_row.locked_by is None
    assert job_row.attempts == 1
    session.close()


def test_record_progress_renueva_el_lease(SessionFactory):
    """Un trabajo legítimo de 3 horas no puede confundirse con uno abandonado:
    cada trozo reportado corre `locked_at` hacia adelante."""
    session = SessionFactory()
    _, job = _make_job(session)
    job_id = job.id
    job_queue.claim_job(session, worker_id="worker-a")
    session.commit()

    # se envejece el lease a mano: falta un minuto para que venza
    stale = job_queue._db_now(session) - timedelta(seconds=1740)  # noqa: SLF001
    job_row = session.get(ProcessingJob, job_id)
    job_row.locked_at = stale
    session.commit()

    job_queue.record_progress(
        session, job_id,
        {"chunks_done": 4, "chunks_total": 13, "percent": 31, "eta_seconds": 1680},
    )
    session.commit()

    job_row = session.get(ProcessingJob, job_id)
    assert job_row.progress["chunks_done"] == 4
    assert job_row.locked_at > stale  # el lease se renovó
    # y ahora ya no lo alcanza el corte del lease
    assert job_queue.recover_zombie_jobs(session, lease_timeout_seconds=1800) == 0
    session.commit()
    assert session.get(ProcessingJob, job_id).status == JobStatus.RUNNING
    session.close()


def test_record_progress_no_resella_un_job_que_ya_no_es_nuestro(SessionFactory):
    """Si el job se recuperó (o se liberó) mientras el motor seguía corriendo,
    el progreso tardío no debe re-sellar `locked_at` sobre una fila PENDING."""
    session = SessionFactory()
    _, job = _make_job(session)
    job_id = job.id
    job_queue.claim_job(session, worker_id="worker-a")
    session.commit()
    job_queue.release_job(session, job_id)
    session.commit()

    job_queue.record_progress(session, job_id, {"chunks_done": 1, "chunks_total": 2,
                                                "percent": 50, "eta_seconds": 10})
    session.commit()

    job_row = session.get(ProcessingJob, job_id)
    assert job_row.status == JobStatus.PENDING
    assert job_row.locked_at is None
    session.close()


def test_fail_job_y_release_job_borran_la_firma(SessionFactory):
    """`locked_by` es "quién lo tiene AHORA": dejarla puesta en un job que ya
    volvió a la cola haría que ese worker se lo "recuperara" a sí mismo al
    arrancar, aunque otro ya lo estuviera trabajando."""
    session = SessionFactory()
    _, job = _make_job(session)
    job_id = job.id

    job_queue.claim_job(session, worker_id="worker-a")
    session.commit()
    job_queue.fail_job(session, job_id, error="tronó", max_attempts=3)
    session.commit()
    assert session.get(ProcessingJob, job_id).locked_by is None

    job_row = session.get(ProcessingJob, job_id)
    job_row.available_at = job_queue._db_now(session)  # noqa: SLF001
    session.commit()
    job_queue.claim_job(session, worker_id="worker-a")
    session.commit()
    job_queue.release_job(session, job_id)
    session.commit()
    assert session.get(ProcessingJob, job_id).locked_by is None
    session.close()


def test_recover_stuck_processing_jobs_sin_job_running(SessionFactory):
    """Caso raro (doc del módulo): un Recording quedó en PROCESSING sin
    ningún job RUNNING detrás — se corrige aparte de la recuperación de
    zombis, que solo mira la tabla de jobs."""
    session = SessionFactory()
    recording, job = _make_job(session)
    recording.status = RecordingStatus.PROCESSING
    session.commit()

    fixed = job_queue.recover_stuck_processing_jobs(session)
    session.commit()
    assert fixed == 1
    session.close()

    # el UPDATE es masivo (`synchronize_session=False`, ver queue.py): no
    # refresca objetos ya cargados en la sesión que lo emitió, así que se lee
    # con una sesión nueva — como haría cualquier otro proceso real.
    check = SessionFactory()
    recording_row = check.get(Recording, recording.id)
    assert recording_row.status == RecordingStatus.QUEUED
    check.close()
