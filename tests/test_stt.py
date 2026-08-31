"""Tests del contrato `SttEngine`.

La mayoría corre contra `FakeEngine` (no carga modelos, corre en segundos:
AGENTS.md regla 8). El único test marcado `slow` usa `FasterWhisperEngine`
con el modelo `tiny` y verifica la reanudación por checkpoint (RF-10).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tardic.core.audio import to_wav16k_mono
from tardic.core.fake_engine import FakeEngine
from tardic.core.stt import ChunkProgress, SttEngine


def _build_tone_wav(dst: Path, duration_s: float, *, sample_rate: int = 16000) -> Path:
    """Un tono simple de `duration_s` segundos. Sirve para probar la
    mecánica de trozos/checkpoints, no la exactitud de la transcripción."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-t", f"{duration_s}", "-i", f"sine=frequency=440:sample_rate={sample_rate}",
        "-ac", "1", str(dst),
    ]
    subprocess.run(cmd, check=True, timeout=60)
    return dst


@pytest.fixture(scope="module")
def long_tone_wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # 185s con chunk_seconds=60 da 4 trozos: 3 completos + una cola de 5s.
    d = tmp_path_factory.mktemp("stt-audio")
    return _build_tone_wav(d / "tone.wav", 185.0)


def test_fake_engine_cumple_el_protocolo() -> None:
    engine = FakeEngine()
    assert isinstance(engine, SttEngine)


def test_timestamps_crecientes_y_absolutos(long_tone_wav: Path) -> None:
    engine = FakeEngine(chunk_seconds=60.0)
    result = engine.transcribe(long_tone_wav)

    assert result.segments, "el motor falso debe producir al menos un segmento"
    assert result.segments[0].start == 0.0

    prev_end = 0.0
    for seg in result.segments:
        assert seg.start >= prev_end - 1e-6  # no retrocede: línea de tiempo absoluta
        assert seg.end > seg.start
        prev_end = seg.end

    assert result.segments[-1].end <= result.audio_duration_seconds + 0.5


def test_on_progress_una_vez_por_trozo_y_termina_en_100(long_tone_wav: Path) -> None:
    progress_calls: list[ChunkProgress] = []
    engine = FakeEngine(chunk_seconds=60.0)

    result = engine.transcribe(long_tone_wav, on_progress=progress_calls.append)

    expected_chunks = len(result.chunks)
    assert expected_chunks == 4  # 60+60+60+5

    assert len(progress_calls) == expected_chunks
    for i, p in enumerate(progress_calls, start=1):
        assert p.chunks_done == i
        assert p.chunks_total == expected_chunks

    assert progress_calls[-1].chunks_done == progress_calls[-1].chunks_total
    assert progress_calls[-1].percent == 100


def test_chunkprogress_percent() -> None:
    p = ChunkProgress(chunks_done=2, chunks_total=4, seconds_done=50.0,
                       seconds_total=100.0, elapsed_seconds=25.0)
    assert p.percent == 50

    done = ChunkProgress(chunks_done=4, chunks_total=4, seconds_done=100.0,
                          seconds_total=100.0, elapsed_seconds=40.0)
    assert done.percent == 100

    sin_total = ChunkProgress(chunks_done=0, chunks_total=1, seconds_done=0.0,
                               seconds_total=0.0, elapsed_seconds=0.0)
    assert sin_total.percent == 0


def test_chunkprogress_eta_seconds() -> None:
    p = ChunkProgress(chunks_done=2, chunks_total=4, seconds_done=50.0,
                       seconds_total=100.0, elapsed_seconds=25.0)
    # ritmo medido en esta corrida: 25s de proceso por 50s de audio → 0.5 s/s;
    # faltan 50s de audio → ~25s más.
    assert p.eta_seconds == 25

    sin_avance = ChunkProgress(chunks_done=0, chunks_total=4, seconds_done=0.0,
                                seconds_total=100.0, elapsed_seconds=0.0)
    assert sin_avance.eta_seconds is None


@pytest.mark.slow
def test_faster_whisper_reanuda_desde_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tardic.core.faster_whisper_engine import FasterWhisperEngine

    raw = tmp_path / "tone.wav"
    _build_tone_wav(raw, 8.0, sample_rate=44100)
    wav_path = tmp_path / "audio16k.wav"
    to_wav16k_mono(raw, wav_path)  # el motor espera el WAV ya preprocesado

    ckpt_dir = tmp_path / "checkpoints"
    engine = FasterWhisperEngine(model="tiny", compute_type="int8", vad=False, language="es")
    result1 = engine.transcribe(wav_path, checkpoint_dir=ckpt_dir)

    checkpoint_files = sorted(ckpt_dir.glob("chunk-*.json"))
    assert len(checkpoint_files) >= 1, "debe dejar al menos un checkpoint"

    # Instancia nueva (simula un proceso nuevo, sin el modelo ya en memoria).
    # Si intenta cargar el modelo otra vez es que NO está reanudando.
    def _no_model(*args: object, **kwargs: object) -> None:
        raise AssertionError("no debería cargar el modelo: ya hay checkpoints")

    monkeypatch.setattr("faster_whisper.WhisperModel", _no_model)

    engine2 = FasterWhisperEngine(model="tiny", compute_type="int8", vad=False, language="es")
    result2 = engine2.transcribe(wav_path, checkpoint_dir=ckpt_dir)

    assert result2.chunks == result1.chunks
    assert [s.text for s in result2.segments] == [s.text for s in result1.segments]
