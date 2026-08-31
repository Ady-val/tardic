# Fase 0 — Benchmark STT

Mide, sobre grabaciones reales en español, qué motor y qué tamaño de modelo
aguantan el caso de uso de Tardic en **CPU** (sin GPU: la laptop de desarrollo y el
VPS no tienen NVIDIA).

## Máquina de referencia

AMD Ryzen 5 5500U · 6 núcleos / 12 hilos · 18 GB RAM · Radeon integrada (sin
CUDA) · Windows 11. Se usan **6 hilos** (núcleos físicos) en todas las corridas.

## Clips (`audio/`, ignorados por git)

| clip | origen | duración | por qué |
|------|--------|----------|---------|
| `sesion-10min` | Sesión de trabajo presencial, minutos 20–30 | 10 min | dos voces, presencial, español coloquial, ruido de café |
| `demo-30min` | Demo remota de producto | 30 min | demo remota, varias voces, vocabulario de producto |
| `capacitacion-66min` | Sesión de capacitación grabada | 66 min | una voz principal, Google Meet, tiene transcripción de referencia |
| *(pendiente)* `sesion-3h` | La misma sesión presencial, completa | 3 h 08 | la prueba real de "archivo largo" |

Todos a 16 kHz mono PCM (`ffmpeg -ac 1 -ar 16000`).

`ref-<clip>.txt` = transcripción previa hecha con otra herramienta (también
basada en Whisper). Sirve para **WER de acuerdo**, no como verdad absoluta.

## Correr

```bash
uv run python benchmark/bench.py --engine faster-whisper --model small --clip sesion-10min
uv run python benchmark/bench.py --engine whisper-cpp   --model small --clip sesion-10min
bash benchmark/run_series.sh sesion-10min    # toda la serie, una corrida a la vez
```

whisper.cpp se espera en `tools/whisper.cpp/**/whisper-cli.exe` (release
`whisper-blas-bin-x64.zip`) y sus modelos en `tools/models/ggml-<model>.bin`
(de `huggingface.co/ggerganov/whisper.cpp`). Los de faster-whisper se bajan
solos a la caché de Hugging Face.

### Archivos largos — `bench_chunked.py`

La RAM de faster-whisper crece con la duración (1.6 GB en 10 min → 4.6 GB en
66 min): a 3 h se iría a ~10 GB. `bench_chunked.py` parte el audio, carga el
modelo una sola vez y deja un checkpoint por trozo, así que una corrida
interrumpida se reanuda donde iba (§13 del doc 03 y RF-10).

```bash
uv run python benchmark/bench_chunked.py --clip sesion-3h --model large-v3-turbo
uv run python benchmark/bench_chunked.py --clip sesion-3h --chunk-min 10 --restart
```

Las fronteras **no** caen a tiempo exacto: una pasada de `ffmpeg silencedetect`
busca dónde hay silencio y cada corte se mueve al más cercano (±90 s) para no
partir palabras. El umbral es adaptativo — se prueba de −40 dB hacia arriba
hasta cubrir todas las fronteras, porque una grabación con ruido de fondo
(`sesion-3h` mide −22 dB de media) no tiene un solo silencio bajo −30 dB.

Los checkpoints viven en `results/_chunks/<clip>__<model>/` y los ignora git;
`--restart` los tira y rehace todo. La fila que agrega a `summary.csv` lleva
`compute = int8-chunked` para distinguirla de una corrida de un solo golpe.

### Diarización — `diarize.py`

```bash
uv run python benchmark/diarize.py --clip sesion-10min --speakers 2
```

Requiere, una sola vez: token de lectura de Hugging Face (`uv run hf auth
login` o `HF_TOKEN`) **y** aceptar los términos de
`pyannote/speaker-diarization-3.1` y `pyannote/segmentation-3.0`, que son
modelos *gated*. Sin eso el script se detiene con un mensaje, no truena.

Deja los turnos en `<clip>__pyannote__3.1.json`, el `.rttm` estándar y —si ya
existe el JSON de faster-whisper del mismo clip— un transcript alineado donde
cada segmento recibe el speaker con el que más se traslapa.

## Resultados

`results/summary.csv` acumula una fila por corrida; el detalle (segmentos con
timestamps, logprob, no_speech_prob) queda en `results/<clip>__<engine>__<model>__<compute>.json`
y el texto plano en `.txt`.

Columnas clave: `rtf` (segundos de proceso por segundo de audio; <1 = más
rápido que tiempo real), `peak_rss_mb`, `avg_cpu_pct` (normalizado a la
máquina), `wer_vs_ref`.
