"""Motor STT falso: no carga ningún modelo.

Implementa el mismo `SttEngine` que `FasterWhisperEngine` pero deriva
segmentos deterministas de la duración del audio. Es lo que permite que los
tests de todos los agentes (worker, API) corran en segundos sin descargar
1.6 GB de modelo (AGENTS.md regla 8, doc de `core/stt.py`).
"""
from __future__ import annotations

import time
from pathlib import Path

from tardic.core.audio import probe_duration
from tardic.core.stt import ChunkProgress, ProgressCallback, SttResult, SttSegment

SEGMENT_SECONDS = 5.0  # tamaño de cada segmento sintético dentro de un trozo
CHUNK_SECONDS = 60.0  # tamaño de "trozo" sintético, para poder probar on_progress


class FakeEngine:
    """`SttEngine` sin faster-whisper. Segmentos de texto de relleno cada
    `SEGMENT_SECONDS`, agrupados en trozos de `chunk_seconds` para simular el
    reporte de progreso trozo a trozo del motor real."""

    def __init__(
        self, *, language: str | None = "es", chunk_seconds: float = CHUNK_SECONDS
    ) -> None:
        self.language = language
        self.chunk_seconds = chunk_seconds

    def transcribe(
        self,
        audio_path: Path,
        *,
        checkpoint_dir: Path | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> SttResult:
        # checkpoint_dir se acepta por firma del contrato; este motor no lo
        # necesita porque "calcular" un trozo es gratis.
        total_s = probe_duration(Path(audio_path))
        t0 = time.perf_counter()

        bounds = [0.0]
        while bounds[-1] < total_s:
            bounds.append(min(bounds[-1] + self.chunk_seconds, total_s))
        if len(bounds) < 2:
            bounds.append(total_s)
        ranges = list(zip(bounds[:-1], bounds[1:], strict=False))

        segments: list[SttSegment] = []
        chunks: list[dict] = []

        for i, (start, end) in enumerate(ranges):
            cursor = start
            n = 0
            while cursor < end:
                seg_end = min(cursor + SEGMENT_SECONDS, end)
                segments.append(SttSegment(
                    start=round(cursor, 2), end=round(seg_end, 2),
                    text=f"[fake trozo {i} segmento {n}]", confidence=1.0,
                ))
                cursor = seg_end
                n += 1

            chunks.append({
                "chunk": i, "start": round(start, 1), "end": round(end, 1),
                "duration_seconds": round(end - start, 1), "transcribe_seconds": 0.0,
                "rtf": 0.0, "peak_rss_mb": 0.0, "language": self.language,
                "segments_count": n,
            })

            if on_progress is not None:
                on_progress(ChunkProgress(
                    chunks_done=i + 1, chunks_total=len(ranges),
                    seconds_done=end, seconds_total=total_s,
                    elapsed_seconds=time.perf_counter() - t0,
                ))

        return SttResult(
            segments=segments, language=self.language, model="fake",
            processing_time_seconds=round(time.perf_counter() - t0, 3),
            audio_duration_seconds=total_s, chunks=chunks,
        )
