#!/usr/bin/env bash
cd "$(dirname "$0")/.."
run() { uv run python benchmark/bench.py "$@" 2>&1 | grep -E "^(▶|✔|Traceback|.*Error)"; }
# ¿escala faster-whisper con 12 hilos?
run --engine faster-whisper --model small --clip sesion-10min --threads 12
# whisper.cpp con turbo cuantizado
curl -sL -o benchmark/tools/models/ggml-large-v3-turbo-q5_0.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
run --engine whisper-cpp --model large-v3-turbo-q5_0 --clip sesion-10min --threads 6
# clip de 66 min con referencia → WER
run --engine whisper-cpp --model small --clip capacitacion-66min --threads 6
run --engine whisper-cpp --model large-v3-turbo-q5_0 --clip capacitacion-66min --threads 6
run --engine faster-whisper --model large-v3-turbo --clip capacitacion-66min --threads 6
run --engine faster-whisper --model small --clip capacitacion-66min --threads 12
echo "SERIE2 TERMINADA"
