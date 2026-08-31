#!/usr/bin/env bash
# Serie secuencial (una corrida a la vez para no contaminar las métricas).
cd "$(dirname "$0")/.."
CLIP=${1:-sesion-10min}
T=${THREADS:-6}
for m in base small medium large-v3-turbo large-v3; do
  uv run python benchmark/bench.py --engine faster-whisper --model $m --clip $CLIP --threads $T 2>&1 | grep -E "^(▶|✔|Traceback|.*Error)"
done
for m in base small; do
  uv run python benchmark/bench.py --engine whisper-cpp --model $m --clip $CLIP --threads $T 2>&1 | grep -E "^(▶|✔|Traceback|.*Error)"
done
echo "SERIE TERMINADA $CLIP"
