"""Pruebas del bucle del worker (`worker/main.py`) contra una Postgres real.

Igual que `test_queue.py`, necesitan la BD de prueba — `@pytest.mark.db`.

El motor real (faster-whisper) nunca se usa aquí: se inyecta `FakeEngine`
(`tardic.core.fake_engine`, del agente A — ya existe, así que se reusa en vez
de escribir uno propio) o, para el caso del progreso, un motor de prueba
propio que expone justo el punto medio de `transcribe()` que hace falta
comprobar. `default_preprocess` sí corre de verdad (ffmpeg está disponible y
un clip sintético de segundos es rápido): así el pipeline se ejercita
completo, PREPROCESS incluido, sin marcarlo `slow`.
"""
from __future__ import annotations

import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from tardic.config import Settings
from tardic.core.fake_engine import FakeEngine
from tardic.core.stt import ChunkProgress, ProgressCallback, SttResult, SttSegment
from tardic.models import Base, JobStatus, ProcessingJob, Recording, RecordingStatus, Stage
from tardic.repository import create_recording_with_job, get_transcript
from tardic.storage import Storage
from tardic.worker import queue as job_queue
from tardic.worker.main import Worker

TEST_DATABASE_URL = os.environ.get(
    "TARDIC_TEST_DATABASE_URL",
    "postgresql+psycopg://tardic:test@127.0.0.1:55432/tardic_test",
)

pytestmark = pytest.mark.db


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DATABASE_URL, future=True)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def SessionFactory(engine) -> sessionmaker[Session]:  # noqa: N802
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture(autouse=True)
def _clean_tables(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE recordings, processing_jobs RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        api_key="test-api-key-0123456789",
        database_url=TEST_DATABASE_URL,
        data_dir=tmp_path,
        max_attempts=3,
        poll_interval_seconds=0.01,
        job_timeout_seconds=3600,
        lease_timeout_seconds=1800,
    )


@pytest.fixture
def storage(settings) -> Storage:
    return Storage(settings.data_dir)


def _make_synthetic_audio(dst: Path, seconds: float = 6.0) -> None:
    """Fixture sintética con ffmpeg (AGENTS.md regla 2: nada de audio de
    clientes en el repo). Un tono generado, no un archivo real de nadie."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-ar", "22050", "-ac", "1", str(dst),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def _seed_recording(
    session: Session, storage: Storage, *, seconds: float = 6.0
) -> tuple[uuid.UUID, uuid.UUID]:
    """Crea un Recording+ProcessingJob de verdad y deja un audio sintético en
    la ruta que `storage.py` espera para el original."""
    recording, job = create_recording_with_job(
        session, filename="clip.wav", storage_path="pending"  # se corrige abajo
    )
    # `create_recording_with_job` hace `session.refresh(...)` tras su propio
    # commit, lo que reabre una transacción (aunque sea de solo lectura) en
    # esta `session`. Si esa transacción se queda viva mientras corre ffmpeg
    # (subprocess, potencialmente lento si la máquina está ocupada con otros
    # agentes) bloquea el TRUNCATE del siguiente test — visto en pruebas: se
    # cuelga la sesión "idle in transaction" y el siguiente test espera el
    # lock para siempre. Se cierra aquí, antes de la parte lenta que no toca
    # la BD.
    session.commit()
    original = storage.original_path(recording.id, suffix=".wav")
    _make_synthetic_audio(original, seconds=seconds)
    recording.storage_path = storage.relative(original)
    session.commit()
    return recording.id, job.id


def _engine_factory(engine_instance):
    """El worker pide una *factory* (parámetro/factory, nunca un import
    fijo — instrucción del agente): esto envuelve una instancia ya creada."""
    return lambda: engine_instance


# --------------------------------------------------------------------------
# pipeline feliz: QUEUED -> PROCESSING -> COMPLETED
# --------------------------------------------------------------------------
def test_pipeline_completo_con_fake_engine(SessionFactory, storage, settings):
    setup = SessionFactory()
    recording_id, job_id = _seed_recording(setup, storage, seconds=6.0)
    assert setup.get(Recording, recording_id).status == RecordingStatus.QUEUED
    setup.close()

    worker = Worker(
        session_factory=SessionFactory,
        engine_factory=_engine_factory(FakeEngine(chunk_seconds=2.0)),
        storage=storage,
        settings=settings,
    )
    processed = worker.run_once()
    assert processed is True

    check = SessionFactory()
    recording = check.get(Recording, recording_id)
    job = check.get(ProcessingJob, job_id)

    assert recording.status == RecordingStatus.COMPLETED
    assert recording.duration_seconds == pytest.approx(6.0, abs=0.5)
    assert recording.language == "es"
    assert recording.ended_at is not None

    assert job.status == JobStatus.DONE
    assert job.stage == Stage.PERSIST
    assert job.completed_at is not None
    assert job.progress["percent"] == 100

    transcript = get_transcript(check, recording_id)
    assert transcript is not None
    assert transcript.model == "fake"
    assert len(transcript.segments) > 0
    check.close()

    # el .txt entregable quedó en disco (storage.py)
    txt_path = storage.transcript_txt_path(recording_id)
    assert txt_path.exists()
    assert txt_path.read_text(encoding="utf-8") == transcript.text


def test_segmentos_quedan_con_timestamps_absolutos(SessionFactory, storage, settings):
    """Con varios trozos (chunk_seconds=2 sobre un audio de 6s), los
    timestamps de los segmentos deben cubrir la línea de tiempo COMPLETA,
    creciendo sin reiniciarse en cada trozo (AGENTS.md regla 6)."""
    setup = SessionFactory()
    recording_id, job_id = _seed_recording(setup, storage, seconds=6.0)
    setup.close()

    worker = Worker(
        session_factory=SessionFactory,
        engine_factory=_engine_factory(FakeEngine(chunk_seconds=2.0)),
        storage=storage,
        settings=settings,
    )
    worker.run_once()

    check = SessionFactory()
    transcript = get_transcript(check, recording_id)
    starts = [s.start_time for s in transcript.segments]
    ends = [s.end_time for s in transcript.segments]

    assert starts == sorted(starts)  # el orden de la relación ya es por start_time
    assert starts[0] == pytest.approx(0.0, abs=0.01)
    assert ends[-1] == pytest.approx(6.0, abs=0.5)
    # si el motor reiniciara el reloj en cada trozo, este segmento del
    # segundo/tercer trozo empezaría en 0 o 2 otra vez en vez de seguir
    # subiendo — con 3 trozos de 2s debe haber segmentos más allá del primero.
    assert any(s > 2.0 for s in starts)
    check.close()


def test_progreso_se_actualiza_en_la_bd_durante_el_proceso(SessionFactory, storage, settings):
    """El callback `on_progress` debe escribir en una transacción CORTA e
    INDEPENDIENTE — visible para otra sesión mientras `transcribe()` sigue
    corriendo, no solo al final. Se prueba con un motor de una sola prueba
    que, a la mitad de `transcribe()`, deja que el test lea la BD con su
    propia sesión antes de terminar."""
    setup = SessionFactory()
    recording_id, job_id = _seed_recording(setup, storage, seconds=1.0)
    setup.close()

    snapshots: list[dict] = []

    @dataclass
    class MidflightProbeEngine:
        session_factory: sessionmaker[Session]
        job_id: uuid.UUID
        chunks: list[dict] = field(default_factory=lambda: [{}, {}])

        def transcribe(
            self, audio_path: Path, *, checkpoint_dir: Path | None = None,
            on_progress: ProgressCallback | None = None,
        ) -> SttResult:
            assert on_progress is not None
            on_progress(ChunkProgress(
                chunks_done=1, chunks_total=2, seconds_done=0.5,
                seconds_total=1.0, elapsed_seconds=0.05,
            ))
            # transcribe() SIGUE corriendo aquí: si esta lectura, con una
            # sesión totalmente aparte, ya ve el avance, es porque
            # on_progress no dejó la transacción abierta esperando a que
            # todo el trabajo termine.
            with self.session_factory() as probe_session:
                job = probe_session.get(ProcessingJob, self.job_id)
                snapshots.append(dict(job.progress))

            on_progress(ChunkProgress(
                chunks_done=2, chunks_total=2, seconds_done=1.0,
                seconds_total=1.0, elapsed_seconds=0.1,
            ))
            return SttResult(
                segments=[SttSegment(start=0.0, end=1.0, text="hola", confidence=1.0)],
                language="es", model="probe", processing_time_seconds=0.1,
                audio_duration_seconds=1.0, chunks=self.chunks,
            )

    worker = Worker(
        session_factory=SessionFactory,
        engine_factory=_engine_factory(MidflightProbeEngine(SessionFactory, job_id)),
        storage=storage,
        settings=settings,
    )
    worker.run_once()

    assert len(snapshots) == 1
    assert snapshots[0] == {"chunks_done": 1, "chunks_total": 2, "percent": 50, "eta_seconds": 0}

    check = SessionFactory()
    assert check.get(Recording, recording_id).status == RecordingStatus.COMPLETED
    check.close()


# --------------------------------------------------------------------------
# motor que falla -> job reintentable
# --------------------------------------------------------------------------
def test_motor_que_lanza_excepcion_deja_job_reintentable(SessionFactory, storage, settings):
    class BrokenEngine:
        def transcribe(self, audio_path, *, checkpoint_dir=None, on_progress=None) -> SttResult:
            raise RuntimeError("el motor truena")

    setup = SessionFactory()
    recording_id, job_id = _seed_recording(setup, storage, seconds=1.0)
    setup.close()

    worker = Worker(
        session_factory=SessionFactory,
        engine_factory=_engine_factory(BrokenEngine()),
        storage=storage,
        settings=settings,  # max_attempts=3: un solo fallo debe quedar reintentable
    )
    processed = worker.run_once()
    assert processed is True

    check = SessionFactory()
    job = check.get(ProcessingJob, job_id)
    recording = check.get(Recording, recording_id)

    assert job.status == JobStatus.PENDING  # no FAILED todavía: quedan reintentos
    assert job.attempts == 1
    assert job.error  # mensaje guardado
    assert job.available_at > job_queue._db_now(check)  # noqa: SLF001 — con backoff, no disponible YA

    # el Recording no se le reporta como fallido al usuario mientras el
    # sistema todavía va a reintentar solo.
    assert recording.status == RecordingStatus.PROCESSING
    check.close()


def test_motor_que_siempre_falla_agota_intentos_y_falla_recording(SessionFactory, storage, settings):
    class AlwaysBrokenEngine:
        def transcribe(self, audio_path, *, checkpoint_dir=None, on_progress=None) -> SttResult:
            raise RuntimeError("el motor truena siempre")

    setup = SessionFactory()
    recording_id, job_id = _seed_recording(setup, storage, seconds=1.0)
    setup.close()

    worker = Worker(
        session_factory=SessionFactory,
        engine_factory=_engine_factory(AlwaysBrokenEngine()),
        storage=storage,
        settings=settings,
    )
    for _ in range(settings.max_attempts):
        with SessionFactory() as s:
            job = s.get(ProcessingJob, job_id)
            job.available_at = job_queue._db_now(s)  # noqa: SLF001 — salta el backoff, reloj de Postgres
            s.commit()
        worker.run_once()

    check = SessionFactory()
    job = check.get(ProcessingJob, job_id)
    recording = check.get(Recording, recording_id)
    assert job.status == JobStatus.FAILED
    assert job.attempts == settings.max_attempts
    assert recording.status == RecordingStatus.FAILED
    assert recording.processing_error  # mensaje para humanos, sin rutas ni trazas
    check.close()


# --------------------------------------------------------------------------
# apagado limpio: SIGTERM libera el job sin penalizarlo
# --------------------------------------------------------------------------
def test_apagado_limpio_libera_el_job_sin_penalizar(SessionFactory, storage, settings):
    class SlowEngine:
        """Simula un motor que reporta progreso entre trozos; el worker debe
        cortar en esa frontera al ver `self._stop`, no en cualquier punto."""

        def __init__(self, worker_ref: dict) -> None:
            self.worker_ref = worker_ref

        def transcribe(self, audio_path, *, checkpoint_dir=None, on_progress=None) -> SttResult:
            # pide el apagado ANTES de que on_progress lo revise — simula
            # que `docker compose down` llegó a medio trabajo.
            self.worker_ref["worker"].request_stop()
            on_progress(ChunkProgress(
                chunks_done=1, chunks_total=4, seconds_done=1.0,
                seconds_total=4.0, elapsed_seconds=0.1,
            ))
            raise AssertionError("no debería seguir corriendo tras el _ShutdownSignal")

    setup = SessionFactory()
    recording_id, job_id = _seed_recording(setup, storage, seconds=1.0)
    setup.close()

    ref: dict = {}
    worker = Worker(
        session_factory=SessionFactory,
        engine_factory=_engine_factory(SlowEngine(ref)),
        storage=storage,
        settings=settings,
    )
    ref["worker"] = worker

    processed = worker.run_once()
    assert processed is True

    check = SessionFactory()
    job = check.get(ProcessingJob, job_id)
    recording_row = check.get(Recording, recording_id)
    assert job.status == JobStatus.PENDING
    assert job.attempts == 1  # apagado limpio no es un fallo: no se penaliza
    assert job.locked_at is None
    assert recording_row.status == RecordingStatus.QUEUED
    check.close()


# --------------------------------------------------------------------------
# identidad del worker: un job huérfano vuelve a la cola AL INSTANTE
# --------------------------------------------------------------------------
def _worker(session_factory, storage, settings, *, engine=None, worker_id="worker-test") -> Worker:
    return Worker(
        session_factory=session_factory,
        engine_factory=_engine_factory(
            engine if engine is not None else FakeEngine(chunk_seconds=2.0)
        ),
        storage=storage,
        settings=settings,
        worker_id=worker_id,
    )


def test_recover_at_startup_recupera_los_jobs_propios_de_inmediato(
    SessionFactory, storage, settings
):
    """EL FALLO: el worker moría con un job a medias, Docker lo reiniciaba en
    segundos y el Recording se quedaba congelado en PROCESSING durante HORAS
    porque solo se recuperaba tras `job_timeout_seconds` (8 h).

    Con `locked_by`, el worker que revive reconoce lo suyo y lo devuelve a la
    cola sin esperar nada — aquí el lease es de media hora y el job se tomó
    hace un instante, así que si se recupera es por identidad, no por tiempo."""
    setup = SessionFactory()
    recording_id, job_id = _seed_recording(setup, storage, seconds=1.0)
    setup.close()

    # encarnación 1: toma el job y la matan con SIGKILL (ni release ni fail)
    worker = _worker(SessionFactory, storage, settings)
    assert worker._claim() is not None
    with SessionFactory() as s:
        job = s.get(ProcessingJob, job_id)
        assert job.status == JobStatus.RUNNING
        assert job.locked_by == "worker-test"
        assert job.attempts == 1

    # encarnación 2: mismo contenedor, mismo hostname, arranca de cero
    revived = _worker(SessionFactory, storage, settings)
    revived.recover_at_startup()

    with SessionFactory() as s:
        job = s.get(ProcessingJob, job_id)
        assert job.status == JobStatus.PENDING
        assert job.locked_at is None
        assert job.locked_by is None
        assert job.attempts == 1  # no se penaliza: el worker no falló, se murió
        assert job.available_at <= job_queue._db_now(s)  # tomable YA
        assert s.get(Recording, recording_id).status == RecordingStatus.QUEUED

    # y el worker sano que estaba al lado lo termina en el mismo ciclo
    assert revived.run_once() is True
    with SessionFactory() as s:
        assert s.get(Recording, recording_id).status == RecordingStatus.COMPLETED


def test_recover_at_startup_no_le_roba_el_job_a_otro_worker_vivo(
    SessionFactory, storage, settings
):
    """El worker de otra máquina puede estar transcribiendo ahora mismo: su
    job solo se recupera cuando el lease vence, nunca por arrancar yo."""
    setup = SessionFactory()
    _recording_id, job_id = _seed_recording(setup, storage, seconds=1.0)
    setup.close()

    otro = _worker(SessionFactory, storage, settings, worker_id="worker-otra-maquina")
    assert otro._claim() is not None

    yo = _worker(SessionFactory, storage, settings, worker_id="worker-test")
    yo.recover_at_startup()

    with SessionFactory() as s:
        job = s.get(ProcessingJob, job_id)
        assert job.status == JobStatus.RUNNING
        assert job.locked_by == "worker-otra-maquina"


def test_el_progreso_renueva_el_lease_durante_el_proceso(SessionFactory, storage, settings):
    """Un trabajo largo y legítimo no puede confundirse con uno abandonado:
    cada trozo reportado corre `locked_at` hacia adelante."""
    setup = SessionFactory()
    _recording_id, job_id = _seed_recording(setup, storage, seconds=1.0)
    setup.close()

    leases: list = []

    class LeaseProbeEngine:
        """Envejece el lease a mano ANTES de reportar el trozo y mira si el
        reporte lo devuelve al presente."""

        def transcribe(self, audio_path, *, checkpoint_dir=None, on_progress=None) -> SttResult:
            with SessionFactory() as s:
                job = s.get(ProcessingJob, job_id)
                job.locked_at = job_queue._db_now(s) - timedelta(seconds=1700)
                s.commit()
                leases.append(job.locked_at)

            on_progress(ChunkProgress(
                chunks_done=1, chunks_total=2, seconds_done=0.5,
                seconds_total=1.0, elapsed_seconds=0.05,
            ))

            with SessionFactory() as s:
                leases.append(s.get(ProcessingJob, job_id).locked_at)

            return SttResult(
                segments=[SttSegment(start=0.0, end=1.0, text="hola", confidence=1.0)],
                language="es", model="probe", processing_time_seconds=0.1,
                audio_duration_seconds=1.0, chunks=[{}],
            )

    worker = _worker(SessionFactory, storage, settings, engine=LeaseProbeEngine())
    worker.run_once()

    envejecido, renovado = leases
    assert renovado > envejecido
    # el lease volvió al presente: ya no lo alcanza el corte de media hora
    assert (renovado - envejecido).total_seconds() > 1600


# --------------------------------------------------------------------------
# la BD se cae: el worker es un bucle, no un script
# --------------------------------------------------------------------------
class _FlakySessionFactory:
    """`sessionmaker` que simula una Postgres caída: revienta en las llamadas
    cuyo número de orden pasa de `works_for`, hasta que `heal()` la revive."""

    def __init__(self, inner: sessionmaker[Session], *, works_for: int = 0) -> None:
        self.inner = inner
        self.works_for = works_for
        self.calls = 0

    def heal(self) -> None:
        self.works_for = 10_000

    def __call__(self) -> Session:
        self.calls += 1
        if self.calls > self.works_for:
            raise OperationalError("SELECT 1", None, ConnectionError("la base no responde"))
        return self.inner()


def test_worker_sobrevive_a_la_bd_caida_al_reclamar(SessionFactory, storage, settings):
    """EL FALLO: `_claim()` llamaba a `claim_job` sin red. Con Postgres caído,
    la excepción subía por run_once/run_forever y MATABA el proceso."""
    setup = SessionFactory()
    recording_id, _job_id = _seed_recording(setup, storage, seconds=1.0)
    setup.close()

    flaky = _FlakySessionFactory(SessionFactory, works_for=0)
    worker = _worker(flaky, storage, settings)

    # con la BD caída: no revienta, solo dice "no hubo trabajo" y sigue vivo
    assert worker.run_once() is False
    assert worker.run_once() is False
    assert flaky.calls >= 2  # de verdad lo reintentó, no se quedó mudo

    # la base vuelve: el mismo worker, sin reiniciar, retoma el trabajo
    flaky.heal()
    assert worker.run_once() is True
    with SessionFactory() as s:
        assert s.get(Recording, recording_id).status == RecordingStatus.COMPLETED


def test_worker_sobrevive_a_que_la_bd_se_caiga_a_media_ejecucion(
    SessionFactory, storage, settings
):
    """Peor caso del mismo fallo: la BD se va DESPUÉS del reclamo, así que el
    `except` intenta `fail_job(...)` —que también necesita la BD— y esa segunda
    excepción tumbaba el proceso sin red alguna."""
    setup = SessionFactory()
    _recording_id, job_id = _seed_recording(setup, storage, seconds=1.0)
    setup.close()

    # 1 llamada buena (el reclamo) y de ahí en adelante todo truena
    flaky = _FlakySessionFactory(SessionFactory, works_for=1)
    worker = _worker(flaky, storage, settings)

    assert worker.run_once() is True  # tomó trabajo y NO se murió al fallar

    # el job quedó RUNNING con mi firma: al revivir, la recuperación por
    # identidad lo devuelve a la cola. Eso es lo que hace tolerable perder el
    # `fail_job` — nada se queda colgado para siempre.
    flaky.heal()
    with SessionFactory() as s:
        job = s.get(ProcessingJob, job_id)
        assert job.status == JobStatus.RUNNING
        assert job.locked_by == "worker-test"

    worker.recover_at_startup()
    with SessionFactory() as s:
        assert s.get(ProcessingJob, job_id).status == JobStatus.PENDING


def test_recover_at_startup_no_revienta_con_la_bd_caida(SessionFactory, storage, settings):
    """El worker arranca junto con Postgres: que la base no esté lista todavía
    no puede dejarlo en crash-loop antes de la primera vuelta del bucle."""
    flaky = _FlakySessionFactory(SessionFactory, works_for=0)
    _worker(flaky, storage, settings).recover_at_startup()  # no lanza


# --------------------------------------------------------------------------
# el latido es telemetría: que falle no puede tumbar el proceso
# --------------------------------------------------------------------------
def test_latido_que_falla_no_tumba_el_worker(
    SessionFactory, storage, settings, monkeypatch, caplog
):
    """EL FALLO: `run_forever` escribía el latido sin red ANTES del ciclo. Con
    el volumen en solo-lectura (o el disco lleno) el worker moría con OSError
    sin siquiera intentar tomar trabajo."""
    setup = SessionFactory()
    recording_id, _job_id = _seed_recording(setup, storage, seconds=1.0)
    setup.close()

    intentos = {"n": 0}

    def _disco_de_solo_lectura(_data_dir: Path) -> None:
        intentos["n"] += 1
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr("tardic.worker.main.write_heartbeat", _disco_de_solo_lectura)

    worker = _worker(SessionFactory, storage, settings)
    # los handlers de señal se instalarían sobre el proceso de pytest: fuera.
    monkeypatch.setattr(worker, "_install_signal_handlers", lambda: None)

    corridas = {"n": 0}
    real_run_once = worker.run_once

    # Se para en cuanto una vuelta procesa algo, con un tope por si nunca lo
    # hace. No es "esperar a que salga bien": es que `_claim` ahora se traga los
    # errores de base de datos a propósito (el worker debe sobrevivir a que
    # Postgres se caiga), así que una vuelta puede volver vacía por un fallo
    # transitorio de conexión — y con la suite completa peleando por conexiones,
    # pasa. Fijar UNA sola vuelta hacía fallar este test de forma intermitente
    # por un motivo que no tiene nada que ver con lo que verifica.
    def _vueltas_hasta_procesar() -> bool:
        corridas["n"] += 1
        resultado = real_run_once()
        if resultado or corridas["n"] >= 10:
            worker.request_stop()
        return resultado

    monkeypatch.setattr(worker, "run_once", _vueltas_hasta_procesar)

    with caplog.at_level(logging.ERROR, logger="tardic.worker"):
        worker.run_forever()  # antes: OSError en la primera línea

    assert corridas["n"] >= 1  # llegó a trabajar, no murió antes del ciclo
    assert intentos["n"] >= 2  # y siguió latiendo (o intentándolo) durante el ciclo
    with SessionFactory() as s:
        assert s.get(Recording, recording_id).status == RecordingStatus.COMPLETED

    # se registra UNA vez por racha, no una línea idéntica por cada latido
    latido_roto = [r for r in caplog.records if "latido" in r.getMessage()]
    assert len(latido_roto) == 1


# --------------------------------------------------------------------------
# limpieza: el WAV derivado y los checkpoints no se quedan para siempre
# --------------------------------------------------------------------------
class _CheckpointingFakeEngine:
    """`FakeEngine` que además deja checkpoints en disco, como el motor real
    (RF-10): son justo los archivos que hay que barrer al terminar."""

    def __init__(self, inner: FakeEngine) -> None:
        self.inner = inner

    def transcribe(self, audio_path, *, checkpoint_dir=None, on_progress=None) -> SttResult:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (checkpoint_dir / f"chunk-{i:02d}.json").write_text("{}", encoding="utf-8")
        return self.inner.transcribe(
            audio_path, checkpoint_dir=checkpoint_dir, on_progress=on_progress
        )


def test_al_completar_se_borran_el_wav_y_los_checkpoints_y_se_conserva_el_original(
    SessionFactory, storage, settings
):
    """EL FALLO: cada transcripción exitosa dejaba `audio.wav` (~345 MB por
    cada 3 h de audio) y todos los `chunks/chunk-NN.json` en el volumen para
    siempre. Lo que SÍ se conserva: el original (RF-04) y el transcript.txt."""
    setup = SessionFactory()
    recording_id, _job_id = _seed_recording(setup, storage, seconds=2.0)
    setup.close()

    original = storage.original_path(recording_id, suffix=".wav")
    assert original.exists()

    worker = _worker(
        SessionFactory, storage, settings,
        engine=_CheckpointingFakeEngine(FakeEngine(chunk_seconds=1.0)),
    )
    assert worker.run_once() is True

    with SessionFactory() as s:
        assert s.get(Recording, recording_id).status == RecordingStatus.COMPLETED

    assert not storage.wav_path(recording_id).exists()
    assert not storage.chunks_dir(recording_id).exists()
    assert original.exists()  # RF-04: el audio original se persiste
    assert storage.transcript_txt_path(recording_id).exists()


def test_keep_intermediate_files_conserva_todo_para_depurar(SessionFactory, storage, settings):
    settings = settings.model_copy(update={"keep_intermediate_files": True})
    setup = SessionFactory()
    recording_id, _job_id = _seed_recording(setup, storage, seconds=2.0)
    setup.close()

    worker = _worker(
        SessionFactory, storage, settings,
        engine=_CheckpointingFakeEngine(FakeEngine(chunk_seconds=1.0)),
    )
    assert worker.run_once() is True

    assert storage.wav_path(recording_id).exists()
    assert storage.chunks_dir(recording_id).exists()


def test_un_fallo_al_borrar_no_rompe_un_trabajo_ya_terminado(
    SessionFactory, storage, settings, monkeypatch
):
    """La limpieza corre DESPUÉS de marcar COMPLETED: si el borrado truena
    (permisos, disco raro) se registra y ya — no puede deshacer una
    transcripción buena ni provocar un reintento."""
    setup = SessionFactory()
    recording_id, job_id = _seed_recording(setup, storage, seconds=1.0)
    setup.close()

    worker = _worker(SessionFactory, storage, settings)

    def _no_se_puede(*_a, **_k):
        raise PermissionError("el volumen no deja borrar")

    # se rompe solo el borrado, no la escritura del transcript.txt
    monkeypatch.setattr(Path, "unlink", _no_se_puede)

    assert worker.run_once() is True

    with SessionFactory() as s:
        assert s.get(Recording, recording_id).status == RecordingStatus.COMPLETED
        assert s.get(ProcessingJob, job_id).status == JobStatus.DONE
