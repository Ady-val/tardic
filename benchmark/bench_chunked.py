"""Tardic — Fase 0 (parte 3): transcripción de archivos largos por trozos.

Responde a §13 del doc 03 ("nunca asumir que un archivo de 4 h se procesa como
una única operación indivisible") y a RF-10 ("una falla a mitad no obliga a
empezar de cero"). La Fase 0 midió que la RAM de faster-whisper crece con la
duración (1.6 GB en 10 min → 4.6 GB en 66 min): a 3 h se iría a ~10 GB. El
chunking es la respuesta, y este script mide si de verdad la deja plana.

Qué hace distinto a bench.py:
  · corta el audio en trozos de ~N minutos, **en silencio**, no a la brava:
    una pasada de ffmpeg silencedetect da los silencios reales y cada frontera
    se mueve al silencio más cercano (±90 s) para no partir palabras;
  · carga el modelo UNA vez y reutiliza el mismo objeto en todos los trozos;
  · deja un checkpoint por trozo, así que una corrida interrumpida se reanuda
    donde iba (idempotencia);
  · reporta RAM pico POR TROZO además de la global — el dato que justifica (o
    tumba) la estrategia.

Uso:
  uv run python benchmark/bench_chunked.py --clip sesion-3h --model large-v3-turbo
  uv run python benchmark/bench_chunked.py --clip sesion-3h --chunk-min 10 --restart

Deja en results/:
  <clip>__faster-whisper__<model>__int8__chunked.json   segmentos + métricas + por-trozo
  <clip>__faster-whisper__<model>__int8__chunked.txt    texto plano
  _chunks/<clip>__<model>/chunk-NN.json                 checkpoints (ignorados por git)
y agrega una fila a summary.csv con el mismo esquema que bench.py.
"""
from __future__ import annotations

import argparse
import csv
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

from bench import AUDIO, RESULTS, SUMMARY_FIELDS, Monitor, audio_duration, wer_vs_ref

CHUNKS = RESULTS / "_chunks"
SEARCH_WINDOW_S = 90.0  # cuánto se puede mover una frontera para caer en silencio


# ---------- corte en silencio ----------
# Umbrales de menos a más permisivo. Una grabación de café tiene un piso de
# ruido alto (sesion-3h mide -22 dB de media) y con -30 dB ffmpeg no ve un solo
# silencio; se va bajando la exigencia hasta encontrar huecos donde cortar.
NOISE_STEPS_DB = (-40, -35, -30, -27, -24, -21, -18)


def detect_silences(path: Path, noise_db: int, min_dur: float = 0.3) -> list[tuple[float, float]]:
    """Lista de (inicio, fin) de los silencios del audio, vía ffmpeg."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
           "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stderr
    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", err)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", err)]
    return list(zip(starts, ends))  # zip corta si el último silencio no cerró


def targets_for(total_s: float, chunk_s: float) -> list[float]:
    """Los tiempos ideales de corte, antes de moverlos al silencio más cercano."""
    n = max(1, round(total_s / chunk_s))  # trozos parejos, sin una cola diminuta
    return [total_s * i / n for i in range(1, n)]


def cut_points(total_s: float, chunk_s: float, path: Path) -> tuple[list[float], int, int]:
    """Fronteras de corte y con qué umbral se lograron.

    Devuelve (puntos, umbral_usado_db, cuántos cayeron en silencio real).
    """
    targets = targets_for(total_s, chunk_s)
    best: tuple[list[float], int, int] = ([], NOISE_STEPS_DB[0], -1)
    for noise_db in NOISE_STEPS_DB:
        mids = [(a + b) / 2 for a, b in detect_silences(path, noise_db)]
        points, hits = [], 0
        for t in targets:
            floor = (points[-1] if points else 0) + 60  # nunca un trozo de <1 min
            near = [m for m in mids if abs(m - t) <= SEARCH_WINDOW_S and m > floor]
            if near:
                points.append(min(near, key=lambda m: abs(m - t)))
                hits += 1
            else:
                points.append(max(t, floor))
        if hits > best[2]:
            best = (points, noise_db, hits)
        if hits == len(targets):  # todas las fronteras cayeron en silencio: listo
            break
    return best


def slice_audio(src: Path, dst: Path, start: float, end: float) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(dst)],
        check=True,
    )


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", required=True)
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--chunk-min", type=float, default=15.0, help="minutos por trozo")
    ap.add_argument("--compute", default="int8")
    ap.add_argument("--threads", type=int, default=psutil.cpu_count(logical=False) or 4)
    ap.add_argument("--lang", default="es", help="'' para autodetección")
    ap.add_argument("--no-vad", action="store_true")
    ap.add_argument("--restart", action="store_true", help="ignora los checkpoints y rehace todo")
    ap.add_argument("--max-chunks", type=int, default=None,
                    help="procesa a lo más N trozos y para; la siguiente corrida reanuda")
    args = ap.parse_args()

    path = AUDIO / f"{args.clip}.wav"
    if not path.exists():
        sys.exit(f"No existe {path}")
    lang = args.lang or None
    vad = not args.no_vad
    chunk_s = args.chunk_min * 60
    total_s = audio_duration(path)

    ckpt_dir = CHUNKS / f"{args.clip}__{args.model}"
    if args.restart and ckpt_dir.exists():
        shutil.rmtree(ckpt_dir)
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    print(f"▶ {args.clip}: {total_s/60:.1f} min · trozos de ~{args.chunk_min:g} min · "
          f"{args.model} {args.compute} · {args.threads} hilos · vad={vad}", flush=True)

    print("  buscando silencios para cortar sin partir palabras…", flush=True)
    t0 = time.perf_counter()
    points, noise_db, hits = cut_points(total_s, chunk_s, path)
    bounds = [0.0] + points + [total_s]
    ranges = list(zip(bounds[:-1], bounds[1:]))
    print(f"  {len(ranges)} trozos · {hits}/{len(points)} fronteras cayeron en silencio "
          f"(umbral {noise_db} dB, {time.perf_counter()-t0:.0f}s)", flush=True)

    from faster_whisper import WhisperModel

    model = None  # se carga perezosamente: si todo está en checkpoint, no hace falta
    load_s = 0.0
    all_segments: list[dict] = []
    per_chunk: list[dict] = []
    total_transcribe_s = 0.0
    peak_rss_global = 0

    done_now = 0  # trozos calculados en ESTA corrida (los de checkpoint no cuentan)

    for i, (start, end) in enumerate(ranges):
        ckpt = ckpt_dir / f"chunk-{i:02d}.json"
        if not ckpt.exists() and args.max_chunks and done_now >= args.max_chunks:
            print(f"\n⏸ --max-chunks {args.max_chunks} alcanzado: quedan "
                  f"{len(ranges)-i} trozos. El mismo comando reanuda desde el {i}.")
            return
        if ckpt.exists():
            data = json.loads(ckpt.read_text(encoding="utf-8"))
            all_segments.extend(data["segments"])
            per_chunk.append(data["meta"])
            total_transcribe_s += data["meta"]["transcribe_s"]
            peak_rss_global = max(peak_rss_global, data["meta"]["peak_rss_mb"] * 1_000_000)
            print(f"  [{i+1}/{len(ranges)}] checkpoint reutilizado "
                  f"({data['meta']['transcribe_s']}s, {data['meta']['peak_rss_mb']}MB)", flush=True)
            continue

        if model is None:
            t0 = time.perf_counter()
            model = WhisperModel(args.model, device="cpu", compute_type=args.compute,
                                 cpu_threads=args.threads)
            load_s = time.perf_counter() - t0
            print(f"  modelo cargado en {load_s:.1f}s", flush=True)

        tmp = ckpt_dir / f"chunk-{i:02d}.wav"
        slice_audio(path, tmp, start, end)
        t0 = time.perf_counter()
        with Monitor() as mon:
            segs_iter, info = model.transcribe(
                str(tmp), language=lang, beam_size=5, vad_filter=vad,
                vad_parameters=dict(min_silence_duration_ms=500) if vad else None,
            )
            segments = [
                # los timestamps vuelven a la línea de tiempo del archivo completo
                {"start": round(s.start + start, 2), "end": round(s.end + start, 2),
                 "text": s.text.strip(), "avg_logprob": round(s.avg_logprob, 3),
                 "no_speech_prob": round(s.no_speech_prob, 3)}
                for s in segs_iter
            ]
        dt = time.perf_counter() - t0
        tmp.unlink(missing_ok=True)

        meta = dict(chunk=i, start=round(start, 1), end=round(end, 1),
                    dur_s=round(end - start, 1), transcribe_s=round(dt, 1),
                    rtf=round(dt / (end - start), 3), peak_rss_mb=round(mon.peak_rss / 1e6),
                    avg_cpu_pct=round(mon.avg_cpu, 1), segments=len(segments),
                    language=info.language)
        ckpt.write_text(json.dumps({"meta": meta, "segments": segments},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
        all_segments.extend(segments)
        per_chunk.append(meta)
        total_transcribe_s += dt
        peak_rss_global = max(peak_rss_global, mon.peak_rss)
        done_now += 1
        done_s = end
        eta = (total_s - done_s) * (total_transcribe_s / done_s)
        print(f"  [{i+1}/{len(ranges)}] {meta['dur_s']/60:.1f} min → {dt:.0f}s "
              f"RTF={meta['rtf']} RAM={meta['peak_rss_mb']}MB · faltan ~{eta/60:.0f} min",
              flush=True)

    text = "\n".join(s["text"] for s in all_segments)
    rss_chunks = [c["peak_rss_mb"] for c in per_chunk]
    row = dict(
        clip=args.clip, engine="faster-whisper", model=args.model,
        compute=f"{args.compute}-chunked", threads=args.threads, vad=vad,
        audio_s=round(total_s, 1), load_s=round(load_s, 1),
        transcribe_s=round(total_transcribe_s, 1),
        rtf=round(total_transcribe_s / total_s, 3),
        peak_rss_mb=round(peak_rss_global / 1e6),
        avg_cpu_pct=round(sum(c["avg_cpu_pct"] for c in per_chunk) / len(per_chunk), 1),
        language=per_chunk[0]["language"], lang_prob=None,
        segments=len(all_segments), words=len(text.split()),
        wer_vs_ref=wer_vs_ref(args.clip, text), ts=time.strftime("%Y-%m-%d %H:%M"),
    )

    tag = f"{args.clip}__faster-whisper__{args.model}__{args.compute}__chunked"
    (RESULTS / f"{tag}.json").write_text(
        json.dumps({"run": row, "host": platform.processor(),
                    "chunk_min": args.chunk_min, "chunks": per_chunk,
                    "segments": all_segments}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    (RESULTS / f"{tag}.txt").write_text(text, encoding="utf-8")

    summary = RESULTS / "summary.csv"
    new = not summary.exists()
    with summary.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)

    print(f"\n✔ {tag}")
    print(f"  {row['transcribe_s']/60:.0f} min de proceso para {total_s/60:.0f} min de audio "
          f"· RTF={row['rtf']} · WER={row['wer_vs_ref']} · {row['words']} palabras")
    print(f"  RAM por trozo: min {min(rss_chunks)}MB · máx {max(rss_chunks)}MB "
          f"→ {'PLANA (el chunking sirve)' if max(rss_chunks) - min(rss_chunks) < 500 else 'CRECIENTE (revisar)'}")


if __name__ == "__main__":
    main()
