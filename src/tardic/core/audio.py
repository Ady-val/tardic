"""Utilidades de ffmpeg/ffprobe para preparar el audio antes de transcribir.

RN-05 (nunca cargar el audio completo en memoria): todo aquí es streaming a
disco vía subprocess, nunca leyendo el archivo completo en Python. El corte
en silencio (`detect_silences`/`cut_points`) es el algoritmo medido en
`benchmark/bench_chunked.py` el 25/08 — con umbral fijo de -30 dB encuentra
CERO silencios en una grabación con ruido de fondo, por eso escala. No se
toca sin volver a medir (AGENTS.md).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# Ninguna llamada a ffmpeg/ffprobe debe colgarse jamás (AGENTS.md). Los
# valores por default cubren una grabación larga (horas); se pueden ajustar
# por llamada si algún caso concreto lo exige.
PROBE_TIMEOUT_S = 30
CONVERT_TIMEOUT_S = 3600
SLICE_TIMEOUT_S = 900
SILENCE_TIMEOUT_S = 900

SEARCH_WINDOW_S = 90.0  # cuánto se puede mover una frontera para caer en silencio

# Umbrales de menos a más permisivo (medido en bench_chunked.py 25/08): una
# grabación de café tiene un piso de ruido alto (sesion-3h mide -22 dB de
# media) y con -30 dB ffmpeg no ve un solo silencio; se baja la exigencia
# hasta encontrar huecos reales donde cortar. NO "mejorar" este umbral.
NOISE_STEPS_DB = (-40, -35, -30, -27, -24, -21, -18)


class AudioError(Exception):
    """Fallo de ffmpeg/ffprobe al procesar un archivo.

    El mensaje trae el stderr recortado, nunca la traza completa: lo que ve
    el usuario no lleva rutas del servidor (AGENTS.md regla 7); el stderr
    completo se queda en el log de quien llama, no aquí.
    """


def _run(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"{cmd[0]} no respondió en {timeout:.0f}s") from exc
    except FileNotFoundError as exc:
        raise AudioError(f"no se encontró el binario {cmd[0]!r} en el sistema") from exc


def _trimmed(stderr: str, limit: int = 2000) -> str:
    stderr = (stderr or "").strip()
    return stderr[-limit:] if stderr else "(sin salida de error)"


def probe_duration(path: Path) -> float:
    """Duración en segundos vía ffprobe."""
    proc = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        timeout=PROBE_TIMEOUT_S,
    )
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        raise AudioError(
            f"ffprobe no pudo leer la duración de {path.name}: {_trimmed(proc.stderr)}"
        )
    try:
        return float(out)
    except ValueError as exc:
        raise AudioError(f"ffprobe devolvió una duración ilegible para {path.name}") from exc


def probe_audio_info(path: Path) -> dict:
    """Duración, códec, canales y sample rate de la pista de audio.

    Falla con `AudioError` si el archivo no tiene pista de audio — un
    usuario puede subir un PDF renombrado a .mp3, y eso debe rechazarse
    con un mensaje claro, no con una excepción críptica de ffprobe.
    """
    proc = _run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        timeout=PROBE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise AudioError(f"ffprobe no pudo leer {path.name}: {_trimmed(proc.stderr)}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AudioError(f"ffprobe devolvió una salida ilegible para {path.name}") from exc

    audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio_streams:
        raise AudioError(f"{path.name} no tiene ninguna pista de audio")
    stream = audio_streams[0]

    duration = data.get("format", {}).get("duration") or stream.get("duration")
    if duration is None:
        raise AudioError(f"no se pudo determinar la duración de {path.name}")

    try:
        channels = int(stream.get("channels", 0))
        sample_rate = int(stream.get("sample_rate", 0))
    except (TypeError, ValueError) as exc:
        raise AudioError(f"ffprobe devolvió metadatos de audio inválidos para {path.name}") from exc

    return {
        "duration_seconds": float(duration),
        "codec": stream.get("codec_name") or "desconocido",
        "channels": channels,
        "sample_rate": sample_rate,
    }


def to_wav16k_mono(src: Path, dst: Path, *, timeout: float = CONVERT_TIMEOUT_S) -> Path:
    """Convierte a WAV PCM 16 kHz mono — el formato que come el motor STT."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
        timeout=timeout,
    )
    if proc.returncode != 0 or not dst.exists():
        raise AudioError(f"ffmpeg no pudo convertir {src.name} a WAV: {_trimmed(proc.stderr)}")
    return dst


def detect_silences(
    path: Path, noise_db: int, min_dur: float = 0.3, *, timeout: float = SILENCE_TIMEOUT_S
) -> list[tuple[float, float]]:
    """Lista de (inicio, fin) de los silencios del audio, vía ffmpeg silencedetect.

    Copiado de `benchmark/bench_chunked.py` (medido el 25/08): NO cambiar la
    lógica de umbrales aquí, eso vive en `cut_points`.
    """
    proc = _run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"],
        timeout=timeout,
    )
    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", proc.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", proc.stderr)]
    return list(zip(starts, ends, strict=False))  # zip corta si el último silencio no cerró


def _targets_for(total_s: float, chunk_s: float) -> list[float]:
    """Los tiempos ideales de corte, antes de moverlos al silencio más cercano."""
    n = max(1, round(total_s / chunk_s))  # trozos parejos, sin una cola diminuta
    return [total_s * i / n for i in range(1, n)]


def cut_points(total_s: float, chunk_s: float, path: Path) -> tuple[list[float], int, int]:
    """Fronteras de corte y con qué umbral se lograron.

    Devuelve (puntos, umbral_usado_db, cuántos cayeron en silencio real).
    Copiado tal cual de `benchmark/bench_chunked.py` — el umbral adaptativo,
    la ventana de búsqueda de ±90s y el piso de 60s por trozo están medidos,
    no se "mejoran" aquí.
    """
    targets = _targets_for(total_s, chunk_s)
    best: tuple[list[float], int, int] = ([], NOISE_STEPS_DB[0], -1)
    for noise_db in NOISE_STEPS_DB:
        mids = [(a + b) / 2 for a, b in detect_silences(path, noise_db)]
        points, hits = [], 0
        for t in targets:
            floor = (points[-1] if points else 0) + 60  # nunca un trozo de <1 min
            # El piso empuja la frontera hacia adelante y podía pasarse del
            # final del audio: entonces salía un trozo con `end < start` y
            # ffmpeg abortaba ("-to value smaller than -ss"), tumbando el job
            # en sus tres intentos. Solo ocurría con `chunk_minutes` por debajo
            # de ~1.3, pero es un valor que el operador puede poner libremente.
            if floor >= total_s:
                break  # ya no cabe otro trozo: lo que queda va en el último
            near = [m for m in mids if abs(m - t) <= SEARCH_WINDOW_S and m > floor]
            if near:
                points.append(min(near, key=lambda m: abs(m - t)))
                hits += 1
            else:
                points.append(min(max(t, floor), total_s))
        if hits > best[2]:
            best = (points, noise_db, hits)
        if hits == len(targets):  # todas las fronteras cayeron en silencio: listo
            break
    return best


def slice_audio(
    src: Path, dst: Path, start: float, end: float, *, timeout: float = SLICE_TIMEOUT_S
) -> Path:
    """Recorta [start, end) de `src` a un WAV 16 kHz mono en `dst`."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(dst)],
        timeout=timeout,
    )
    if proc.returncode != 0 or not dst.exists():
        raise AudioError(
            f"ffmpeg no pudo recortar {src.name} "
            f"[{start:.1f}-{end:.1f}]: {_trimmed(proc.stderr)}"
        )
    return dst
