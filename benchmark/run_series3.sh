#!/usr/bin/env bash
# Corre desacoplado del harness; log en benchmark/results/series3.log
cd "$(dirname "$0")/.."
LOG=benchmark/results/series3.log
run() { uv run python benchmark/bench.py "$@" 2>&1 | grep -E "^(▶|✔|Traceback|.*Error|No encuentro|Falta)" >> "$LOG"; }
echo "esperando a que termine whisper-cli (turbo sesion-10min)…" >> "$LOG"
while tasklist 2>/dev/null | grep -qi "whisper-cli.exe"; do sleep 15; done
echo "arranca serie 3 $(date +%H:%M)" >> "$LOG"
run --engine whisper-cpp   --model small               --clip capacitacion-66min --threads 6
run --engine whisper-cpp   --model large-v3-turbo-q5_0 --clip capacitacion-66min --threads 6
run --engine faster-whisper --model large-v3-turbo     --clip capacitacion-66min --threads 6
run --engine faster-whisper --model small              --clip capacitacion-66min --threads 6
echo "SERIE3 TERMINADA $(date +%H:%M)" >> "$LOG"
