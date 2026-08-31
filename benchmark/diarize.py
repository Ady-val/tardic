"""Tardic — Fase 0 (parte 2): diarización con pyannote.audio en CPU.

Requisitos (una sola vez):
  1. Cuenta en huggingface.co y token de lectura (Settings → Access Tokens).
  2. Aceptar los términos de:
       https://huggingface.co/pyannote/speaker-diarization-3.1
       https://huggingface.co/pyannote/segmentation-3.0
  3. Guardar el token:  uv run hf auth login   (o exportar HF_TOKEN)
  4. uv add pyannote.audio

Uso:
  uv run python benchmark/diarize.py --clip sesion-10min [--speakers 2]

Deja en results/:
  <clip>__pyannote__3.1.json   turnos {start, end, speaker} + métricas
  <clip>__pyannote__3.1.rttm   formato estándar de diarización
Y si existe results/<clip>__faster-whisper__<model>__int8.json, produce el
transcript alineado (cada segmento STT recibe el speaker que más se traslapa):
  <clip>__aligned__<model>.txt
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import wave
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
AUDIO = ROOT / "audio"
RESULTS = ROOT / "results"


def load_wav(path: Path):
    """WAV PCM → (tensor [canal, muestras] en [-1,1], sample_rate).

    pyannote 4.x decodifica con torchcodec, que exige las DLLs compartidas de
    FFmpeg en el PATH; una build estática de ffmpeg.exe no las trae y truena al
    importar. Como los clips del benchmark ya son WAV PCM 16 kHz mono, se leen
    con la stdlib y se le entrega el waveform ya decodificado: pyannote acepta
    {"waveform", "sample_rate"} y así no toca torchcodec.
    """
    import numpy as np
    import torch

    with wave.open(str(path), "rb") as w:
        sr, n_ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        if width != 2:
            sys.exit(f"{path.name}: se esperaba PCM de 16 bits, no de {width*8}")
        raw = w.readframes(n)
    data = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    return torch.from_numpy(data.reshape(-1, n_ch).T.copy()), sr


def align(stt_segments: list[dict], turns: list[dict]) -> list[dict]:
    """Asigna a cada segmento STT el speaker con mayor traslape temporal."""
    out = []
    for seg in stt_segments:
        best, best_ov = None, 0.0
        for t in turns:
            ov = min(seg["end"], t["end"]) - max(seg["start"], t["start"])
            if ov > best_ov:
                best, best_ov = t["speaker"], ov
        out.append({**seg, "speaker": best or "UNKNOWN"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", required=True)
    ap.add_argument("--speakers", type=int, default=None, help="número de hablantes si se conoce")
    ap.add_argument("--align-model", default="small", help="modelo faster-whisper cuyo JSON alinear")
    args = ap.parse_args()

    path = AUDIO / f"{args.clip}.wav"
    if not path.exists():
        sys.exit(f"No existe {path}")
    token = os.environ.get("HF_TOKEN")
    if not token:
        try:
            from huggingface_hub import get_token
            token = get_token()
        except Exception:
            token = None
    if not token:
        sys.exit("Falta HF_TOKEN (ver docstring: aceptar términos de pyannote y hacer login).")

    import psutil
    import torch
    from pyannote.audio import Pipeline

    torch.set_num_threads(psutil.cpu_count(logical=False) or 4)
    proc = psutil.Process()

    t0 = time.perf_counter()
    pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", token=token)
    load_s = time.perf_counter() - t0

    waveform, sr = load_wav(path)
    audio_s = waveform.shape[1] / sr

    t0 = time.perf_counter()
    kwargs = {"num_speakers": args.speakers} if args.speakers else {}
    result = pipe({"waveform": waveform, "sample_rate": sr}, **kwargs)
    diar_s = time.perf_counter() - t0
    annotation = getattr(result, "speaker_diarization", result)  # API 3.x vs 4.x

    turns = [
        {"start": round(s.start, 2), "end": round(s.end, 2), "speaker": spk}
        for s, _, spk in annotation.itertracks(yield_label=True)
    ]
    speakers = sorted({t["speaker"] for t in turns})
    row = dict(clip=args.clip, engine="pyannote", model="speaker-diarization-3.1",
               load_s=round(load_s, 1), diarize_s=round(diar_s, 1),
               rtf=round(diar_s / audio_s, 3) if audio_s else None,
               peak_rss_mb=round(proc.memory_info().peak_wset / 1e6) if hasattr(proc.memory_info(), "peak_wset") else None,
               speakers=len(speakers), turns=len(turns), ts=time.strftime("%Y-%m-%d %H:%M"))

    tag = f"{args.clip}__pyannote__3.1"
    (RESULTS / f"{tag}.json").write_text(json.dumps({"run": row, "turns": turns}, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    with (RESULTS / f"{tag}.rttm").open("w", encoding="utf-8") as f:
        annotation.write_rttm(f)

    summary = RESULTS / "summary-diarization.csv"
    new = not summary.exists()
    with summary.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)

    stt = RESULTS / f"{args.clip}__faster-whisper__{args.align_model}__int8.json"
    if stt.exists():
        segs = json.loads(stt.read_text(encoding="utf-8"))["segments"]
        aligned = align(segs, turns)
        lines, last = [], None
        for s in aligned:
            prefix = f"\n[{s['speaker']}] " if s["speaker"] != last else " "
            lines.append(prefix + s["text"])
            last = s["speaker"]
        (RESULTS / f"{args.clip}__aligned__{args.align_model}.txt").write_text("".join(lines).strip(), encoding="utf-8")
        print(f"  transcript alineado → {args.clip}__aligned__{args.align_model}.txt")

    print(f"✔ {tag}: {row['diarize_s']}s  RTF={row['rtf']}  speakers={speakers}  turnos={len(turns)}")


if __name__ == "__main__":
    main()
