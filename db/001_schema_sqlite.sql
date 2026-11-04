-- Esquema SQLite de Coconut: generado, no editar a mano.
--   docker compose run --rm --no-deps worker \
--       python scripts/generar_esquema_sqlite.py > db/001_schema_sqlite.sql
--
-- Sale de introspeccionar la BD viva, porque db/001_schema.sql no declara todas las
-- tablas reales: el resto las crea el código al arrancar.


CREATE TABLE IF NOT EXISTS ajustes (
    clave                  TEXT NOT NULL,
    valor                  JSONTEXT NOT NULL,
    actualizado            TIMESTAMPTZ NOT NULL DEFAULT (now()),
    PRIMARY KEY (clave)
);

CREATE TABLE IF NOT EXISTS alert_log (
    clave                  TEXT NOT NULL,
    enviado                TIMESTAMPTZ NOT NULL DEFAULT (now()),
    PRIMARY KEY (clave)
);

CREATE TABLE IF NOT EXISTS backfill_dias (
    temporada              TEXT NOT NULL,
    fecha                  DATE NOT NULL,
    filas                  INTEGER,
    ts                     TIMESTAMPTZ NOT NULL DEFAULT (now()),
    PRIMARY KEY (temporada, fecha)
);

CREATE TABLE IF NOT EXISTS bloqueos (
    recurso                TEXT NOT NULL,
    motivo                 TEXT NOT NULL DEFAULT '',
    datos                  JSONTEXT NOT NULL DEFAULT '{}',
    creado                 TIMESTAMPTZ NOT NULL DEFAULT (now()),
    PRIMARY KEY (recurso)
);

CREATE TABLE IF NOT EXISTS decisions (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                     TIMESTAMPTZ DEFAULT (now()),
    kind                   TEXT,
    payload                JSONTEXT,
    rationale              TEXT,
    executed               BOOLEAN DEFAULT 0,
    outcome                TEXT,
    modo                   TEXT,
    accion                 TEXT,
    objetivo               TEXT,
    importe                INTEGER DEFAULT 0,
    motivo                 TEXT,
    ejecutada              BOOLEAN DEFAULT 0,
    resultado              TEXT
);

CREATE TABLE IF NOT EXISTS jobs_config (
    nombre                 TEXT NOT NULL,
    config                 JSONTEXT NOT NULL DEFAULT '{}',
    actualizado            TIMESTAMPTZ NOT NULL DEFAULT (now()),
    PRIMARY KEY (nombre)
);

CREATE TABLE IF NOT EXISTS jobs_log (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre                 TEXT NOT NULL,
    inicio                 TIMESTAMPTZ NOT NULL DEFAULT (now()),
    fin                    TIMESTAMPTZ,
    ms                     INTEGER,
    ok                     BOOLEAN,
    detalle                TEXT,
    origen                 TEXT NOT NULL DEFAULT 'cron'
);
CREATE INDEX IF NOT EXISTS idx_jobs_log_nombre ON jobs_log (nombre, inicio DESC);

CREATE TABLE IF NOT EXISTS jobs_peticiones (
    id                     TEXT NOT NULL,
    nombre                 TEXT NOT NULL,
    pedida                 TIMESTAMPTZ NOT NULL DEFAULT (now()),
    estado                 TEXT NOT NULL DEFAULT 'pendiente',
    resultado              TEXT,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS league_events (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                     TIMESTAMPTZ NOT NULL,
    kind                   TEXT NOT NULL,
    manager_id             INTEGER,
    counterparty           INTEGER,
    player_id              INTEGER,
    amount                 INTEGER,
    raw                    JSONTEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON league_events (ts DESC);
-- UNIQUE NULLS NOT DISTINCT: SQLite no lo tiene. Con un UNIQUE
-- normal dos NULL cuentan como distintos y la tabla se llena de
-- duplicados sin dar error, de ahí el coalesce de cada columna.
CREATE UNIQUE INDEX IF NOT EXISTS league_events_dedupe ON league_events (coalesce(ts, '∅'), coalesce(kind, '∅'), coalesce(manager_id, '∅'), coalesce(player_id, '∅'), coalesce(amount, '∅'));

CREATE TABLE IF NOT EXISTS managers (
    id                     INTEGER PRIMARY KEY,
    name                   TEXT,
    is_me                  BOOLEAN DEFAULT 0,
    balance_known          INTEGER,
    balance_est            INTEGER,
    team_value             INTEGER,
    points                 INTEGER,
    updated_at             TIMESTAMPTZ DEFAULT (now()),
    balance_committed      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS market_offers (
    market_id              TEXT NOT NULL,
    player_id              INTEGER NOT NULL,
    sale_price             INTEGER NOT NULL,
    expires_at             TIMESTAMPTZ,
    num_bids               INTEGER DEFAULT 0,
    seller                 TEXT,
    status                 TEXT,
    seen_at                TIMESTAMPTZ NOT NULL DEFAULT (now()),
    tipo                   TEXT,
    num_offers             INTEGER DEFAULT 0,
    player_team_id         TEXT,
    blindado               BOOLEAN,
    PRIMARY KEY (market_id),
    FOREIGN KEY (player_id) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_market_expira ON market_offers (expires_at);

CREATE TABLE IF NOT EXISTS market_values (
    player_id              INTEGER NOT NULL,
    ts                     TIMESTAMPTZ NOT NULL,
    value                  INTEGER NOT NULL,
    delta_24h              INTEGER,
    PRIMARY KEY (player_id, ts),
    FOREIGN KEY (player_id) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_mv_delta ON market_values (ts DESC, delta_24h DESC);
CREATE INDEX IF NOT EXISTS idx_mv_ts ON market_values (ts DESC);

CREATE TABLE IF NOT EXISTS market_values_historico (
    temporada              TEXT NOT NULL,
    fecha                  DATE NOT NULL,
    player_id              INTEGER NOT NULL,
    nickname               TEXT,
    position               INTEGER,
    value                  INTEGER,
    points                 INTEGER,
    PRIMARY KEY (temporada, fecha, player_id)
);
CREATE INDEX IF NOT EXISTS idx_mvh_jugador ON market_values_historico (temporada, player_id, fecha);

CREATE TABLE IF NOT EXISTS market_values_oficial (
    player_id              INTEGER NOT NULL,
    fecha                  DATE NOT NULL,
    valor                  INTEGER NOT NULL,
    pujas                  INTEGER,
    PRIMARY KEY (player_id, fecha)
);
CREATE INDEX IF NOT EXISTS idx_mvo_player ON market_values_oficial (player_id, fecha DESC);

CREATE TABLE IF NOT EXISTS player_points (
    player_id              INTEGER NOT NULL,
    jornada                INTEGER NOT NULL,
    points                 INTEGER,
    minutes                INTEGER,
    is_starter             BOOLEAN,
    stats                  JSONTEXT,
    PRIMARY KEY (player_id, jornada),
    FOREIGN KEY (player_id) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS players (
    id                     INTEGER PRIMARY KEY,
    name                   TEXT NOT NULL,
    nickname               TEXT,
    team_id                INTEGER,
    position               INTEGER,
    status                 TEXT,
    raw                    JSONTEXT,
    updated_at             TIMESTAMPTZ DEFAULT (now()),
    FOREIGN KEY (team_id) REFERENCES teams(id)
);

CREATE TABLE IF NOT EXISTS predictions (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                     TIMESTAMPTZ NOT NULL DEFAULT (now()),
    model                  TEXT NOT NULL,
    player_id              INTEGER,
    horizon                TEXT,
    value                  REAL,
    lo                     REAL,
    hi                     REAL,
    actual                 REAL,
    features               JSONTEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_model_ts ON predictions (model, ts DESC);

CREATE TABLE IF NOT EXISTS probable_lineups (
    jornada                INTEGER NOT NULL,
    team_id                INTEGER NOT NULL,
    player_id              INTEGER NOT NULL,
    probability            REAL,
    source                 TEXT NOT NULL,
    ts                     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (player_id, jornada, source)
);
CREATE INDEX IF NOT EXISTS idx_pl_jornada ON probable_lineups (jornada, player_id);
CREATE UNIQUE INDEX IF NOT EXISTS probable_lineups_nueva_pkey ON probable_lineups (player_id, jornada, source);

CREATE TABLE IF NOT EXISTS probable_lineups_historico (
    jornada                INTEGER NOT NULL,
    player_id              INTEGER NOT NULL,
    probability            REAL,
    source                 TEXT NOT NULL,
    ts                     TIMESTAMPTZ NOT NULL DEFAULT (now()),
    PRIMARY KEY (player_id, jornada, source, ts)
);
CREATE INDEX IF NOT EXISTS idx_plh_jornada ON probable_lineups_historico (jornada, ts);

CREATE TABLE IF NOT EXISTS seguimientos (
    id                     TEXT NOT NULL,
    tipo                   TEXT NOT NULL,
    params                 JSONTEXT NOT NULL DEFAULT '{}',
    estado                 TEXT NOT NULL DEFAULT 'activo',
    fase                   TEXT NOT NULL DEFAULT 'inicio',
    revisar_cada           INTEGER NOT NULL DEFAULT 300,
    proxima_revision       TIMESTAMPTZ NOT NULL DEFAULT (now()),
    caduca                 TIMESTAMPTZ,
    intentos               INTEGER NOT NULL DEFAULT 0,
    creada                 TIMESTAMPTZ NOT NULL DEFAULT (now()),
    actualizada            TIMESTAMPTZ NOT NULL DEFAULT (now()),
    resultado              TEXT,
    historial              JSONTEXT NOT NULL DEFAULT '[]',
    nota                   TEXT,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_seg_activos ON seguimientos (proxima_revision) WHERE (estado = 'activo');

CREATE TABLE IF NOT EXISTS sesiones (
    token_hash             TEXT NOT NULL,
    usuario_id             INTEGER NOT NULL,
    creada                 TIMESTAMPTZ NOT NULL DEFAULT (now()),
    expira                 TIMESTAMPTZ NOT NULL,
    agente                 TEXT,
    PRIMARY KEY (token_hash),
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);
CREATE INDEX IF NOT EXISTS idx_sesiones_usuario ON sesiones (usuario_id);

CREATE TABLE IF NOT EXISTS squad_snapshots (
    ts                     TIMESTAMPTZ NOT NULL,
    manager_id             INTEGER NOT NULL,
    player_id              INTEGER NOT NULL,
    buyout_clause          INTEGER,
    clause_lock_until      TIMESTAMPTZ,
    purchase_price         INTEGER,
    shielded               BOOLEAN DEFAULT 0,
    player_team_id         TEXT,
    PRIMARY KEY (ts, manager_id, player_id),
    FOREIGN KEY (manager_id) REFERENCES managers(id),
    FOREIGN KEY (player_id) REFERENCES players(id)
);
CREATE INDEX IF NOT EXISTS idx_squad_latest ON squad_snapshots (player_id, ts DESC);

CREATE TABLE IF NOT EXISTS tareas (
    id                     TEXT NOT NULL,
    accion                 TEXT NOT NULL,
    params                 JSONTEXT NOT NULL,
    cuando                 TIMESTAMPTZ NOT NULL,
    estado                 TEXT NOT NULL DEFAULT 'pendiente',
    creada                 TIMESTAMPTZ NOT NULL DEFAULT (now()),
    ejecutada_en           TIMESTAMPTZ,
    resultado              TEXT,
    nota                   TEXT,
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_tareas_pendientes ON tareas (cuando) WHERE (estado = 'pendiente');

CREATE TABLE IF NOT EXISTS teams (
    id                     INTEGER PRIMARY KEY,
    name                   TEXT NOT NULL,
    short_name             TEXT,
    slug                   TEXT
);

CREATE TABLE IF NOT EXISTS trading_ops (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id              INTEGER NOT NULL,
    nombre                 TEXT,
    abierta_en             TIMESTAMPTZ NOT NULL DEFAULT (now()),
    precio_compra          INTEGER NOT NULL,
    valor_compra           INTEGER NOT NULL,
    momentum               REAL,
    estado                 TEXT NOT NULL DEFAULT 'propuesta',
    listada_en             TIMESTAMPTZ,
    cerrada_en             TIMESTAMPTZ,
    precio_venta           INTEGER,
    nota                   TEXT
);
CREATE INDEX IF NOT EXISTS idx_trading_abiertas ON trading_ops (estado) WHERE (estado IN ('propuesta', 'comprada', 'listada'));

CREATE TABLE IF NOT EXISTS usuarios (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    email                  TEXT NOT NULL,
    nombre                 TEXT NOT NULL DEFAULT '',
    clave                  TEXT,
    google_sub             TEXT,
    creado                 TIMESTAMPTZ NOT NULL DEFAULT (now()),
    ultimo_acceso          TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS usuarios_email_key ON usuarios (email);
CREATE UNIQUE INDEX IF NOT EXISTS usuarios_google_sub_key ON usuarios (google_sub);

-- Las vistas van en db/002_vistas_sqlite.sql: usan DISTINCT ON y se
-- reescriben a mano con row_number().
