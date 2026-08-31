# Tardic

Subes un audio y después está la transcripción.

Servicio de transcripción autoalojable: recibe grabaciones de reuniones de 20
minutos a 4 horas, las transcribe en segundo plano y entrega el texto con marcas
de tiempo. Corre entero en tu servidor — el audio y las transcripciones nunca
salen de tu infraestructura, no hay un SaaS de por medio y no se paga por
minuto. Existe porque las reuniones largas en español son justo el caso que sale
caro por API y que uno no quiere subir a un tercero.

La transcripción es la primera capa. Sobre ella vienen después resúmenes,
acuerdos, compromisos, búsqueda y memoria empresarial — pero primero tiene que
funcionar lo de arriba.

## Cómo se usa

```bash
# 1. Subir
curl -X POST http://localhost:8080/v1/recordings \
     -H "X-API-Key: $TARDIC_API_KEY" \
     -F "file=@reunion.m4a"
# -> 201 {"id": "3f2a...", "status": "QUEUED"}

# 2. Consultar mientras procesa
curl http://localhost:8080/v1/recordings/3f2a... -H "X-API-Key: $TARDIC_API_KEY"
# -> {"status":"PROCESSING","stage":"TRANSCRIBE",
#     "progress":{"chunks_done":4,"chunks_total":13,"percent":31,"eta_seconds":1680}}

# 3. Descargar cuando esté COMPLETED
curl -O -J http://localhost:8080/v1/recordings/3f2a.../transcript.txt \
     -H "X-API-Key: $TARDIC_API_KEY"
```

El progreso es real: sale de los trozos ya transcritos, no de un temporizador.
Y la estimación de tiempo restante se calcula con el ritmo medido en esa misma
corrida, porque una laptop y un VPS rinden distinto.

En `postman/` está la colección lista para importar, con el flujo completo.

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| `POST` | `/v1/recordings` | sube audio (multipart) y encola el trabajo |
| `GET` | `/v1/recordings/{id}` | estado, progreso y error si lo hubo |
| `GET` | `/v1/recordings/{id}/transcript.txt` | descarga el texto |
| `GET` | `/v1/recordings/{id}/transcript` | JSON con segmentos y timestamps |
| `GET` | `/v1/recordings` | listado paginado |
| `DELETE` | `/v1/recordings/{id}` | borra la grabación y sus derivados |
| `GET` | `/health` | salud de la API, la base y el worker (sin auth) |

Todo salvo `/health` exige la cabecera `X-API-Key`.

## Levantarlo

```bash
cp .env.example .env    # edita al menos TARDIC_API_KEY y TARDIC_DB_PASSWORD
docker compose up -d
curl localhost:8080/health
```

La primera transcripción descarga el modelo (~1.6 GB) a un volumen. Tarda una
vez; después ya está.

Para desplegarlo en un servidor, **lee `docs/despliegue-vps.md`** — incluye lo
que hay que medir antes, la configuración de nginx para subidas grandes y qué
no hacer. Si eres un agente que va a operar esto, tu documento es `CLAUDE.md`.

## Cómo funciona

```
cliente → api (FastAPI) → PostgreSQL ← worker → ffmpeg → faster-whisper → texto
                ↓                         ↑
           volumen de audio ──────────────┘
```

Un monolito modular con un worker aparte, no microservicios. La cola **es
PostgreSQL** (`SELECT … FOR UPDATE SKIP LOCKED`): con este volumen, meter Redis
sería un servicio y un punto de falla más a cambio de nada.

El audio se procesa **por trozos**, cortando en silencios reales. No es solo
para no quedarse sin memoria: medido, es un 30 % más rápido que procesar el
archivo de una sola vez (RTF 0.51 contra 0.75) y usa un tercio de la RAM. Cada
trozo deja un punto de guardado, así que una falla a la mitad de tres horas no
obliga a empezar de cero.

El motor de transcripción está detrás de una interfaz (`core/stt.py`):
cambiarlo por whisper.cpp o por un servicio externo es escribir otra clase, sin
tocar la API, el worker ni la base.

## Arquitectura y decisiones

### Cola y worker aparte, no trabajo dentro del request

`POST /v1/recordings` hace lo mínimo y devuelve: escribe el archivo a disco en
streaming, lo valida con `ffprobe`, inserta el `Recording` en `QUEUED` y un
`ProcessingJob`, y contesta 201. No transcribe nada. Transcribir tres horas de
audio en CPU tarda horas — el tope de un trabajo está puesto en 8 h
(`job_timeout_seconds`) —, y ninguna conexión HTTP sobrevive a eso.

La cola **es la tabla de Postgres**. El worker toma trabajo con
`SELECT … WHERE status='PENDING' AND available_at <= now() ORDER BY available_at
FOR UPDATE SKIP LOCKED LIMIT 1`: dos workers que consultan a la vez no chocan,
el segundo simplemente ve una fila menos. Redis o Celery serían un servicio y un
punto de falla más a cambio de nada con este volumen.

El worker corre **un solo job a la vez**, a propósito: comparte máquina con
otros servicios, y `docker-compose.yml` le pone además `cpus`, `mem_limit` y
`nice`. Un worker de Whisper sin límites se lleva todos los núcleos durante
horas por cada audio largo.

Detalle que costó un bug: **todo tiempo que se compara contra la base sale de la
base** (`SELECT now()`), nunca del reloj del proceso. El drift entre el reloj
del host y el del contenedor de Postgres hacía que un job recién encolado a
veces no apareciera como tomable.

### Chunking, y de dónde sale el tamaño del trozo

La RAM de faster-whisper crece con la duración del audio: 1.6 GB en 10 min,
4.6 GB en 66 min, extrapolado a ~10 GB en tres horas. Eso solo ya obliga a
cortar. Pero medido, cortar además **es más rápido**: 30 % menos tiempo y un
tercio de la RAM (RTF 0.51 y 1.46 GB por trozos, contra 0.71 y 4.6 GB de un
golpe, `large-v3-turbo`).

El tamaño es un **objetivo**, no un corte exacto: 15 minutos por defecto
(`TARDIC_CHUNK_MINUTES`), y cada frontera se mueve al silencio real más cercano
dentro de ±90 s para no partir una palabra a la mitad, con un piso de 60 s por
trozo. El umbral de silencio es adaptativo, de −40 dB hacia arriba: con un
umbral fijo de −30 dB, una grabación con ruido de fondo (piso medido en −22 dB)
no tiene un solo silencio y el corte falla entero.

Los 15 minutos también fijan el ritmo del latido: el worker renueva su lease al
terminar cada trozo, y el lease dura 30 minutos. El trozo tiene que caber
holgado ahí dentro o un trabajo sano se declararía abandonado.

### Progreso y ETA con el ritmo de esta corrida

El porcentaje sale de segundos de audio ya transcritos, no de un temporizador.
El ETA se calcula con `rate = elapsed / seconds_computed` **medido en esa misma
corrida**, no con un RTF fijo: la máquina de desarrollo y el servidor rinden
distinto — el RTF medido aquí es el de una laptop concreta y no se puede dar por
bueno en otra CPU, con otro número de núcleos asignados.

Hay una distinción que parece un detalle y no lo es: `seconds_computed` (audio
realmente transcrito ahora) va separado de `seconds_done` (audio listo, incluido
lo que vino de checkpoint). Al reanudar, los trozos recuperados cuentan como
avance pero no consumieron tiempo; sin separarlos, el ritmo sale absurdamente
rápido y el ETA anuncia 0 s para trabajo que todavía va a tardar quince minutos.
Y si en esta corrida no se computó nada, el ETA es `null` — que el cliente
muestre "sin estimación" es mejor que prometer un cero falso.

### Los números

Fase 0, 23–25/08, sobre grabaciones reales en español. Máquina de referencia:
AMD Ryzen 5 5500U, 6 núcleos / 12 hilos, 18 GB RAM, **sin GPU**, 6 hilos en
todas las corridas. RTF = segundos de proceso por segundo de audio; <1 es más
rápido que tiempo real.

| Corrida | Modelo | RTF | RAM pico | WER vs. ref |
|---|---|---|---|---|
| 66 min, un solo golpe | `large-v3-turbo` int8 | 0.708 | 4.6 GB | 8.9 % |
| 66 min, un solo golpe | `small` int8, 12 hilos | 0.48 | 3.8 GB | 16.3 % |
| 66 min, whisper.cpp | `large-v3-turbo-q5_0` | 0.781 | 1.7 GB | 9.8 % |
| 10 min, un solo golpe | `large-v3` int8 | 1.979 | 3.1 GB | — |
| **por trozos** | `large-v3-turbo` int8 | **0.51** | **1.46 GB** | — |

Por eso el default es `large-v3-turbo` en `int8` y por trozos: es la única
combinación que da la calidad del modelo grande sin el costo del modelo grande.
`large-v3` a RTF 1.979 significa que tres horas de audio tardarían casi seis;
`small` es el plan B para una máquina lenta, y cuesta casi el doble de error.

Ninguno de estos números es del servidor donde esto corra: `docs/despliegue-vps.md`
trae el procedimiento para medir el RTF real de la máquina de destino, porque es
el dato que decide qué se le puede prometer al usuario.

### Diarización: medida y aplazada, no olvidada

Saber quién habló está implementado y medido en `benchmark/diarize.py`, y **no**
entró al producto. `pyannote/speaker-diarization-3.1` sobre el clip de 10 minutos:
586.8 s de proceso, **RTF 0.978**, 2.4 GB de RSS pico. Eso prácticamente duplica
el tiempo total de un trabajo (0.978 encima del ~0.5 del STT) y sube el piso de
memoria del worker.

A eso se suman otras cuatro que la medición dejó ver:

- arrastra `torch` (~2 GB) a la imagen, por un extra opcional;
- necesita **tres** repos *gated* de Hugging Face, no dos: pyannote 4.x saca el
  PLDA de `speaker-diarization-community-1`;
- `torchcodec` no funciona con una build estática de FFmpeg — hay que pasarle el
  waveform ya decodificado;
- la alineación tiene que hacerse **por palabra, no por segmento**: el 3 % de
  los segmentos abarca a dos hablantes, y alinear por segmento se traga la
  intervención del otro.

Duplicar el costo de cada transcripción por una función que además todavía
alinea mal no valía la pena antes de que lo básico fuera sólido. Así que
`diarize=true` responde **501 explícito** en vez de aceptarse en silencio: la
bandera se guarda en el job, pero nadie se queda esperando hablantes que no van
a llegar.

### Qué pasa cuando algo se rompe a media transcripción

Cada trozo deja un checkpoint, así que una falla a las 2 h 50 de un audio de
3 h se retoma desde el último trozo completo, no desde cero. El checkpoint
guarda su **firma** (modelo, tipo de cómputo, tamaño de trozo, idioma, VAD) y
sus límites, y solo se reusa si coincide: sin eso, cambiar el tamaño de trozo
entre dos intentos hacía que un trozo viejo se solapara con los nuevos y el
resultado repitiera un pedazo del audio, entregado como transcripción buena.

- **Falla del pipeline** (ffmpeg, disco, modelo): hasta 3 intentos con backoff
  exponencial, 30 s duplicando hasta un tope de 1 h. Agotados, el `Recording`
  queda en `FAILED` con un mensaje legible.
- **El worker muere y vuelve**: al arrancar recupera sus propios trabajos por
  hostname y los devuelve a la cola de inmediato, sin cobrarles el intento —
  no fracasaron, se murió quien los tenía. Antes de esto, un `Recording` se
  quedaba congelado en "PROCESSING 62 %" durante 8 horas con el worker sano y
  ocioso al lado.
- **El worker muere y no vuelve** (otra máquina): red de seguridad por lease de
  30 min. El lease **no** es el timeout del trabajo (8 h): confundirlos era
  justo lo que hacía esperar 8 horas por un job huérfano.
- **`Recording` en `PROCESSING` sin ningún job detrás**: se detecta sin depender
  del tiempo y vuelve a `QUEUED`.
- **Se cae la base justo al registrar el fallo**: se atrapa y se deja que el
  lease lo recupere, en vez de que una segunda excepción mate al proceso.
- **Apagado limpio** (`SIGTERM`): suelta el job antes de salir.

El texto se escribe a disco **antes** del commit de la base, para que un fallo
al escribir todavía sea reintentable; la limpieza de archivos intermedios corre
**después** del commit, para que no pueda tumbar un trabajo ya terminado. Y lo
que ve el usuario en un error nunca lleva rutas del servidor ni trazas: eso va
al log estructurado.

## Desarrollo

```bash
uv sync --extra dev
uv run pytest                    # rápido: usa un motor falso, sin modelos
uv run pytest -m slow            # lento: modelos y audio reales, a propósito
uv run ruff check src tests
```

Los tests que necesitan base de datos levantan un PostgreSQL en Docker solos. Si
estás en Windows, usa siempre `127.0.0.1` y nunca `localhost` en las cadenas de
conexión: el intento de IPv6 hace que cada conexión tarde segundos.

`benchmark/` guarda la Fase 0, donde se midió qué motor y qué modelo aguantan
esto en CPU. Sus resultados no se versionan: son transcripciones de reuniones
reales.

## Estado

MVP. Funciona: subir, transcribir, consultar, descargar.

Todavía no: **diarización** (saber quién habló). El código existe y está medido,
pero duplica el tiempo de proceso, así que no entra hasta que la transcripción
esté sólida. Tampoco hay multiusuario, cliente web ni resúmenes automáticos.

## Licencia

MIT. Ver [`LICENSE`](LICENSE).
