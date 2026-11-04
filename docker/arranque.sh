#!/bin/bash
# Arranque de la imagen todo-en-uno: Postgres, luego la API, luego el worker.
#
# El orden importa: el worker consulta la base nada más arrancar y la API sirve /health,
# que es lo que mira el HEALTHCHECK. Al revés, el primer log de una instalación nueva
# empieza con una conexión rechazada y parece rota sin estarlo.
set -euo pipefail

log() { echo "[coconut] $*"; }

# 1. /data tiene que ser un volumen
#
# Con Postgres dentro del contenedor, arrancar sin volumen deja la serie de precios en la
# capa de escritura: desaparece en el primer `docker rm` y no se puede reconstruir hacia
# atrás. Se prefiere no arrancar a perderla en silencio.
#
# Un volumen (nombrado o bind) aparece en /proc/mounts; la capa del contenedor, no.
if ! grep -q ' /data ' /proc/mounts; then
  # sin-rastro-ok: el recuadro se imprime en el terminal de quien arranca la imagen.
  cat >&2 <<'FIN'

  ╔═══════════════════════════════════════════════════════════════════════╗
  ║  /data no está montado como volumen. No se arranca.                   ║
  ╠═══════════════════════════════════════════════════════════════════════╣
  ║  Ahí vive la base de datos, y con ella la serie de precios, que no se ║
  ║  puede reconstruir hacia atrás: cada día que no se toma se pierde     ║
  ║  para siempre. Sin volumen desaparece al recrear el contenedor.       ║
  ║                                                                       ║
  ║  Arranca así:                                                         ║
  ║                                                                       ║
  ║    docker volume create coconut-datos                                 ║
  ║    docker run -d --name coconut -p 8140:8000 \                        ║
  ║      -v coconut-datos:/data ghcr.io/jesus33lcc/laliga-fantasy-api     ║
  ║                                                                       ║
  ║  Si quieres datos efímeros para probar, añade:                        ║
  ║    -e COCONUT_SIN_VOLUMEN=acepto-perder-los-datos                     ║
  ╚═══════════════════════════════════════════════════════════════════════╝

FIN
  if [ "${COCONUT_SIN_VOLUMEN:-}" != "acepto-perder-los-datos" ]; then
    exit 1
  fi
  log "arrancando sin volumen porque lo has pedido: los datos se perderán."
fi

mkdir -p /data/estado

# 2. Postgres
#
# `DATABASE_URL` del entorno MANDA. Con una puesta no se arranca el Postgres interno: los
# datos van a ese servidor, y levantar el de dentro deja un servidor escuchando que nadie
# usa y un esquema aplicado donde la aplicación no mira.
#
# Las tablas del destino las crea `db.asegurar_tablas()` al arrancar la API, que las
# conoce todas; `db/001_schema.sql` declara 12 y los módulos crean el resto.
# `BIN` vacío es la señal de que no hay Postgres interno: lo mira `parar()`.
BIN=""

arrancar_postgres_interno() {
  mkdir -p /data/postgres
  chown -R postgres:postgres /data/postgres
  chmod 700 /data/postgres

  BIN="$(ls -d /usr/lib/postgresql/*/bin | head -1)"

  if [ ! -s "$PGDATA/PG_VERSION" ]; then
    log "primera vez: creando la base de datos en $PGDATA"
    gosu postgres "$BIN/initdb" -D "$PGDATA" -E UTF8 --locale=C >/dev/null
    # Solo localhost: los tres procesos viven en este contenedor. Exponerlo sería regalar
    # la base a la red del host.
    echo "listen_addresses = 'localhost'" >> "$PGDATA/postgresql.conf"
  fi

  log "arrancando Postgres"
  gosu postgres "$BIN/pg_ctl" -D "$PGDATA" -o "-c listen_addresses=localhost" -w -t 60 start

  # Bucle con `pg_isready` y no un `sleep` fijo: el arranque tarda mucho más en una
  # máquina pequeña que en un portátil, y un sleep calibrado para una falla en la otra.
  for i in $(seq 1 60); do
    gosu postgres "$BIN/pg_isready" -q && break
    sleep 1
  done

  local CLAVE="${POSTGRES_PASSWORD:-}"
  if [ -z "$CLAVE" ]; then
    # Sin clave declarada se genera una y se guarda en el volumen: así no hay credencial
    # por defecto igual para todo el mundo, y sobrevive a los reinicios.
    if [ ! -s /data/.pgpass ]; then
      head -c 32 /dev/urandom | base64 | tr -d '/+=' > /data/.pgpass
      chmod 600 /data/.pgpass
    fi
    CLAVE="$(cat /data/.pgpass)"
  fi

  # La clave va a dos sitios con reglas distintas, y la generada aquí no las rompe pero una
  # puesta en `POSTGRES_PASSWORD` sí: en SQL una comilla simple cierra la cadena, y en el DSN
  # los caracteres `@ / : #` son separadores. Sin esto, una clave con una arroba produce una
  # URL de conexión mal partida y el fallo aparece lejos, al conectar.
  local CLAVE_SQL="${CLAVE//\'/\'\'}"
  local CLAVE_URL
  CLAVE_URL="$(python -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$CLAVE")"

  gosu postgres psql -v ON_ERROR_STOP=1 --quiet <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${POSTGRES_USER}') THEN
    CREATE ROLE ${POSTGRES_USER} LOGIN SUPERUSER;
  END IF;
END \$\$;
ALTER ROLE ${POSTGRES_USER} PASSWORD '${CLAVE_SQL}';
SQL
  gosu postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${POSTGRES_DB}'" \
    | grep -q 1 || gosu postgres createdb -O "${POSTGRES_USER}" "${POSTGRES_DB}"

  # La clave hace falta en el paso 3 para aplicar el esquema con `psql`.
  CLAVE_PG="$CLAVE"
  export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${CLAVE_URL}@localhost:5432/${POSTGRES_DB}"
}

if [ -n "${DATABASE_URL:-}" ]; then
  log "DATABASE_URL viene del entorno: los datos van ahí y el Postgres interno no arranca"
else
  arrancar_postgres_interno
fi
export COCONUT_DB_URL="$DATABASE_URL"

# 3. Esquema
#
# Solo los de Postgres: los `*_sqlite.sql` usan tipos que Postgres no conoce (`JSONTEXT`)
# y fallan enteros. Aplicarlos igual llena el arranque de errores que son normales, y con
# ese ruido un fallo de verdad no se distingue.
#
# `ON_ERROR_STOP=0` porque los DDL llevan `IF NOT EXISTS` y reaplicarlos produce avisos
# benignos. Lo que NO se silencia es un `ERROR:`, que sí hay que ver.
#
# Con base externa no se aplica nada: este `psql` apunta a `localhost`, que ahí no es la
# base de la aplicación.
if [ -n "${CLAVE_PG:-}" ] && [ -d /app/db ]; then
  for f in /app/db/*.sql; do
    [ -e "$f" ] || continue
    case "$f" in *_sqlite.sql) continue ;; esac
    log "esquema: $(basename "$f")"
    salida="$(PGPASSWORD="$CLAVE_PG" psql -h localhost -U "${POSTGRES_USER}" \
      -d "${POSTGRES_DB}" -v ON_ERROR_STOP=0 --quiet -f "$f" 2>&1)" || true
    if echo "$salida" | grep -q '^psql.*ERROR:'; then
      log "  ERRORES aplicando $(basename "$f"):"
      echo "$salida" | grep '^psql.*ERROR:' | head -10 | sed 's/^/  [coconut]   /' >&2
    fi
  done
fi

# 4. API y worker
#
# `COCONUT_WORKER_VIA_API=1` por defecto: un solo cliente hablando con LaLiga, para que el
# límite de 0,5 req/s se aplique de verdad. Con dos clientes cada uno tiene su propio cubo
# y el límite no se cumple entre ellos.
export COCONUT_WORKER_VIA_API="${COCONUT_WORKER_VIA_API:-1}"
export COCONUT_API_INTERNA="${COCONUT_API_INTERNA:-http://localhost:8000}"
export COCONUT_ESTADO_DIR="${COCONUT_ESTADO_DIR:-/data/estado}"

parar() {
  log "parando"
  kill "${PID_WORKER:-}" "${PID_API:-}" 2>/dev/null || true
  # `$BIN` está vacío cuando la base es externa: no hay Postgres interno que parar.
  #
  # Va en un `if` y no en `[ … ] && …`: con `set -e`, una lista `&&` con el lado izquierdo
  # falso devuelve no-cero y aborta la función antes del `exit 0`. El contenedor sale
  # entonces con 1 ante un SIGTERM normal.
  if [ -n "${BIN:-}" ]; then
    gosu postgres "$BIN/pg_ctl" -D "$PGDATA" -m fast -w stop >/dev/null 2>&1 || true
  fi
  exit 0
}
trap parar SIGTERM SIGINT

log "arrancando la API en :8000"
python -m uvicorn fantasy_coconut.api.main:app --host 0.0.0.0 --port 8000 &
PID_API=$!

# El worker espera a que la API responda porque sus lecturas pasan por ella. Tiene
# fallback al cliente directo, pero usarlo desperdicia el cliente compartido en la
# primera toma.
#
# Se espera a que CONTESTE, no a que conteste 200: /health devuelve 503 en una instalación
# limpia (sin precios, sin plantillas y sin refresh token) y esa es su respuesta correcta.
#
# El fallo de curl se absorbe FUERA de la sustitución de comando. Dentro no vale: curl ya
# imprime `000` cuando no conecta, así que un `|| echo 000` añadiría un segundo; y con
# `set -e`, una asignación cuya sustitución falla aborta el script entero.
for i in $(seq 1 45); do
  codigo=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/health 2>/dev/null) \
    || codigo=000
  case "$codigo" in
    000|"") sleep 2 ;;                       # aún no hay nadie escuchando
    *) log "la API responde (HTTP $codigo)"; break ;;
  esac
done

log "arrancando el worker"
python -m fantasy_coconut.ingest.scheduler &
PID_WORKER=$!

log "listo: http://localhost:8000/app/"

# `wait -n` devuelve en cuanto muere cualquiera de los dos, y entonces cae el contenedor
# entero para que Docker lo reinicie. Preferible a quedarse con la API viva y el worker
# muerto sin que nada lo indique.
wait -n "$PID_API" "$PID_WORKER"
log "un proceso ha terminado; se para el contenedor para que Docker lo reinicie"
parar
