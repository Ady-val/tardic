"""El bucle del worker.

Toma UN job a la vez — nunca dos: el VPS comparte CPU con producción (doc 02
§10) — y lo corre por el pipeline PREPROCESS → TRANSCRIBE → MERGE → PERSIST,
actualizando `stage` y `progress` del job conforme avanza.

El motor de transcripción se inyecta (`engine_factory`), nunca se importa
fijo: así los tests corren con un `FakeEngine` en segundos, y cambiar de motor
(RNF-05) es escribir otra factory, no tocar este archivo.

Diarización: `Stage` (en `models.py`) incluye `DIARIZE`, pero esta entrega
NO la implementa — no hay contrato (protocolo) para un diarizador equivalente
al `SttEngine` de `core/stt.py`, y las instrucciones de este agente fijan el
pipeline como PREPROCESS → TRANSCRIBE → MERGE → PERSIST. `ProcessingJob.diarize`
se respeta como bandera (queda guardada), pero hoy no dispara nada; los
segmentos se guardan sin `speaker_id`. Repórtalo si el MVP la necesita ya.
"""
from __future__ import annotations

import json
import logging
import shutil
import signal
import socket
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from sqlalchemy.orm import Session, sessionmaker

from .. import repository
from ..config import Settings, get_settings
from ..core.stt import ChunkProgress, SttEngine
from ..db import get_sessionmaker
from ..models import JobStatus, ProcessingJob, Recording, RecordingStatus, Stage
from ..storage import Storage
from . import queue as job_queue

logger = logging.getLogger("tardic.worker")

EngineFactory = Callable[[], SttEngine]
PreprocessFn = Callable[[Path, Path], None]

# Latido para `/health` (`HealthOut.worker_seen_seconds_ago`, contrato en
# schemas.py). DECISIÓN: no se agrega una tabla nueva — `models.py` fija las
# cinco entidades y su docstring prohíbe inventar tablas. Se usa un archivo
# plano, hermano de `audio/` bajo `data_dir`, con la marca de tiempo UTC en
# ISO-8601. El endpoint de salud (agente C) lee ese archivo y calcula
# `now - mtime` (o parsea el contenido; da lo mismo, la escritura es atómica).
HEARTBEAT_FILENAME = "worker_heartbeat.txt"


def default_worker_id() -> str:
    """Identidad estable de este worker: el hostname.

    En Docker el hostname es el id corto del contenedor, y **sobrevive a los
    reinicios del mismo contenedor** (`restart: unless-stopped` reinicia el
    proceso, no recrea el contenedor). Justo lo que hace falta para que un
    worker que revive reconozca los jobs que traía cuando lo mataron. Fuera de
    Docker es el nombre de la máquina, que también sirve.
    """
    return socket.gethostname()


class _ShutdownSignal(Exception):
    """Señal interna: se lanza dentro de `on_progress` para desenrollar el
    pipeline en una frontera de trozo segura cuando llegó SIGTERM/SIGINT.

    No es un error del trabajo — por eso el manejador la trata distinto a
    cualquier otra excepción: el job se libera con `release_job` (sin
    penalizar `attempts`), no con `fail_job`.
    """


class StructuredFormatter(logging.Formatter):
    """Log estructurado (doc 03 §14): recording_id, job_id, stage, duración,
    intento — como JSON de una línea, fácil de grepear o de mandar a un
    colector sin parsear texto libre."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("recording_id", "job_id", "stage", "duration_seconds", "attempt"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    root = logging.getLogger("tardic")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False


def default_preprocess(src: Path, dst: Path) -> None:
    """PREPROCESS: castea a WAV PCM 16 kHz mono.

    Reusa `core.audio.to_wav16k_mono` (agente A, AGENTS.md: "ya existe y
    funciona, no lo reinventes") con import perezoso — igual que el motor,
    para que este archivo se pueda importar en los tests sin arrastrar ese
    módulo, y para que inyectar otra implementación (`preprocess=`) siga
    siendo un cambio de una línea, no de arquitectura.
    """
    from ..core.audio import AudioError, to_wav16k_mono

    try:
        to_wav16k_mono(src, dst)
    except AudioError as exc:
        # AudioError trae el stderr de ffmpeg recortado, que puede incluir la
        # ruta del archivo temporal: eso se va al log (detalle completo), no
        # al usuario (doc 03 §15 — nada de rutas del servidor).
        logger.error("ffmpeg no pudo convertir el audio: %s", exc, extra={"stage": "PREPROCESS"})
        raise RuntimeError("no se pudo preparar el audio para transcribir") from exc


def write_heartbeat(data_dir: Path) -> None:
    """Escritura atómica: si el proceso muere a medio escribir, el archivo
    previo (o ninguno) queda intacto — nunca un heartbeat corrupto a medias."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / HEARTBEAT_FILENAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    tmp.replace(path)


def _user_facing_message(exc: Exception) -> str:
    """Mensaje que ve el usuario final: sin rutas del servidor ni trazas
    (doc 03 §15). El detalle completo ya se fue al log vía `logger.exception`."""
    if isinstance(exc, RuntimeError) and str(exc):
        return str(exc)
    return "no se pudo terminar de transcribir este audio"


class Worker:
    """Un worker de un solo job a la vez.

    `session_factory` produce sesiones cortas e independientes — nunca se
    guarda una sesión abierta durante todo el pipeline. `engine_factory` se
    llama una vez por job (permite, por ejemplo, cargar el modelo una sola
    vez si la factory memoiza internamente).
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        engine_factory: EngineFactory,
        storage: Storage,
        settings: Settings,
        preprocess: PreprocessFn = default_preprocess,
        worker_id: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.engine_factory = engine_factory
        self.storage = storage
        self.settings = settings
        self.preprocess = preprocess
        # Inyectable solo para los tests: en producción siempre el hostname.
        self.worker_id = worker_id or default_worker_id()
        self._stop = False
        # Para no inundar el log si el latido falla en cada vuelta del bucle.
        self._heartbeat_broken = False

    # --- apagado limpio ---
    def request_stop(self, *_args: object) -> None:
        self._stop = True

    def _install_signal_handlers(self) -> None:
        def _handle(signum: int, frame: FrameType | None) -> None:
            logger.info("señal de apagado recibida (%s), liberando job en curso si lo hay", signum)
            self.request_stop()

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)

    # --- latido ---
    def write_heartbeat(self) -> None:
        """Latido a prueba de disco: un fallo aquí NUNCA tumba el worker.

        El latido es telemetría para `/health`, no parte del trabajo. Con el
        volumen montado en solo-lectura (o el disco lleno) esto lanzaba
        `OSError` antes siquiera de intentar tomar un job, y el contenedor
        entraba en crash-loop transcribiendo cero audios. Se registra una sola
        vez por racha para no llenar el log con la misma línea cada 5 s.
        """
        try:
            write_heartbeat(self.settings.data_dir)
        except OSError as exc:
            if not self._heartbeat_broken:
                self._heartbeat_broken = True
                logger.error(
                    "no se pudo escribir el latido (%s); el worker sigue trabajando, "
                    "pero /health lo verá como ausente",
                    exc,
                )
        else:
            if self._heartbeat_broken:
                self._heartbeat_broken = False
                logger.info("el latido volvió a escribirse")

    # --- arranque ---
    def recover_at_startup(self) -> None:
        """RF-10: lo primero que corre el worker al arrancar.

        Tres redes, de la más precisa a la más burda:
        1. mis propios jobs `RUNNING` (los dejé colgados al morir) → PENDING YA;
        2. jobs de cualquiera con el lease vencido → PENDING;
        3. `Recording`s en PROCESSING sin job `RUNNING` detrás.

        Si la base no está lista todavía (el worker arranca junto con Postgres)
        no se cae: se logea y el bucle lo reintentará en la siguiente vuelta.
        """
        try:
            with self.session_factory() as session:
                own = job_queue.recover_own_jobs(session, worker_id=self.worker_id)
                zombies = job_queue.recover_zombie_jobs(
                    session, lease_timeout_seconds=self.settings.lease_timeout_seconds
                )
                stuck = job_queue.recover_stuck_processing_jobs(session)
                session.commit()
        except Exception:  # noqa: BLE001 — la BD puede no estar arriba todavía
            logger.exception("no se pudo recuperar el estado al arrancar; se sigue de todos modos")
            return
        if own:
            logger.warning("jobs propios recuperados al arrancar (worker reiniciado): %d", own)
        if zombies:
            logger.warning("jobs zombis recuperados al arrancar: %d", zombies)
        if stuck:
            logger.warning("recordings huérfanos en PROCESSING corregidos: %d", stuck)

    # --- bucle principal ---
    def run_forever(self) -> None:
        self._install_signal_handlers()
        self.recover_at_startup()
        self.write_heartbeat()
        while not self._stop:
            processed = self.run_once()
            self.write_heartbeat()
            if not processed and not self._stop:
                time.sleep(self.settings.poll_interval_seconds)
        logger.info("worker detenido limpiamente")

    def run_once(self) -> bool:
        """Procesa como máximo un job. Devuelve True si tomó trabajo."""
        claimed = self._claim()
        if claimed is None:
            return False
        recording_id, job_id, attempt = claimed
        log_extra = {"recording_id": str(recording_id), "job_id": str(job_id), "attempt": attempt}
        t0 = time.monotonic()
        try:
            self._process_job(recording_id, job_id)
            logger.info(
                "job completado",
                extra={**log_extra, "duration_seconds": round(time.monotonic() - t0, 1)},
            )
        except _ShutdownSignal:
            # Si la BD se cayó justo aquí, el job se queda RUNNING con mi firma:
            # al revivir, `recover_own_jobs` lo devuelve a la cola. Por eso
            # perder este release es molesto, no fatal.
            if self._in_db(lambda s: job_queue.release_job(s, job_id)):
                logger.info("job liberado por apagado limpio", extra=log_extra)
            else:
                logger.error("no se pudo liberar el job al apagar", extra=log_extra)
        except Exception as exc:  # noqa: BLE001 — cualquier falla del pipeline se atrapa aquí
            logger.exception("job falló", extra=log_extra)
            # El mensaje se saca AQUÍ, no dentro del lambda: Python borra el
            # nombre `exc` al salir del `except`, y una closure que lo mire
            # después sería una bomba de tiempo (`NameError`).
            message = _user_facing_message(exc)
            # Registrar el fallo también necesita la BD. Si es JUSTO la BD la
            # que se cayó, esta llamada revienta — y antes esa segunda
            # excepción subía por run_once/run_forever y mataba el proceso.
            # Ahora no: el lease vencido (o el arranque siguiente) recupera el
            # job solo, que es exactamente para lo que existe.
            if not self._in_db(
                lambda s: job_queue.fail_job(
                    s,
                    job_id,
                    error=message,
                    max_attempts=self.settings.max_attempts,
                )
            ):
                logger.error("no se pudo registrar el fallo del job en la BD", extra=log_extra)
        return True

    def _in_db(self, work: Callable[[Session], None]) -> bool:
        """Corre `work` en una transacción corta. Devuelve si salió bien.

        Un worker es un bucle, no un script: que Postgres se vaya un rato no
        puede tumbar el proceso. Se logea y se reintenta en la vuelta
        siguiente.
        """
        try:
            with self.session_factory() as session:
                work(session)
                session.commit()
        except Exception:  # noqa: BLE001 — cualquier problema de BD, no solo OperationalError
            logger.exception("operación de base de datos fallida")
            return False
        return True

    def _claim(self) -> tuple[uuid.UUID, uuid.UUID, int] | None:
        """Toma un job, o `None` si no hay ninguno **o si la BD falló**.

        Devolver `None` ante un error deja que `run_forever` duerma el poll y
        vuelva a intentar. Antes esta excepción subía sin red y mataba el
        proceso entero cada vez que Postgres hipaba.
        """
        try:
            with self.session_factory() as session:
                job = job_queue.claim_job(session, worker_id=self.worker_id)
                if job is None:
                    session.commit()
                    return None
                recording_id, job_id, attempt = job.recording_id, job.id, job.attempts
                session.commit()  # suelta el lock de la fila cuanto antes
        except Exception:  # noqa: BLE001 — la BD puede estar caída; no es fatal
            logger.exception("no se pudo reclamar trabajo (¿base de datos caída?); se reintenta")
            return None
        return recording_id, job_id, attempt

    def _set_stage(self, job_id: uuid.UUID, stage: Stage) -> None:
        with self.session_factory() as session:
            job = session.get(ProcessingJob, job_id)
            if job is not None:
                job.stage = stage
                session.commit()

    def _make_on_progress(self, job_id: uuid.UUID) -> Callable[[ChunkProgress], None]:
        def _on_progress(progress: ChunkProgress) -> None:
            if self._stop:
                # Frontera segura entre trozos: aquí sí podemos parar sin
                # perder el checkpoint del motor (RF-10) ni dejar el job a
                # medias en un estado raro.
                raise _ShutdownSignal
            payload = {
                "chunks_done": progress.chunks_done,
                "chunks_total": progress.chunks_total,
                "percent": progress.percent,
                "eta_seconds": progress.eta_seconds,
            }
            # Guarda el avance Y renueva el lease: reportar progreso ES la
            # señal de vida. Si la BD hipa justo aquí no se pierde el trabajo
            # —el motor sigue con su checkpoint—, solo un refresco de progreso.
            self._in_db(lambda s: job_queue.record_progress(s, job_id, payload))
            self.write_heartbeat()

        return _on_progress

    def _process_job(self, recording_id: uuid.UUID, job_id: uuid.UUID) -> None:
        with self.session_factory() as session:
            recording = session.get(Recording, recording_id)
            if recording is None:
                raise RuntimeError(f"recording {recording_id} no existe")
            original_path = self.storage.absolute(recording.storage_path)

        self.storage.ensure_dir(recording_id)
        wav_path = self.storage.wav_path(recording_id)
        checkpoint_dir = self.storage.chunks_dir(recording_id)

        self._set_stage(job_id, Stage.PREPROCESS)
        logger.info(
            "preprocesando",
            extra={"recording_id": str(recording_id), "job_id": str(job_id), "stage": "PREPROCESS"},
        )
        self.preprocess(original_path, wav_path)

        self._set_stage(job_id, Stage.TRANSCRIBE)
        logger.info(
            "transcribiendo",
            extra={"recording_id": str(recording_id), "job_id": str(job_id), "stage": "TRANSCRIBE"},
        )
        engine = self.engine_factory()
        on_progress = self._make_on_progress(job_id)
        result = engine.transcribe(wav_path, checkpoint_dir=checkpoint_dir, on_progress=on_progress)

        self._set_stage(job_id, Stage.MERGE)
        segments = [
            repository.SegmentData(
                start_time=seg.start,
                end_time=seg.end,
                text=seg.text,
                speaker_label=None,  # sin diarización en esta entrega (ver docstring del módulo)
                confidence=seg.confidence,
            )
            for seg in result.segments
        ]

        self._set_stage(job_id, Stage.PERSIST)
        logger.info(
            "persistiendo",
            extra={"recording_id": str(recording_id), "job_id": str(job_id), "stage": "PERSIST"},
        )
        # El .txt se escribe ANTES del commit de la BD: si escribir falla,
        # el job todavía no quedó marcado como terminado y se puede reintentar.
        self.storage.transcript_txt_path(recording_id).write_text(result.text, encoding="utf-8")

        with self.session_factory() as session:
            repository.save_transcript_result(
                session,
                recording_id=recording_id,
                text=result.text,
                language=result.language,
                model=result.model,
                processing_time_seconds=result.processing_time_seconds,
                segments=segments,
            )
            recording = session.get(Recording, recording_id)
            if recording is not None:
                recording.status = RecordingStatus.COMPLETED
                recording.duration_seconds = result.audio_duration_seconds
                recording.language = recording.language or result.language
                recording.ended_at = datetime.now(UTC)
            job = session.get(ProcessingJob, job_id)
            if job is not None:
                job.status = JobStatus.DONE
                job.stage = Stage.PERSIST
                job.completed_at = datetime.now(UTC)
                n_chunks = len(result.chunks)
                job.progress = {
                    "chunks_done": n_chunks,
                    "chunks_total": n_chunks,
                    "percent": 100,
                    "eta_seconds": 0,
                }
            session.commit()

        # Hasta aquí el trabajo ya está terminado y persistido: lo que sigue es
        # limpieza y no puede hacerlo fracasar.
        self._cleanup_intermediate(recording_id)

    def _cleanup_intermediate(self, recording_id: uuid.UUID) -> None:
        """Borra lo derivado que ya no sirve: el WAV de 16 kHz y los
        checkpoints por trozo.

        El WAV pesa ~345 MB por cada 3 h de audio y los checkpoints se suman;
        sin esto, cada transcripción exitosa dejaba esa basura en el volumen
        para siempre (solo desaparecía si el usuario hacía DELETE). En un VPS
        con 25 contenedores de producción encima, eso llena el disco.

        Lo que SÍ se conserva: `original.<ext>` (RF-04 exige persistir el audio
        original) y `transcript.txt` (el entregable de descarga). Con
        `TARDIC_KEEP_INTERMEDIATE_FILES=true` no se borra nada, para depurar.

        Tolerante a fallos por diseño: el trabajo ya está terminado y marcado
        COMPLETED, así que un error de borrado se registra y ya — no puede
        deshacer una transcripción buena ni provocar un reintento.
        """
        if self.settings.keep_intermediate_files:
            return
        log_extra = {"recording_id": str(recording_id)}
        try:
            self.storage.wav_path(recording_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("no se pudo borrar el WAV derivado: %s", exc, extra=log_extra)
        chunks_dir = self.storage.chunks_dir(recording_id)
        try:
            if chunks_dir.exists():
                shutil.rmtree(chunks_dir)
        except OSError as exc:
            logger.warning("no se pudo borrar el directorio de checkpoints: %s", exc,
                           extra=log_extra)


def _default_engine_factory() -> SttEngine:
    """Composition root del worker real: es el ÚNICO lugar de todo `worker/`
    que conoce el motor concreto, y a propósito con import perezoso (solo se
    ejecuta al correr `main()`, nunca al importar el módulo ni en los tests).

    El resto del pipeline (`Worker`, `run_once`, `_process_job`...) solo
    conoce `SttEngine` (el Protocol de `core/stt.py`) — esta función es el
    único empalme necesario para que `tardic-worker` arranque con un motor
    de verdad, tomando los parámetros de `Settings` (única fuente de
    verdad de configuración, doc de `config.py`).
    """
    from ..core.faster_whisper_engine import FasterWhisperEngine

    settings = get_settings()
    return FasterWhisperEngine(
        settings.stt_model,
        compute_type=settings.stt_compute_type,
        threads=settings.stt_threads,
        language=settings.stt_language,
        chunk_minutes=settings.chunk_minutes,
        vad=settings.vad,
    )


def main() -> None:
    """Punto de entrada de `tardic-worker` (ver `pyproject.toml`)."""
    configure_logging()
    settings = get_settings()
    storage = Storage(settings.data_dir)
    worker = Worker(
        session_factory=get_sessionmaker(),
        engine_factory=_default_engine_factory,
        storage=storage,
        settings=settings,
    )
    # El id queda en el log a propósito: es la firma que se ve en
    # `processing_jobs.locked_by` al diagnosticar un job atorado.
    logger.info("worker arrancando (id=%s)", worker.worker_id)
    worker.run_forever()


if __name__ == "__main__":
    main()
