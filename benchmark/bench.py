"""Tardic — Fase 0: benchmark de motores STT en CPU.

Uso:
  uv run python benchmark/bench.py --engine faster-whisper --model small --clip sesion-10min
  uv run python benchmark/bench.py --engine whisper-cpp   --model small --clip sesion-10min

Cada corrida deja en benchmark/results/:
  <clip>__<engine>__<model>__<compute>.json   segmentos + métricas
  <clip>__<engine>__<model>__<compute>.txt    texto plano
y agrega una fila a benchmark/results/summary.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

for _s in (sys.stdout, sys.stderr):  # consola de Windows en cp1252
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
AUDIO = ROOT / "audio"
RESULTS = ROOT / "results"
TOOLS = ROOT / "tools"
RESULTS.mkdir(exist_ok=True)

SUMMARY_FIELDS = [
    "clip", "engine", "model", "compute", "threads", "vad",
    "audio_s", "load_s", "transcribe_s", "rtf", "peak_rss_mb", "avg_cpu_pct",
    "language", "lang_prob", "segments", "words", "wer_vs_ref", "ts",
]


# ---------- métricas de proceso ----------
class Monitor:
    """Muestrea RSS y CPU del proceso (y sus hijos) en un hilo aparte."""

    def __init__(self, pid: int | None = None, interval: float = 0.5):
        self.proc = psutil.Process(pid or os.getpid())
        self.interval = interval
        self.peak_rss = 0
        self.cpu_samples: list[float] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _procs(self):
        try:
            return [self.proc] + self.proc.children(recursive=True)
        except psutil.Error:
            return []

    def _run(self):
        for p in self._procs():
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass
        while not self._stop.is_set():
            rss = 0
            cpu = 0.0
            for p in self._procs():
                try:
                    rss += p.memory_info().rss
                    cpu += p.cpu_percent(None)
                except psutil.Error:
                    pass
            self.peak_rss = max(self.peak_rss, rss)
            self.cpu_samples.append(cpu)
            time.sleep(self.interval)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(timeout=2)

    @property
    def avg_cpu(self) -> float:
        # cpu_percent suma por núcleo lógico; se normaliza a % de la máquina
        if not self.cpu_samples:
            return 0.0
        return sum(self.cpu_samples) / len(self.cpu_samples) / psutil.cpu_count()


# ---------- referencia / WER ----------
def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\sáéíóúüñ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def wer_vs_ref(clip: str, hyp: str) -> float | None:
    ref_path = AUDIO / f"ref-{clip}.txt"
    if not ref_path.exists():
        return None
    import jiwer

    ref = normalize(ref_path.read_text(encoding="utf-8", errors="ignore"))
    return round(jiwer.wer(ref, normalize(hyp)), 4)


def audio_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], text=True)
    return float(out.strip())


# ---------- motores ----------
def run_faster_whisper(path: Path, model: str, compute: str, threads: int, vad: bool, lang: str | None):
    from faster_whisper import WhisperModel

    t0 = time.perf_counter()
    m = WhisperModel(model, device="cpu", compute_type=compute, cpu_threads=threads)
    load_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    segs_iter, info = m.transcribe(
        str(path), language=lang, beam_size=5, vad_filter=vad,
        vad_parameters=dict(min_silence_duration_ms=500) if vad else None,
    )
    segments = [
        {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(),
         "avg_logprob": round(s.avg_logprob, 3), "no_speech_prob": round(s.no_speech_prob, 3)}
        for s in segs_iter
    ]
    transcribe_s = time.perf_counter() - t0
    return dict(load_s=load_s, transcribe_s=transcribe_s, segments=segments,
                language=info.language, lang_prob=round(info.language_probability, 3))


def run_whisper_cpp(path: Path, model: str, threads: int, lang: str | None, mon_holder: dict):
    exe = next(TOOLS.glob("whisper.cpp/**/whisper-cli.exe"), None)
    if not exe:
        sys.exit("No encuentro whisper-cli.exe bajo benchmark/tools/whisper.cpp/")
    model_path = TOOLS / "models" / f"ggml-{model}.bin"
    if not model_path.exists():
        sys.exit(f"Falta el modelo {model_path}")
    out_base = RESULTS / f"_wcpp_{path.stem}_{model}"
    cmd = [str(exe), "-m", str(model_path), "-f", str(path), "-t", str(threads),
           "-oj", "-of", str(out_base), "-np", "-bs", "5"]
    if lang:
        cmd += ["-l", lang]
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    mon = Monitor(proc.pid)
    mon_holder["mon"] = mon
    mon.__enter__()
    out, err = proc.communicate()
    mon.__exit__()
    total = time.perf_counter() - t0
    if proc.returncode != 0:
        sys.exit(f"whisper-cli falló ({proc.returncode}):\n{err[-2000:]}")
    # whisper.cpp reporta "load time" en stderr
    load_s = 0.0
    m = re.search(r"load time\s*=\s*([\d.]+) ms", err)
    if m:
        load_s = float(m.group(1)) / 1000
    data = json.loads(Path(str(out_base) + ".json").read_text(encoding="utf-8"))
    segments = [
        {"start": s["offsets"]["from"] / 1000, "end": s["offsets"]["to"] / 1000,
         "text": s["text"].strip()}
        for s in data.get("transcription", [])
    ]
    lang_detected = data.get("result", {}).get("language", lang or "?")
    Path(str(out_base) + ".json").unlink(missing_ok=True)
    return dict(load_s=load_s, transcribe_s=total - load_s, segments=segments,
                language=lang_detected, lang_prob=None)


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["faster-whisper", "whisper-cpp"], required=True)
    ap.add_argument("--model", required=True, help="tiny|base|small|medium|large-v3|large-v3-turbo…")
    ap.add_argument("--clip", required=True, help="nombre sin extensión en benchmark/audio/")
    ap.add_argument("--compute", default="int8", help="faster-whisper: int8|int8_float32|float32")
    ap.add_argument("--threads", type=int, default=psutil.cpu_count(logical=False) or 4)
    ap.add_argument("--no-vad", action="store_true")
    ap.add_argument("--lang", default="es", help="'' para autodetección")
    args = ap.parse_args()

    path = AUDIO / f"{args.clip}.wav"
    if not path.exists():
        sys.exit(f"No existe {path}")
    lang = args.lang or None
    vad = not args.no_vad
    compute = args.compute if args.engine == "faster-whisper" else "ggml"
    tag = f"{args.clip}__{args.engine}__{args.model}__{compute}"
    audio_s = audio_duration(path)

    print(f"▶ {tag}  ({audio_s/60:.1f} min de audio, {args.threads} hilos, vad={vad})", flush=True)
    holder: dict = {}
    if args.engine == "faster-whisper":
        with Monitor() as mon:
            r = run_faster_whisper(path, args.model, compute, args.threads, vad, lang)
    else:
        r = run_whisper_cpp(path, args.model, args.threads, lang, holder)
        mon = holder["mon"]

    text = "\n".join(s["text"] for s in r["segments"])
    words = len(text.split())
    row = dict(
        clip=args.clip, engine=args.engine, model=args.model, compute=compute,
        threads=args.threads, vad=vad, audio_s=round(audio_s, 1),
        load_s=round(r["load_s"], 1), transcribe_s=round(r["transcribe_s"], 1),
        rtf=round(r["transcribe_s"] / audio_s, 3),
        peak_rss_mb=round(mon.peak_rss / 1e6), avg_cpu_pct=round(mon.avg_cpu, 1),
        language=r["language"], lang_prob=r["lang_prob"], segments=len(r["segments"]),
        words=words, wer_vs_ref=wer_vs_ref(args.clip, text),
        ts=time.strftime("%Y-%m-%d %H:%M"),
    )
    (RESULTS / f"{tag}.json").write_text(
        json.dumps({"run": row, "host": platform.processor(), "segments": r["segments"]},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    (RESULTS / f"{tag}.txt").write_text(text, encoding="utf-8")

    summary = RESULTS / "summary.csv"
    new = not summary.exists()
    with summary.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)

    print(f"✔ {tag}: {row['transcribe_s']}s  RTF={row['rtf']}  "
          f"RAM pico={row['peak_rss_mb']}MB  CPU={row['avg_cpu_pct']}%  "
          f"WER={row['wer_vs_ref']}  palabras={words}", flush=True)


if __name__ == "__main__":
    main()
