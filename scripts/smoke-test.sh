#!/usr/bin/env bash
# Batería de humo contra una instancia de Tardic ya levantada.
#
# Comprueba el camino feliz completo (subir → procesar → descargar) y los casos
# límite que más se rompen. Está pensada para correrse en el VPS justo después
# del primer despliegue, y otra vez después de cada cambio.
#
#   ./scripts/smoke-test.sh [URL_BASE] [API_KEY] [ARCHIVO_AUDIO]
#
# Sin argumentos toma TARDIC_API_PORT/TARDIC_API_KEY del .env del directorio
# actual y genera un audio de prueba con ffmpeg.
#
# Sale con 0 si todo pasó, 1 si algo falló. Cada prueba dice qué esperaba.
set -uo pipefail

BASE="${1:-}"
KEY="${2:-}"
AUDIO="${3:-}"

if [[ -z "$BASE" || -z "$KEY" ]]; then
    if [[ -f .env ]]; then
        # shellcheck disable=SC1091
        PORT=$(grep -E '^TARDIC_API_PORT=' .env | cut -d= -f2- | tr -d '"' | tr -d "'")
        KEY="${KEY:-$(grep -E '^TARDIC_API_KEY=' .env | cut -d= -f2- | tr -d '"' | tr -d "'")}"
        BASE="${BASE:-http://127.0.0.1:${PORT:-8080}}"
    else
        echo "Uso: $0 <url_base> <api_key> [archivo_audio]" >&2
        exit 2
    fi
fi

ok=0
fallo=0

check() { # check <descripción> <esperado> <obtenido>
    if [[ "$2" == "$3" ]]; then
        printf '  \033[32mOK\033[0m   %-46s %s\n' "$1" "$3"
        ok=$((ok + 1))
    else
        printf '  \033[31mFALLA\033[0m %-46s esperaba=%s obtuvo=%s\n' "$1" "$2" "$3"
        fallo=$((fallo + 1))
    fi
}

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "== Tardic · prueba de humo =="
echo "   destino: $BASE"
echo

# --- 1. salud -------------------------------------------------------------
echo "1) Salud"
HEALTH=$(curl -s --max-time 10 "$BASE/health")
check "GET /health responde" "200" "$(code --max-time 10 "$BASE/health")"
echo "$HEALTH" | grep -q '"database":true' \
    && check "la API ve la base de datos" "si" "si" \
    || check "la API ve la base de datos" "si" "no"
WORKER=$(echo "$HEALTH" | grep -oE '"worker_seen_seconds_ago":[0-9.]+|"worker_seen_seconds_ago":null')
if [[ "$WORKER" == *null* ]]; then
    check "el worker ha dado señales de vida" "si" "NUNCA latió (revisa el worker y el volumen)"
else
    check "el worker ha dado señales de vida" "si" "si (${WORKER##*:}s)"
fi

# --- 2. autenticación -----------------------------------------------------
echo
echo "2) Autenticación"
check "sin API key" "401" "$(code "$BASE/v1/recordings")"
check "API key incorrecta" "401" "$(code "$BASE/v1/recordings" -H 'X-API-Key: incorrecta-pero-larga-000')"
check "API key con caracteres raros" "401" "$(code "$BASE/v1/recordings" -H 'X-API-Key: ñ')"
check "listado con API key válida" "200" "$(code "$BASE/v1/recordings" -H "X-API-Key: $KEY")"

# --- 3. audio de prueba ---------------------------------------------------
echo
echo "3) Subida"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
if [[ -z "$AUDIO" ]]; then
    AUDIO="$TMP/prueba.wav"
    # 20 s: dos tonos separados por silencio, suficiente para ejercitar el
    # pipeline sin esperar una transcripción larga.
    ffmpeg -hide_banner -loglevel error -y \
        -f lavfi -i "sine=frequency=440:duration=8" \
        -f lavfi -i "anullsrc=r=16000:cl=mono:d=4" \
        -f lavfi -i "sine=frequency=880:duration=8" \
        -filter_complex '[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]' -map '[out]' \
        -ac 1 -ar 16000 -c:a pcm_s16le "$AUDIO" 2>/dev/null
fi
echo "   audio: $AUDIO ($(du -h "$AUDIO" | cut -f1))"

echo '{"x":1}' > "$TMP/no-es-audio.json"
check "archivo que no es audio" "400" "$(code -X POST "$BASE/v1/recordings" -H "X-API-Key: $KEY" -F "file=@$TMP/no-es-audio.json")"
check "subida sin credencial" "401" "$(code -X POST "$BASE/v1/recordings" -F "file=@$AUDIO")"
check "diarize=true (no implementado)" "501" "$(code -X POST "$BASE/v1/recordings" -H "X-API-Key: $KEY" -F "file=@$AUDIO" -F 'diarize=true')"

RESP=$(curl -s -X POST "$BASE/v1/recordings" -H "X-API-Key: $KEY" -F "file=@$AUDIO")
ID=$(echo "$RESP" | grep -oE '"id":"[^"]+"' | head -1 | cut -d'"' -f4)
if [[ -z "$ID" ]]; then
    echo "  FALLA no se pudo subir el audio. Respuesta: $RESP"
    exit 1
fi
check "subida aceptada" "si" "si (id=${ID:0:8}…)"

# --- 4. procesamiento -----------------------------------------------------
echo
echo "4) Procesamiento (puede tardar: la primera vez descarga el modelo, ~1.6 GB)"
ESTADO=""
INICIO=$(date +%s)
for _ in $(seq 1 240); do   # hasta 40 min
    R=$(curl -s "$BASE/v1/recordings/$ID" -H "X-API-Key: $KEY")
    ESTADO=$(echo "$R" | grep -oE '"status":"[A-Z]+"' | head -1 | cut -d'"' -f4)
    PCT=$(echo "$R" | grep -oE '"percent":[0-9]+' | head -1 | cut -d: -f2)
    printf '\r   estado=%-11s progreso=%3s%%  (%ss)' "$ESTADO" "${PCT:-0}" "$(( $(date +%s) - INICIO ))"
    [[ "$ESTADO" == "COMPLETED" || "$ESTADO" == "FAILED" ]] && break
    sleep 10
done
echo
TOTAL=$(( $(date +%s) - INICIO ))
check "termina en COMPLETED" "COMPLETED" "$ESTADO"
if [[ "$ESTADO" == "FAILED" ]]; then
    echo "   error reportado: $(echo "$R" | grep -oE '"error":"[^"]*"')"
    echo "   revisa: docker compose logs --tail=50 worker"
fi
echo "   tiempo total: ${TOTAL}s"

# --- 5. resultados --------------------------------------------------------
echo
echo "5) Resultados"
check "descarga del TXT" "200" "$(code "$BASE/v1/recordings/$ID/transcript.txt" -H "X-API-Key: $KEY")"
check "JSON estructurado" "200" "$(code "$BASE/v1/recordings/$ID/transcript" -H "X-API-Key: $KEY")"
curl -s "$BASE/v1/recordings/$ID/transcript" -H "X-API-Key: $KEY" -o "$TMP/tr.json"
grep -q '"segments"' "$TMP/tr.json" \
    && check "el JSON trae segmentos con timestamps" "si" "si" \
    || check "el JSON trae segmentos con timestamps" "si" "no"

# --- 6. errores esperados -------------------------------------------------
echo
echo "6) Errores bien formados"
check "id inexistente" "404" "$(code "$BASE/v1/recordings/00000000-0000-0000-0000-000000000000" -H "X-API-Key: $KEY")"
check "id malformado" "422" "$(code "$BASE/v1/recordings/no-es-uuid" -H "X-API-Key: $KEY")"

# --- 7. borrado -----------------------------------------------------------
echo
echo "7) Borrado"
check "DELETE" "204" "$(code -X DELETE "$BASE/v1/recordings/$ID" -H "X-API-Key: $KEY")"
check "ya no existe" "404" "$(code "$BASE/v1/recordings/$ID" -H "X-API-Key: $KEY")"

# --- resumen --------------------------------------------------------------
echo
echo "=============================================="
printf '  pasaron: %d   fallaron: %d\n' "$ok" "$fallo"
if [[ $fallo -eq 0 ]]; then
    echo "  TODO OK"
    echo
    echo "  Anota el RTF de esta máquina: ${TOTAL}s de proceso para el audio de prueba."
    echo "  Con audio real, RTF = tiempo_de_proceso / duración_del_audio."
    exit 0
fi
echo "  HAY FALLOS — no des el despliegue por bueno"
exit 1
