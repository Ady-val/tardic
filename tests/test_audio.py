"""Tests de `tardic.core.audio` con audio sintético (nada de audio de clientes,
AGENTS.md regla 2). Se generan tonos separados por silencios REALES con los
filtros `sine`/`anullsrc` de ffmpeg y se verifica que el módulo los encuentre.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tardic.core.audio import (
    AudioError,
    cut_points,
    detect_silences,
    probe_audio_info,
    probe_duration,
    slice_audio,
    to_wav16k_mono,
)

# Segmentos del audio sintético: (tipo, duración_s). El piso de `cut_points`
# es "nunca un trozo de <1 min", así que el primer silencio tiene que caer
# después de los 60s para que el algoritmo (sin tocar) pueda alcanzarlo.
SEGMENTS = [
    ("tone", 60.0),      # 0 - 60
    ("silence", 3.0),    # 60 - 63  (silencio 1, punto medio 61.5)
    ("tone", 30.0),      # 63 - 93
    ("silence", 3.0),    # 93 - 96  (silencio 2, punto medio 94.5)
    ("tone", 34.0),      # 96 - 130
]
TOTAL_S = sum(d for _, d in SEGMENTS)
SILENCE_1_MID = 61.5
SILENCE_2_MID = 94.5


def _build_tone_silence_wav(dst: Path, sample_rate: int = 44100) -> None:
    """Concatena tonos y silencios reales (anullsrc = amplitud 0) con ffmpeg."""
    inputs: list[str] = []
    labels: list[str] = []
    for i, (kind, dur) in enumerate(SEGMENTS):
        if kind == "tone":
            inputs += ["-f", "lavfi", "-t", f"{dur}", "-i", f"sine=frequency=440:sample_rate={sample_rate}"]
        else:
            inputs += ["-f", "lavfi", "-t", f"{dur}", "-i", f"anullsrc=r={sample_rate}:cl=mono"]
        labels.append(f"[{i}:a]")
    filter_complex = "".join(labels) + f"concat=n={len(SEGMENTS)}:v=0:a=1[out]"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        *inputs, "-filter_complex", filter_complex, "-map", "[out]", str(dst),
    ]
    subprocess.run(cmd, check=True, timeout=60)


def _build_video_only_mp4(dst: Path) -> None:
    """Un archivo de video sin ninguna pista de audio (el caso del PDF
    renombrado a .mp3: contenido sin audio)."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:size=32x32:duration=1",
        "-an", "-pix_fmt", "yuv420p", str(dst),
    ]
    subprocess.run(cmd, check=True, timeout=60)


@pytest.fixture(scope="module")
def tone_silence_wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("audio")
    path = d / "tones.wav"
    _build_tone_silence_wav(path)
    return path


def test_probe_duration(tone_silence_wav: Path) -> None:
    duration = probe_duration(tone_silence_wav)
    assert abs(duration - TOTAL_S) < 1.0


def test_probe_audio_info(tone_silence_wav: Path) -> None:
    info = probe_audio_info(tone_silence_wav)
    assert abs(info["duration_seconds"] - TOTAL_S) < 1.0
    assert info["channels"] >= 1
    assert info["sample_rate"] == 44100


def test_to_wav16k_mono(tone_silence_wav: Path, tmp_path: Path) -> None:
    dst = tmp_path / "out.wav"
    result = to_wav16k_mono(tone_silence_wav, dst)
    assert result == dst
    assert dst.exists()

    info = probe_audio_info(dst)
    assert info["sample_rate"] == 16000
    assert info["channels"] == 1
    assert abs(info["duration_seconds"] - TOTAL_S) < 1.0


def test_detect_silences_encuentra_los_insertados(tone_silence_wav: Path) -> None:
    silences = detect_silences(tone_silence_wav, noise_db=-40, min_dur=0.3)
    assert len(silences) == 2

    mids = sorted((a + b) / 2 for a, b in silences)
    assert abs(mids[0] - SILENCE_1_MID) < 0.5
    assert abs(mids[1] - SILENCE_2_MID) < 0.5


def test_cut_points_mueve_la_frontera_al_silencio(tone_silence_wav: Path) -> None:
    total_s = probe_duration(tone_silence_wav)
    chunk_s = 65.0  # target teórico a los 65s: cae entre los dos silencios

    points, noise_db, hits = cut_points(total_s, chunk_s, tone_silence_wav)

    assert len(points) == 1
    assert hits == 1
    assert noise_db in (-40, -35, -30, -27, -24, -21, -18)
    # el target ideal (65s) está más cerca del silencio 1 (61.5) que del 2
    # (94.5); el algoritmo debe moverlo ahí, no dejarlo en el target crudo.
    assert abs(points[0] - SILENCE_1_MID) < 1.0


def test_slice_audio(tone_silence_wav: Path, tmp_path: Path) -> None:
    dst = tmp_path / "slice.wav"
    result = slice_audio(tone_silence_wav, dst, 10.0, 20.0)
    assert result == dst
    duration = probe_duration(dst)
    assert abs(duration - 10.0) < 0.5


def test_archivo_sin_audio_lanza_audio_error(tmp_path: Path) -> None:
    video_only = tmp_path / "video.mp4"
    _build_video_only_mp4(video_only)

    with pytest.raises(AudioError):
        probe_audio_info(video_only)


def test_archivo_basura_lanza_audio_error(tmp_path: Path) -> None:
    """Un PDF (u otra cosa) renombrado: ffprobe ni siquiera puede leerlo."""
    fake = tmp_path / "no-es-audio.mp3"
    fake.write_bytes(b"%PDF-1.4\n%no soy un audio\n" * 50)

    with pytest.raises(AudioError):
        probe_audio_info(fake)
