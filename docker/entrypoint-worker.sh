#!/bin/sh
# Arranque del worker. Dos cosas que NO puede hacer `docker-compose.yml` solo
# con `cpus:`/`mem_limit:`:
#
#   1. Bajar la prioridad de scheduling (nice) del proceso frente a los otros
#      25 contenedores de producción del host. `cpus:` limita CUÁNTA CPU en
#      total puede usar el contenedor (cgroup cfs_quota); `nice` decide QUIÉN
#      gana cuando varios procesos compiten por el mismo núcleo en ese
#      instante. Ambos juntos son la defensa real: sin nice, un worker con
#      cpus=2 en un host de 4 núcleos igual puede ganarle turnos de CPU a un
#      contenedor de producción con prioridad normal.
#   2. Exec real (no un `sh -c` que deje al script como PID 1 huérfano): así
#      `tardic-worker` recibe SIGTERM directo de `docker stop` y puede cerrar
#      el trozo en curso en vez de que lo mate el kernel a los 10s de gracia.
set -eu

NICE_LEVEL="${TARDIC_WORKER_NICE:-10}"

echo "[entrypoint-worker] nice=${NICE_LEVEL} cpus(limitado por compose, ver docker-compose.yml)"

exec nice -n "${NICE_LEVEL}" tardic-worker
