# Tardic — instrucciones para el agente que opera este sistema

Lee esto completo antes de tocar nada. Está escrito para el Claude Code que
levanta, prueba y mantiene Tardic en el VPS.

👉 **Si es tu primera vez con este repo, tu documento es
[`docs/continuidad-vps.md`](docs/continuidad-vps.md)**: trae el orden exacto de
lo que hay que hacer, qué medir, qué reportar de vuelta y el trabajo pendiente
priorizado. Para probar, `./scripts/smoke-test.sh`. Este archivo son las reglas;
aquel es el plan.

## Qué es esto

Un sistema que recibe un archivo de audio por HTTP, lo transcribe en segundo
plano y deja la transcripción disponible para descargar. Reuniones de 20
minutos a 4 horas, en español, sin depender de ningún SaaS.

El criterio de aceptación es uno solo: **se sube un audio y después está la
transcripción.** Si algo que vas a hacer no acerca a eso, no lo hagas.

## Dónde corre y qué hay alrededor

el VPS de producción (Ubuntu 24.04) **ya corre producción**: otros servicios —
unos 25 contenedores detrás del nginx que
sirve el apex.

🔴 **Esto es lo más importante de este archivo.** Un worker de Whisper toma
todos los núcleos que le des durante HORAS por cada audio largo. Si lo dejas
suelto, degradas los demás servicios, que son sistemas vivos con clientes. Las
defensas ya están puestas en `docker-compose.yml` (`cpus`, `mem_limit`, `nice`,
un solo job a la vez). **No las quites para ir más rápido.**

## Lo primero que debes hacer: medir

Nadie ha medido esta máquina todavía. Todos los números que verás en la
documentación (RTF 0.51, 1.46 GB de RAM) son de una laptop Ryzen 5 con 6
núcleos, no de aquí.

```bash
nproc                 # núcleos disponibles
free -h               # RAM libre de verdad, no la total
df -h                 # espacio: el audio y los modelos pesan
uptime                # carga actual, con producción encima
```

Con eso decide `TARDIC_WORKER_CPUS` en `.env`: **deja siempre 1 o 2 núcleos
libres** para lo que ya corre. Si la máquina tiene 4 núcleos, el worker se
queda con 2, no con 3.

Si hay menos de ~2.5 GB de RAM libres, el worker va a morir a media
transcripción. Repórtalo antes de seguir en vez de descubrirlo con un audio de
3 horas a medio procesar.

Después de la primera transcripción real, calcula el RTF de esta máquina:
`tiempo_de_proceso / duración_del_audio`. Si sale por encima de 1.5, dilo — una
sesión de 3 horas tardaría más de 4 horas y eso cambia lo que se le puede
prometer al usuario.

## Levantarlo

```bash
cp .env.example .env      # y edítalo: API key, contraseña de BD, puerto
docker compose up -d
docker compose ps         # migrate debe salir con exit 0; api healthy
```

El puerto 8080 puede estar ocupado: revisa con `ss -ltnp` antes. Se cambia con
`TARDIC_API_PORT`.

La primera transcripción **descarga el modelo (~1.6 GB)** al volumen `hf-cache`.
Tarda. No es que se haya colgado. Se descarga una sola vez y sobrevive a los
redeploys.

## Comprobar que está sano

```bash
curl -s localhost:${TARDIC_API_PORT:-8080}/health | python3 -m json.tool
```

- `database: true` — la API ve Postgres.
- `worker_seen_seconds_ago` — segundos desde el último latido del worker.
  `null` significa que el worker **nunca** ha latido (no arrancó o no comparte
  el volumen). Un número que crece sin parar significa que se atoró o murió.
  Este campo **no** tumba el healthcheck a propósito: la API está sana aunque
  el worker no lo esté, y ese matiz es justo el que te ahorra un diagnóstico
  equivocado.

Prueba de punta a punta con un audio corto de verdad antes de dar nada por
bueno. `docs/despliegue-vps.md` trae los comandos.

## Reglas al modificar el código

1. **No inventes dependencias.** Antes de agregar una, responde las siete
   preguntas de `AGENTS.md`. Una solución aburrida y madura le gana a una
   novedosa y frágil.
2. **La cola es PostgreSQL**, con `SELECT … FOR UPDATE SKIP LOCKED`. No metas
   Redis ni Celery: con este volumen serían un servicio y un punto de falla más
   a cambio de nada.
3. **Todo tiempo que se compare contra la base sale de la base** (`SELECT
   now()`), nunca del reloj del proceso. Ya hubo un bug por esto: el drift entre
   el host y el contenedor dejaba trabajos invisibles para el worker.
4. **Los timestamps que salen son de la línea de tiempo del audio completo**,
   aunque se procese por trozos.
5. **El nombre de archivo que sube el usuario nunca toca el disco.** Las rutas
   se derivan del UUID del servidor, vía `storage.py`.
6. **Los errores que ve el usuario no llevan rutas del servidor ni trazas.**
   Eso va al log.
7. **Nada de audio de clientes en el repo**, ni como fixture de prueba.
8. Entrega con tests. `uv run pytest` — los que necesitan modelos reales están
   marcados `slow` y quedan fuera por defecto.

## Cosas que ya se aprendieron a golpes

- **El chunking no es solo defensa de memoria: es 30 % más rápido.** Medido el
  25/08: RTF 0.51 y 1.46 GB en trozos, contra 0.75 y 4.6 GB de un solo golpe.
  No "optimices" quitándolo.
- **El corte entre trozos va en silencio real, con umbral adaptativo.** Con un
  umbral fijo de −30 dB no se encuentra un solo silencio en una grabación con
  ruido de fondo (`sesion-3h` mide −22 dB de media).
- **En Windows, conectar a `localhost` cuesta minutos**; usa `127.0.0.1`. Le
  costó 11 minutos de tests a quien no lo sabía. En Linux da igual.
- **La diarización (saber quién habló) NO está implementada.** El flag existe
  en el modelo pero el pipeline no la corre. Cuando se implemente, ten en
  cuenta que **duplica el tiempo de proceso** (RTF 0.978 medido).
- **pyannote necesita tres repos *gated* de Hugging Face**, no dos: el 4.x saca
  el PLDA de `speaker-diarization-community-1`. Y `torchcodec` no sirve con una
  build estática de FFmpeg.

## Qué NO hacer

- `docker volume prune` — **jamás**. `pgdata` y `audio-data` son datos reales
  del usuario y `hf-cache` son 1.6 GB que habría que volver a bajar.
- Subir el `.env` al repo. Está en `.gitignore`; que siga así.
- Quitar los límites de CPU del worker para que transcriba más rápido.
- Construir cosas que el MVP declara fuera de alcance: multiusuario, billing,
  tiempo real, RAG, Kubernetes. Primero funciona audio → transcript.
