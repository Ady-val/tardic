# syntax=docker/dockerfile:1
#
# Imagen única para `api` y `worker` (AGENTS.md, agente D). El comando que se
# ejecuta lo decide `docker-compose.yml`, no esta imagen.
#
# Dos etapas:
#   1. builder — instala dependencias con uv usando el lockfile del repo
#      (pyproject.toml es intocable: las dependencias ya están decididas).
#   2. runtime — python:3.12-slim + ffmpeg, sin toolchain de compilación,
#      usuario no-root. El modelo de Whisper (~1.6 GB) NUNCA se hornea aquí:
#      vive en el volumen `hf-cache` montado en tiempo de ejecución.

FROM python:3.12-slim AS builder

# uv instala las dependencias fuera de la imagen final del builder; se copia
# solo el resultado (.venv) a runtime, así el compilador y las cabeceras de
# build no viajan a producción.
COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Capa de dependencias primero (cambia poco), código después (cambia mucho):
# así `docker build` reusa la capa pesada de faster-whisper/torch mientras
# solo se edite src/.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Ahora sí el código del paquete, y se instala el propio proyecto (sin dev).
# pyproject.toml no declara `readme`, así que uv_build no necesita el archivo.
# --no-editable: por defecto `uv sync` instala el proyecto en modo editable
# (un shim que apunta de vuelta a /build/src). Eso rompe en runtime porque
# solo copiamos /opt/venv a la imagen final, no /build. Con --no-editable el
# paquete queda copiado de verdad dentro del venv y es independiente de dónde
# se hizo el build.
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim AS runtime

# ffmpeg es obligatorio: todo el pipeline (preprocesado a WAV 16 kHz, chunking
# por silencio, ffprobe para duración) depende de él. No es opcional.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Usuario no-root. UID/GID fijos para que el volumen de datos en el host
# (bind mount o named volume) tenga un dueño predecible entre despliegues.
RUN groupadd --gid 1000 tardic \
    && useradd --uid 1000 --gid tardic --create-home --shell /usr/sbin/nologin tardic

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # HuggingFace/faster-whisper descargan el modelo aquí. Este directorio se
    # monta como volumen (`hf-cache`) para que el modelo sobreviva a
    # recreaciones del contenedor y no vaya dentro de la imagen.
    HF_HOME=/home/tardic/.cache/huggingface

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# alembic.ini y migrations/ (agente B) viajan en la imagen porque el servicio
# `migrate` de docker-compose.yml corre `alembic upgrade head` desde aquí,
# con este mismo Dockerfile. El código de tardic YA quedó instalado (no
# editable) en /opt/venv en la etapa anterior; esto es solo lo que alembic
# necesita para encontrar sus revisiones.
COPY alembic.ini ./alembic.ini
COPY migrations/ ./migrations/

# Entrypoint del worker (nice + exec real, ver el propio script). El de la
# api no hace falta: uvicorn se lanza directo por `command:` en compose y las
# migraciones corren en el servicio `migrate`, no en un entrypoint de la api.
COPY docker/entrypoint-worker.sh /usr/local/bin/entrypoint-worker.sh
RUN chmod +x /usr/local/bin/entrypoint-worker.sh

# /data: donde vive el audio y los checkpoints de trozos (storage.py).
# /home/tardic/.cache: caché de HF/modelo.
# Ambos deben ser escribibles por el usuario no-root ANTES del volumen mount
# (el mount hereda permisos del punto de montaje si el volumen es nuevo).
RUN mkdir -p /data /home/tardic/.cache/huggingface \
    && chown -R tardic:tardic /data /home/tardic

USER tardic

# Sin CMD por defecto: docker-compose.yml fija el comando (api o worker) por
# servicio, ver ese archivo.
