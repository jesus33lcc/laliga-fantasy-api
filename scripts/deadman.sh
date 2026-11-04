#!/usr/bin/env bash
# Vigilante externo de LaLiga Fantasy Coconut.
#
# Vive FUERA de Docker a propósito: si el stack entero se cae, el propio sistema no
# puede avisar de su muerte. Este script es lo único que sigue vivo para contarlo.
#
# Instalar (cron del host, cada hora):
#   0 * * * * /ruta/a/coconut/scripts/deadman.sh >> /var/log/coconut-deadman.log 2>&1

set -uo pipefail

ENV_FILE="/ruta/a/coconut/.env"
URL="http://TU_SERVIDOR:8140/health"
ESTADO="./datos/state/deadman.estado"
SILENCIO=21600   # 6 h entre avisos del mismo problema

[ -r "$ENV_FILE" ] || { echo "no puedo leer $ENV_FILE"; exit 1; }
TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
CHAT=$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2-)

avisar() {
  [ -n "$TOKEN" ] && [ -n "$CHAT" ] || { echo "sin credenciales de Telegram"; return; }
  curl -s -o /dev/null --max-time 20 \
    -d "chat_id=$CHAT" --data-urlencode "text=$1" -d "parse_mode=HTML" \
    "https://api.telegram.org/bot${TOKEN}/sendMessage"
}

# Antirrebote: no repetir el mismo aviso cada hora.
ya_avisado() {
  local clave="$1" ahora previo
  ahora=$(date +%s)
  [ -f "$ESTADO" ] && previo=$(grep -F "$clave " "$ESTADO" 2>/dev/null | tail -1 | awk '{print $2}')
  if [ -n "${previo:-}" ] && [ $((ahora - previo)) -lt "$SILENCIO" ]; then return 0; fi
  mkdir -p "$(dirname "$ESTADO")"
  grep -vF "$clave " "$ESTADO" 2>/dev/null > "$ESTADO.tmp" || true
  echo "$clave $ahora" >> "$ESTADO.tmp"
  mv "$ESTADO.tmp" "$ESTADO"
  return 1
}

CODIGO=$(curl -s -o /tmp/coconut_health.json -w '%{http_code}' --max-time 25 "$URL")

if [ "$CODIGO" = "000" ]; then
  ya_avisado "caido" || avisar " <b>Fantasy Coconut NO RESPONDE</b>
La API no contesta en ${URL}.
Revisa: <code>docker compose ps</code> en /ruta/a/coconut"
  echo "$(date -Is) CAIDO"
  exit 1
fi

if [ "$CODIGO" = "503" ]; then
  FALLOS=$(python3 -c "
import json
d=json.load(open('/tmp/coconut_health.json'))
print('\n'.join('• %s: %s' % (f['que'], f['detalle']) for f in d.get('fallos', [])))
" 2>/dev/null)
  ya_avisado "degradado" || avisar " <b>fantasy-coconut degradado</b>
$FALLOS"
  echo "$(date -Is) DEGRADADO"
  exit 1
fi

echo "$(date -Is) OK"
