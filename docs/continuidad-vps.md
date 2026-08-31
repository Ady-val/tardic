# Continuidad — qué hacer al recibir esto en el VPS

Para el agente que toma el proyecto en el VPS de producción. Léelo completo antes de
ejecutar nada; `CLAUDE.md` tiene las reglas de operación y `README.md` explica
qué es el sistema.

El MVP está construido, con 63 pruebas en verde y **verificado de punta a punta
en Docker en la máquina de desarrollo**: se subió audio real por HTTP, se siguió
el progreso y se descargó la transcripción correcta. Lo que **no** está
verificado es cómo se comporta en este servidor. De eso trata este documento.

---

## Paso 1 · Medir la máquina antes de tocar nada

Nadie ha medido este VPS. Todos los números del repo (RTF 0.48–0.51, 1.46 GB de
RAM) vienen de una laptop Ryzen 5 con 6 núcleos.

```bash
nproc                  # núcleos
free -h                # RAM libre REAL, no la total
df -h /                # espacio: modelo 1.6 GB + audios + base
uptime                 # carga actual, con producción encima
docker ps --format '{{.Names}}\t{{.Ports}}'   # qué puertos están tomados
ss -ltnp | head -30
```

**Decide con eso:**

| Dato | Qué hacer |
|---|---|
| Núcleos | `TARDIC_WORKER_CPUS` = núcleos − 2, mínimo 1. Deja siempre margen para producción. |
| RAM libre | Necesitas ~2.5 GB para el worker. **Si hay menos, párate y repórtalo** en vez de descubrirlo con un audio de 3 h a medias. |
| Disco | Cada hora de audio son ~115 MB de WAV original. Más 1.6 GB del modelo. |
| Puerto | 8080 casi seguro está ocupado: elige uno libre y ponlo en `TARDIC_API_PORT`. |

⚠️ **Este host corre producción**: otros servicios,
unos 25 contenedores. Un worker de Whisper toma todos los núcleos que
le des durante horas. Las defensas están puestas (`cpus`, `mem_limit`, `nice`,
un solo trabajo a la vez) — **no las quites para ir más rápido**.

## Paso 2 · Levantar

```bash
cp .env.example .env
# edita: TARDIC_API_KEY (openssl rand -hex 32), TARDIC_DB_PASSWORD,
#        TARDIC_API_PORT, TARDIC_WORKER_CPUS
docker compose up -d
docker compose ps      # migrate debe salir con exit 0; api healthy
```

La contraseña de la base se escribe **una sola vez**, en `TARDIC_DB_PASSWORD`.
No hay que repetirla en ningún otro lado.

## Paso 3 · Probar

```bash
./scripts/smoke-test.sh
```

Cubre: salud, las tres formas de fallar la autenticación, subida de audio,
rechazo de lo que no es audio, el ciclo completo hasta `COMPLETED`, descarga del
TXT y del JSON, errores bien formados y borrado. Sale con código 1 si algo
falla.

La primera corrida **descarga el modelo (~1.6 GB)** y por eso tarda. No está
colgado.

Si algo falla, no lo des por bueno: `docker compose logs --tail=100 worker`.

## Paso 4 · Medir el RTF real de esta máquina

Es el número que decide qué se le puede prometer al usuario.

```bash
# con un audio real de varios minutos
time curl -X POST http://127.0.0.1:$PUERTO/v1/recordings \
     -H "X-API-Key: $KEY" -F "file=@audio.m4a"
# luego consulta el estado hasta COMPLETED y calcula:
#   RTF = segundos_de_proceso / segundos_de_audio
```

| RTF medido | Qué significa |
|---|---|
| < 0.6 | Bien. Una sesión de 3 h se procesa en ~1 h 45. |
| 0.6 – 1.0 | Aceptable. 3 h de audio ≈ 3 h de proceso: worker nocturno. |
| > 1.5 | Problema. Repórtalo: hay que subir `TARDIC_WORKER_CPUS`, bajar a modelo `small`, o aceptar que las sesiones largas tardan toda la noche. |

`TARDIC_STT_MODEL=small` es el plan B: RTF ~0.31 medido, a cambio de más
errores (WER 14 % contra 9 %).

## Paso 5 · La prueba de verdad: un audio largo

Nada de esto está probado con una grabación de horas en este hardware. Cuando
Adal suba una sesión real:

- Sigue el progreso: debe avanzar trozo a trozo, sin quedarse clavado.
- Vigila la memoria: `docker stats tardic-worker`. Debe mantenerse **plana**
  (~1.5–2 GB). Si crece sin parar, el chunking no está funcionando y hay que
  reportarlo.
- Al terminar, comprueba que en `/data/audio/<id>/` quedaron solo
  `original.<ext>` y `transcript.txt`. Si sigue el `audio.wav` o el directorio
  `chunks/`, la limpieza falló.

## Qué reportar de vuelta

1. Núcleos, RAM libre y disco del VPS.
2. `TARDIC_WORKER_CPUS` que elegiste y por qué.
3. Salida del `smoke-test.sh`.
4. **El RTF medido**, que es el dato que falta desde el principio.
5. Si la memoria del worker se mantuvo plana con un audio largo.

---

## Trabajo pendiente conocido, por prioridad

Nada de esto bloquea el MVP. Está ordenado por lo que más valor da después.

### 1. nginx y el subdominio

Hoy la API solo escucha en el puerto del host. Para llegar desde Postman fuera
del servidor hace falta el vhost. **Lo crítico**: `client_max_body_size` por
defecto rechaza un archivo de 185 MB, y los timeouts cortan subidas largas.
Está detallado en `docs/despliegue-vps.md`.

### 2. Diarización — saber quién habló

Hoy `diarize=true` responde **501**. El código existe y está medido
(`benchmark/diarize.py`), pero:

- **duplica el tiempo de proceso** (RTF 0.978 contra 0.48 del STT);
- necesita **tres** repos *gated* de Hugging Face, no dos: pyannote 4.x saca el
  PLDA de `speaker-diarization-community-1`;
- `torchcodec` no sirve con FFmpeg estático — hay que pasarle el waveform ya
  decodificado, como hace `benchmark/diarize.py`;
- arrastra `torch` (~2 GB), por eso es un extra opcional y no está en la imagen;
- **la alineación debe hacerse por PALABRA, no por segmento**: el 3 % de los
  segmentos abarca a dos hablantes y el alineador actual se traga la
  intervención del otro. Usar `word_timestamps=True`.

### 3. Que el usuario suba desde el teléfono

Hoy se sube por HTTP con una API key. Falta decidir si el cliente es una PWA o
simplemente compartir el archivo de la grabadora. Es la Fase 6 del plan.

### 4. Capa de inteligencia

Resúmenes, acuerdos, compromisos y responsables sobre la transcripción ya
estructurada. Es la Fase 7 y el verdadero motivo del proyecto: alimentar CRM,
memoria empresarial y seguimiento.

---

## Trampas ya pagadas — no las vuelvas a pisar

- **Nada de audio ni transcripciones de clientes en el repo.** `benchmark/results/`
  está ignorado porque guarda conversaciones reales; solo pasan los CSV.
- **En Windows, `localhost` cuesta minutos** por el timeout de IPv6; usa
  `127.0.0.1`. En Linux da igual.
- **Todo tiempo que se compare contra la base sale de la base** (`SELECT now()`),
  nunca del reloj del proceso: el drift entre host y contenedor dejaba trabajos
  invisibles.
- **Los checkpoints solo se reusan si coincide la firma** (modelo, tamaño de
  trozo, rango). Cambiarla y reusarlos producía transcripciones con texto
  duplicado.
- **El worker recupera sus propios trabajos al arrancar** por su hostname. Si
  cambias cómo se identifica, vuelve el bug de los trabajos huérfanos.
- **`docker volume prune`: jamás.** `pgdata` y `audio-data` son datos reales.
