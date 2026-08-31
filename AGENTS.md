# Reglas de construcción — Tardic MVP

Este archivo manda sobre la opinión de cualquier agente. Si algo aquí choca con
lo que te parece mejor práctica, gana este archivo; si crees que está mal,
dilo en tu reporte, no lo cambies por tu cuenta.

## Qué estamos construyendo

**El usuario carga un archivo de audio y después queda disponible la
transcripción.** Se prueba con Postman contra un VPS. Todo lo demás se
subordina a ese criterio.

## Los contratos ya están escritos — no los cambies

Estos archivos son el ensamblaje del sistema. Léelos antes de escribir nada:

| Archivo | Qué fija |
|---|---|
| `src/tardic/config.py` | toda la configuración, por variables `TARDIC_*` |
| `src/tardic/models.py` | las 5 entidades y los estados. **Nombres intocables** |
| `src/tardic/schemas.py` | el contrato HTTP |
| `src/tardic/core/stt.py` | `SttEngine`, `SttResult`, `ChunkProgress` |
| `src/tardic/storage.py` | rutas en disco |

Agregar una columna o un campo es válido si algo lo exige de verdad; renombrar
o quitar, no. Si necesitas cambiar un contrato, **repórtalo y sigue con lo
demás** — quien coordina decide.

## Reglas duras

1. **Antes de agregar una dependencia**, responde: ¿qué problema concreto
   resuelve?, ¿ya hay algo en el proyecto que lo haga?, ¿se mantiene?, ¿qué
   licencia?, ¿cuánta complejidad suma?, ¿hay algo más simple?, ¿es necesaria
   para el MVP? Si no puedes responder las siete, no la metes. Las que ya están
   aprobadas viven en `pyproject.toml`; **no lo edites**, pídelo en tu reporte.
2. **Nada de audio de clientes en el repo.** Las fixtures son sintéticas
   (generadas con ffmpeg) o locales e ignoradas por git.
3. **Secretos fuera del código.** Todo por entorno. Ningún valor real en los
   ejemplos.
4. **El nombre de archivo que sube el usuario nunca toca el disco.** Las rutas
   se derivan del UUID del servidor. Usa `storage.py`, no construyas rutas a
   mano.
5. **Nada carga un audio completo en memoria.** Ni la subida, ni el worker.
   Streaming a disco y proceso por trozos.
6. **Los timestamps que salen son de la línea de tiempo del audio completo**,
   nunca del trozo.
7. **Los errores que ve el usuario no llevan rutas del servidor ni trazas.**
   Eso va al log.
8. **Entregas con tests.** Sin tests no está terminado. Los que necesiten el
   modelo real o audio real van marcados `@pytest.mark.slow`; los demás corren
   en segundos con un motor falso.
9. **Escribe en español** los comentarios y mensajes, como el resto del repo.
   Comenta el *por qué*, no el *qué*.

## Ya existe y funciona — reúsalo, no lo reinventes

`benchmark/bench_chunked.py` tiene **código medido y validado el 25/08** que
resuelve lo difícil:

- corte del audio en trozos **en silencio real**, con umbral adaptativo que
  escala de −40 dB hacia arriba (con umbral fijo de −30 dB encuentra *cero*
  silencios en una grabación con ruido de fondo);
- checkpoint por trozo, reanudable sin recomputar;
- reconstrucción de timestamps a la línea de tiempo completa.

Medido: **RTF 0.51 y 1.46 GB de RAM** con `large-v3-turbo`, contra 0.75 y
4.6 GB procesando el archivo de un golpe. **El chunking no es solo defensa de
memoria: también es 30 % más rápido.** No lo rediseñes; muévelo al paquete.

`benchmark/diarize.py` resuelve dos trampas ya pagadas: son **tres** repos
*gated* de pyannote (el 4.x saca el PLDA de `speaker-diarization-community-1`)
y **`torchcodec` no sirve con FFmpeg estático**, por eso decodifica el WAV con
la stdlib.

## Reparto — cada quien en sus archivos

Nadie edita archivos de otro. Si necesitas algo de otro módulo, prográmalo
contra el contrato y dilo en tu reporte.

- **A · núcleo**: `src/tardic/core/audio.py`, `src/tardic/core/faster_whisper_engine.py`,
  `tests/test_audio.py`, `tests/test_stt.py`
- **B · datos y worker**: `src/tardic/db.py`, `src/tardic/repository.py`,
  `src/tardic/worker/`, `migrations/`, `alembic.ini`, `tests/test_queue.py`,
  `tests/test_worker.py`
- **C · API**: `src/tardic/api/`, `tests/test_api.py`, `tests/conftest.py`
- **D · despliegue**: `docker/`, `docker-compose.yml`, `.env.example`,
  `docs/despliegue-vps.md`, `postman/`

## Cómo se verifica tu trabajo

Nadie da por bueno un reporte. Se levanta el sistema y se le pega con `curl`:
subir audio real, ver el progreso avanzar trozo por trozo, descargar el TXT.
Si tu módulo no aguanta eso, no está listo — aunque los tests pasen.
