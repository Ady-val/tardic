---
Guía de despliegue de Tardic en el VPS de producción, para que la siga el Claude
Code que corre en el servidor. Ese VPS ya tiene 25 contenedores de
producción detrás de un
nginx que sirve el apex. **El objetivo de cada paso es no tocarlos.**
---

# Despliegue de Tardic en el VPS

## 0. Antes de nada: qué es Tardic y cómo se prueba

El usuario sube un audio por HTTP y, minutos u horas después, hay una
transcripción disponible. Se prueba con la colección de Postman
(`postman/tardic.postman_collection.json`), no con un navegador.

## 1. Medir el host ANTES de tocar nada

No se sabe todavía cuántos núcleos ni cuánta RAM libre tiene el VPS. Corre
esto primero y guarda la salida — de aquí sale `TARDIC_WORKER_CPUS`:

```bash
nproc                    # núcleos totales del VPS
free -h                  # RAM total / usada / libre
df -h /                  # espacio en disco (el volumen de audio y el
                          # modelo de ~1.6 GB viven bajo /var/lib/docker)
uptime                   # load average de los últimos 1/5/15 min
docker ps --format '{{.Names}}\t{{.Ports}}'   # qué puertos ya están ocupados
docker stats --no-stream --format \
  'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'  # cuánto están usando
                                                    # de verdad los 25
                                                    # contenedores existentes
```

**Cómo elegir `TARDIC_WORKER_CPUS`** con esos números:

- Si el load average de 5 min ya anda cerca del número de núcleos (`nproc`),
  el host está ocupado: deja `TARDIC_WORKER_CPUS=1.0` y no subas hasta medir
  en una ventana de baja carga.
- Si hay carga baja, regla simple: `TARDIC_WORKER_CPUS = nproc - 2` (deja
  siempre 2 núcleos libres para los 25 contenedores existentes), con un
  mínimo de 1.0 y sin pasar de `nproc - 1`.
  - VPS de 4 núcleos, carga baja → `TARDIC_WORKER_CPUS=2`
  - VPS de 8 núcleos, carga baja → `TARDIC_WORKER_CPUS=4` a `5`
- Si `free -h` muestra menos de ~3 GB libres, no subas `TARDIC_WORKER_MEM_LIMIT`
  por encima de lo que sobra con margen: el benchmark del 25/08 midió ~1.5-2 GB
  reales con `large-v3-turbo` en int8, pero el límite del compose está puesto
  en 2 GB a propósito por encima de eso (ver comentario en
  `docker-compose.yml`, servicio `worker`) — no lo bajes de ahí.

Estos valores van en `.env`, no en el YAML: así se ajustan sin tocar código.

## 2. Clonar, configurar y levantar

```bash
git clone <url-del-repo> tardic
cd tardic
cp .env.example .env
```

Edita `.env`:

- `TARDIC_API_KEY`: genera una real con `openssl rand -hex 32`. El validador
  de `config.py` rechaza el valor de ejemplo.
- `TARDIC_DB_PASSWORD` / `TARDIC_DATABASE_URL`: una contraseña real, y que la
  URL use esa MISMA contraseña (son dos variables porque Postgres necesita
  las piezas sueltas y la app necesita la cadena completa).
- `TARDIC_API_PORT`: **elige un puerto libre.** Este host ya tiene 25
  contenedores; no asumas que 8080 está disponible. Revisa con
  `docker ps --format '{{.Ports}}'` y `ss -ltnp | grep LISTEN` antes de fijar
  el valor. Publica solo este puerto hacia localhost si nginx corre en el
  mismo host (ver paso 3).
- `TARDIC_WORKER_CPUS` / `TARDIC_WORKER_MEM_LIMIT`: con los números del
  paso 1.

Levanta el stack:

```bash
docker compose up -d --build
docker compose ps
```

Orden real de arranque (lo decide `depends_on` en `docker-compose.yml`, no
hace falta hacerlo a mano): `db` sano → `migrate` corre y sale con éxito →
`api` y `worker` arrancan. Si `migrate` falla, `api` y `worker` **no**
arrancan — es la señal de que algo en el esquema está mal, no un bug del
compose.

Verificar salud:

```bash
docker compose ps                       # api y db deben decir "healthy"
curl -s http://localhost:${TARDIC_API_PORT:-8080}/health | python3 -m json.tool
docker compose logs -f worker            # confirma que arrancó sin traceback
```

`worker_seen_seconds_ago` en la respuesta de `/health` te dice **cuántos
segundos hace que el worker dio señales de vida**. Léelo así:

- un número pequeño y que se mueve → el worker está vivo y trabajando;
- `null` → el worker **nunca** ha latido: o no arrancó, o no comparte el
  volumen de datos con la API (revisa que ambos monten `audio-data`);
- un número que solo crece → se atoró o murió.

A propósito **no tumba el healthcheck de la API**: la API está sana aunque el
worker no lo esté, y distinguir ambas cosas es justo lo que evita reiniciar el
contenedor equivocado. Confírmalo igual con `docker compose ps` y
`docker compose logs worker`.

## 3. nginx: exponer el subdominio

El host sirve el apex con nginx fuera de este compose. Tardic solo publica
`TARDIC_API_PORT` en localhost; nginx hace de proxy hacia ahí.

**Valores concretos que hay que poner, no dejar el default de nginx:**

- `client_max_body_size`: el default de nginx (1 MB) **rechaza** cualquier
  audio real. `TARDIC_MAX_UPLOAD_BYTES` en `.env` es 1 GiB por defecto
  (~4 h de m4a) — nginx tiene que permitir al menos eso. Usa
  `client_max_body_size 1100M;` (un margen sobre el 1 GiB del límite de la
  app; si subes `TARDIC_MAX_UPLOAD_BYTES`, sube esto también).
- `proxy_read_timeout` / `proxy_send_timeout`: una subida de 1 GiB en una
  conexión lenta puede tardar minutos. El default de nginx (60s) corta la
  subida a medias. Usa `300s` (5 min) como piso razonable; si el VPS tiene
  subida lenta de verdad, súbelo más.

Bloque de servidor de ejemplo (ajusta `server_name` y las rutas de
certificado a como ya estén los otros 25 sitios de este host):

```nginx
server {
    listen 443 ssl http2;
    server_name tardic.tu-servidor.example.com;

    ssl_certificate     /etc/letsencrypt/live/tardic.tu-servidor.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tardic.tu-servidor.example.com/privkey.pem;

    client_max_body_size 1100M;

    location / {
        proxy_pass http://127.0.0.1:8080;   # el mismo valor de TARDIC_API_PORT
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}

server {
    listen 80;
    server_name tardic.tu-servidor.example.com;
    return 301 https://$host$request_uri;
}
```

Recarga nginx sin tumbar los otros 25 sitios: `nginx -t && systemctl reload nginx`
(o el equivalente si nginx corre en su propio contenedor — revisa cómo está
montado antes de asumir `systemctl`).

## 4. Medir el RTF real del VPS

El benchmark de la Fase 0 (25/08, en una laptop) midió RTF 0.51 con
`large-v3-turbo` chunked. **Ese número no es el del VPS** — CPU distinta,
`TARDIC_WORKER_CPUS` distinto. Hay que medirlo ahí:

1. Sube un audio de duración conocida por Postman (`01 - Subir audio`).
2. Sigue el progreso con `GET /recordings/{id}` hasta `COMPLETED`.
3. El JSON del transcript (`GET /recordings/{id}/transcript`) trae
   `processing_time_seconds` y `duration_seconds`. RTF real =
   `processing_time_seconds / duration_seconds`.

Qué hacer si el RTF es malo (mayor a ~1, es decir, tarda más que la duración
del audio):

- Revisa que `TARDIC_STT_COMPUTE_TYPE=int8` (no `float16`/`float32`: en CPU
  int8 es varias veces más rápido y es lo que se validó en el benchmark).
- Revisa que `TARDIC_WORKER_CPUS` no haya quedado en un valor bajo por
  descuido tras el paso 1.
- Revisa `docker stats` mientras corre: si el worker no llega a saturar los
  CPUs que le diste, el cuello de botella puede ser I/O de disco (el volumen
  `hf-cache`/`audio-data` en un disco lento del proveedor) y no CPU.
- No compares contra el número de la laptop del benchmark sin ajustar por
  núcleos: es un dato de referencia, no un SLA.

## 5. Diagnóstico

- **Logs**: `docker compose logs -f api`, `docker compose logs -f worker`,
  `docker compose logs migrate` (para ver si las migraciones corrieron bien
  la última vez).
- **¿Está vivo el worker?** Lo más rápido es `worker_seen_seconds_ago` en
  `GET /health`: si es un número pequeño y se mueve, está vivo (ver paso 2
  para leerlo bien). Confírmalo con `docker compose ps` (debe decir `Up`, sin
  reinicios en bucle) y `docker compose logs -f worker` sin traceback nuevo.
- **El modelo no descarga**: la primera vez que un `worker` transcribe algo,
  descarga ~1.6 GB a `hf-cache`. Si se queda pegado:
  - `docker exec -it tardic-worker sh -c 'du -sh $HF_HOME'` para ver si algo
    sí se está bajando (aunque lento).
  - Revisa que el VPS tenga salida a internet sin proxy raro:
    `docker exec -it tardic-worker sh -c 'python -c "import urllib.request as u; print(u.urlopen(\"https://huggingface.co\", timeout=5).status)"'`.
  - Revisa espacio en disco (`df -h`): un volumen lleno corta la descarga a
    medias sin un error obvio.
- **Reiniciar sin perder trabajo en curso**: `docker compose restart worker`
  manda SIGTERM antes de matar (por el `exec` en `entrypoint-worker.sh`, no
  hay un `sh -c` intermedio que se coma la señal). El trozo que estaba a
  medias se pierde, pero los trozos ya completados quedan en el checkpoint
  (`audio/<id>/chunks/`, volumen `audio-data`) y el job se reintenta desde
  ahí, no desde cero — es la reanudación de RF-10. Nunca hace falta
  `docker compose down -v` para "limpiar" un worker atorado.

## 6. Qué NO hacer

- **Nunca `docker volume prune`.** `pgdata`, `audio-data` y `hf-cache` son
  datos reales (transcripciones de usuarios, el modelo descargado). Un
  prune de este host se lleva por delante volúmenes de los otros 24
  contenedores también si no están en uso en ese momento — no es un riesgo
  aislado a Tardic.
- **Nunca subir `.env` al repo ni pegarlo en un chat/issue.** Trae la
  contraseña de la base y la API key real. Si se filtra, rótala
  (`openssl rand -hex 32` de nuevo) y reinicia `api`.
- **No bajar `TARDIC_WORKER_MEM_LIMIT` de 2g** sin volver a medir memoria:
  es el número que ya midió el benchmark, no un valor arbitrario.
- **No subir `TARDIC_WORKER_CPUS` a ojo** sin repetir el paso 1. Si producción
  empieza a sentirse lenta después de un deploy de Tardic, ese es el primer
  número a revisar y bajar.
