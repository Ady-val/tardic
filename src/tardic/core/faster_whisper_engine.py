"""Implementación real de `SttEngine` sobre faster-whisper.

Reusa tal cual la lógica medida en `benchmark/bench_chunked.py` (25/08):
corte en silencio real, modelo cargado UNA sola vez y de forma perezosa,
checkpoint por trozo reanudable (RF-10), timestamps devueltos a la línea de
tiempo del audio completo (AGENTS.md regla 6).
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path

import psutil

from tardic.core.audio import cut_points, probe_duration, slice_audio
from tardic.core.stt import ChunkProgress, ProgressCallback, SttResult, SttSegment


class _RssMonitor:
    """Mide el RSS pico del proceso actual (y sus hijos) durante un `with`.

    Versión mínima del `Monitor` de `benchmark/bench.py`: aquí solo hace
    falta el pico de memoria por trozo (doc 03 §14), no el detalle de CPU.
    """

    def __init__(self, interval: float = 0.5) -> None:
        self._proc = psutil.Process(os.getpid())
        self._interval = interval
        self.peak_rss = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _procs(self) -> list[psutil.Process]:
        try:
            return [self._proc, *self._proc.children(recursive=True)]
        except psutil.Error:
            return []

    def _run(self) -> None:
        while not self._stop.is_set():
            rss = 0
            for p in self._procs():
                try:
                    rss += p.memory_info().rss
                except psutil.Error:
                    pass
            self.peak_rss = max(self.peak_rss, rss)
            self._stop.wait(self._interval)

    def __enter__(self) -> _RssMonitor:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


class FasterWhisperEngine:
    """`SttEngine` sobre faster-whisper, procesando por trozos.

    RNF-05 (sustituibilidad): el worker solo conoce este objeto a través del
    Protocol `SttEngine` — cambiar de motor es escribir otra clase, no tocar
    esta.
    """

    def __init__(
        self,
        model: str = "large-v3-turbo",
        *,
        compute_type: str = "int8",
        threads: int = 0,
        language: str | None = "es",
        chunk_minutes: float = 15.0,
        vad: bool = True,
    ) -> None:
        self.model_name = model
        self.compute_type = compute_type
        # 0 = núcleos físicos disponibles (config.py lo documenta así).
        self.threads = threads or (psutil.cpu_count(logical=False) or 4)
        self.language = language
        self.chunk_minutes = chunk_minutes
        self.vad = vad
        self._model = None  # carga perezosa: si todo viene de checkpoint, no hace falta

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type=self.compute_type,
                cpu_threads=self.threads,
            )
        return self._model

    def transcribe(
        self,
        audio_path: Path,
        *,
        checkpoint_dir: Path | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> SttResult:
        audio_path = Path(audio_path)
        total_s = probe_duration(audio_path)
        chunk_s = self.chunk_minutes * 60

        own_tmp: tempfile.TemporaryDirectory | None = None
        if checkpoint_dir is None:
            own_tmp = tempfile.TemporaryDirectory(prefix="tardic-stt-")
            ckpt_dir = Path(own_tmp.name)
        else:
            ckpt_dir = Path(checkpoint_dir)
            ckpt_dir.mkdir(parents=True, exist_ok=True)

        try:
            return self._transcribe_chunks(audio_path, total_s, chunk_s, ckpt_dir, on_progress)
        finally:
            if own_tmp is not None:
                own_tmp.cleanup()

    def _signature(self) -> dict:
        """Con qué parámetros se produjo un trozo.

        Se guarda dentro de cada checkpoint para poder invalidarlo. Sin esto,
        el único criterio de reúso es el NÚMERO de trozo, y basta con cambiar
        `chunk_minutes` entre dos intentos (algo que se hace justo después de
        un OOM) para que el trozo 0 viejo cubra 0–300 s y los nuevos recalculen
        desde 95 s: el resultado repite un pedazo del audio y lo entrega como
        transcripción buena.
        """
        return {
            "model": self.model_name,
            "compute_type": self.compute_type,
            "chunk_minutes": self.chunk_minutes,
            "language": self.language,
            "vad": self.vad,
        }

    @staticmethod
    def _checkpoint_matches(ckpt: Path, signature: dict, start: float, end: float) -> bool:
        """¿Este checkpoint corresponde de verdad al trozo que vamos a pedir?

        Además de la firma de parámetros se comparan los límites: el rango
        guardado tiene que ser el mismo que se va a transcribir ahora, con una
        tolerancia mínima por el redondeo con que se escribieron.
        """
        try:
            meta = json.loads(ckpt.read_text(encoding="utf-8"))["meta"]
        except (OSError, ValueError, KeyError):
            return False
        if meta.get("signature") != signature:
            return False
        return abs(meta.get("start", -1) - round(start, 1)) < 0.15 and \
            abs(meta.get("end", -1) - round(end, 1)) < 0.15

    def _transcribe_chunks(
        self,
        audio_path: Path,
        total_s: float,
        chunk_s: float,
        ckpt_dir: Path,
        on_progress: ProgressCallback | None,
    ) -> SttResult:
        points, _noise_db, _hits = cut_points(total_s, chunk_s, audio_path)
        bounds = [0.0, *points, total_s]
        ranges = list(zip(bounds[:-1], bounds[1:], strict=False))

        all_segments: list[SttSegment] = []
        chunk_meta: list[dict] = []
        language_detected = self.language
        t_start = time.perf_counter()

        signature = self._signature()
        seconds_computed = 0.0

        for i, (start, end) in enumerate(ranges):
            ckpt = ckpt_dir / f"chunk-{i:02d}.json"
            if ckpt.exists() and self._checkpoint_matches(ckpt, signature, start, end):
                # RF-10: un trozo ya calculado no se recalcula.
                data = json.loads(ckpt.read_text(encoding="utf-8"))
            else:
                data = self._run_chunk(audio_path, ckpt_dir, i, start, end)
                ckpt.write_text(
                    json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                # Solo este camino consume tiempo: es el que sirve para medir
                # el ritmo real y estimar lo que falta.
                seconds_computed += end - start

            meta = data["meta"]
            chunk_meta.append(meta)
            for seg in data["segments"]:
                all_segments.append(SttSegment(
                    start=seg["start"], end=seg["end"], text=seg["text"],
                    confidence=seg.get("confidence"),
                ))
            language_detected = meta.get("language") or language_detected

            if on_progress is not None:
                on_progress(ChunkProgress(
                    chunks_done=i + 1, chunks_total=len(ranges),
                    seconds_done=end, seconds_total=total_s,
                    elapsed_seconds=time.perf_counter() - t_start,
                    seconds_computed=seconds_computed,
                ))

        processing_time = sum(m["transcribe_seconds"] for m in chunk_meta)
        return SttResult(
            segments=all_segments, language=language_detected, model=self.model_name,
            processing_time_seconds=round(processing_time, 3),
            audio_duration_seconds=total_s, chunks=chunk_meta,
        )

    def _run_chunk(
        self, audio_path: Path, ckpt_dir: Path, index: int, start: float, end: float
    ) -> dict:
        model = self._ensure_model()
        tmp = ckpt_dir / f"chunk-{index:02d}.wav"
        slice_audio(audio_path, tmp, start, end)
        t0 = time.perf_counter()
        try:
            with _RssMonitor() as mon:
                segs_iter, info = model.transcribe(
                    str(tmp), language=self.language, beam_size=5, vad_filter=self.vad,
                    vad_parameters=dict(min_silence_duration_ms=500) if self.vad else None,
                )
                segments = [
                    {
                        # los timestamps vuelven a la línea de tiempo del audio completo
                        "start": round(s.start + start, 2), "end": round(s.end + start, 2),
                        "text": s.text.strip(),
                        "confidence": round(min(1.0, max(0.0, math.exp(s.avg_logprob))), 3),
                    }
                    for s in segs_iter
                ]
        finally:
            tmp.unlink(missing_ok=True)
        dt = time.perf_counter() - t0

        meta = {
            "chunk": index, "start": round(start, 1), "end": round(end, 1),
            "duration_seconds": round(end - start, 1), "transcribe_seconds": round(dt, 3),
            "rtf": round(dt / max(end - start, 1e-6), 3),
            "peak_rss_mb": round(mon.peak_rss / 1e6, 1),
            "language": info.language, "segments_count": len(segments),
            # Con qué parámetros se calculó: sin esto, un reintento con otro
            # modelo o con otro tamaño de trozo reusa esto a ciegas.
            "signature": self._signature(),
        }
        return {"meta": meta, "segments": segments}
