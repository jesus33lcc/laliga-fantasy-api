-- fantasy-coconut :: esquema base
-- Pensado para responder rápido a dos preguntas: qué va a subir mañana y a quién se
-- puede robar.

CREATE TABLE IF NOT EXISTS teams (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    short_name    TEXT,
    slug          TEXT
);

CREATE TABLE IF NOT EXISTS players (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    nickname          TEXT,
    team_id           INTEGER REFERENCES teams(id),
    position          SMALLINT,          -- 1 POR, 2 DEF, 3 MED, 4 DEL, 5 ENT
    status            TEXT,              -- ok / injured / doubtful / suspended / out_of_league
    raw               JSONB,             -- respuesta cruda: la red de seguridad ante un cambio de API
    updated_at        TIMESTAMPTZ DEFAULT now()
);

-- Serie temporal de precio. Es el corazón del sistema: no se puede reconstruir hacia
-- atrás, así que cada día sin tomarla se pierde para siempre.
CREATE TABLE IF NOT EXISTS market_values (
    player_id     INTEGER NOT NULL REFERENCES players(id),
    ts            TIMESTAMPTZ NOT NULL,
    value         BIGINT NOT NULL,
    delta_24h     BIGINT,                -- calculado en ingesta
    PRIMARY KEY (player_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_mv_ts ON market_values (ts DESC);
CREATE INDEX IF NOT EXISTS idx_mv_delta ON market_values (ts DESC, delta_24h DESC);

-- Puntuación por jornada, con el desglose de eventos si la API lo da.
CREATE TABLE IF NOT EXISTS player_points (
    player_id     INTEGER NOT NULL REFERENCES players(id),
    jornada       SMALLINT NOT NULL,
    points        INTEGER,
    minutes       SMALLINT,
    is_starter    BOOLEAN,
    stats         JSONB,
    PRIMARY KEY (player_id, jornada)
);

-- Los managers de la liga.
CREATE TABLE IF NOT EXISTS managers (
    id              INTEGER PRIMARY KEY,
    name            TEXT,
    is_me           BOOLEAN DEFAULT false,
    balance_known   BIGINT,              -- solo el del equipo propio es exacto
    balance_est     BIGINT,              -- estimado por contabilidad del tablón
    team_value      BIGINT,
    points          INTEGER,
    -- Lo retenido en pujas y ofertas vivas. `teamMoney` NO lo descuenta, así que el
    -- saldo disponible es `balance_known - balance_committed`.
    balance_committed BIGINT DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- Foto de las plantillas: quién tiene a quién, a qué precio lo compró y su cláusula.
CREATE TABLE IF NOT EXISTS squad_snapshots (
    ts              TIMESTAMPTZ NOT NULL,
    manager_id      INTEGER NOT NULL REFERENCES managers(id),
    player_id       INTEGER NOT NULL REFERENCES players(id),
    buyout_clause   BIGINT,
    clause_lock_until TIMESTAMPTZ,       -- protegido hasta: compra + 14 días exactos
    shielded          BOOLEAN DEFAULT false,  -- blindaje: tampoco clausulable
    player_team_id    TEXT,                   -- id de la ficha dentro del equipo; hace falta para alinear
    purchase_price  BIGINT,
    PRIMARY KEY (ts, manager_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_squad_latest ON squad_snapshots (player_id, ts DESC);

-- Tablón de la liga: es la única fuente pública sobre lo que hacen los rivales.
CREATE TABLE IF NOT EXISTS league_events (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL,
    kind          TEXT NOT NULL,         -- market_buy / market_sale / clausulazo / blindaje /
                                         -- recompensa / alta_manager / baja_manager / no_puntua_*
    manager_id    INTEGER,
    counterparty  INTEGER,
    player_id     INTEGER,
    amount        BIGINT,
    raw           JSONB,
    UNIQUE NULLS NOT DISTINCT (ts, kind, manager_id, player_id, amount)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON league_events (ts DESC);

-- Onces probables, scrapeados de una fuente externa.
--
-- La clave primaria NO lleva `ts`, y es deliberado: solo interesa la ÚLTIMA probabilidad
-- de cada (jugador, jornada, fuente), que es la que refleja la rueda de prensa más
-- reciente. La ingesta hace UPSERT.
--
-- Con `ts` en la clave, cada toma insertaría una fila nueva, el `ON CONFLICT` no
-- dispararía nunca y un `JOIN` sin `DISTINCT` cogería una probabilidad al azar entre
-- todas las copias. Para medir hacia atrás está la tabla de histórico de abajo.
CREATE TABLE IF NOT EXISTS probable_lineups (
    jornada       SMALLINT NOT NULL,
    team_id       INTEGER NOT NULL,
    player_id     INTEGER NOT NULL,
    probability   REAL,                  -- 0..1
    source        TEXT NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,  -- cuándo se vio por última vez
    PRIMARY KEY (player_id, jornada, source)
);
CREATE INDEX IF NOT EXISTS idx_pl_jornada ON probable_lineups (jornada, player_id);

-- Salida de modelos. Se guarda SIEMPRE la predicción, o no hay forma de backtestear.
CREATE TABLE IF NOT EXISTS predictions (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    model         TEXT NOT NULL,         -- 'momentum' en producción, 'momentum_backtest' al evaluar
    player_id     INTEGER,
    horizon       TEXT,                  -- 'precio_1d' es el único que se escribe hoy
    value         DOUBLE PRECISION,
    lo            DOUBLE PRECISION,
    hi            DOUBLE PRECISION,
    actual        DOUBLE PRECISION,      -- se rellena a posteriori
    features      JSONB
);
CREATE INDEX IF NOT EXISTS idx_pred_model_ts ON predictions (model, ts DESC);

-- Registro de decisiones: cada propuesta de Coconut, con su resultado.
CREATE TABLE IF NOT EXISTS decisions (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ DEFAULT now(),
    kind          TEXT,
    payload       JSONB,
    rationale     TEXT,
    executed      BOOLEAN DEFAULT false,
    outcome       TEXT,
    -- Las que escribe `politica.registrar()`, que es por donde pasa toda operación.
    -- Sin ellas una instalación nueva falla en el primer movimiento, no al arrancar.
    modo          TEXT,
    accion        TEXT,
    objetivo      TEXT,
    importe       BIGINT DEFAULT 0,
    motivo        TEXT,
    ejecutada     BOOLEAN DEFAULT false,
    resultado     TEXT
);

-- ---------------------------------------------------------------------------
-- Vistas
-- ---------------------------------------------------------------------------

-- Las plantillas del ÚLTIMO snapshot, no la última fila de cada jugador.
--
-- La diferencia decide si la vista dice la verdad. Con `DISTINCT ON (player_id)`, un
-- jugador vendido al mercado no tiene fila posterior, así que su última fila sigue siendo
-- la de su antiguo dueño y la vista lo da por suyo para siempre. En una liga con dos
-- meses de historia eso triplica la plantilla de un manager y llena
-- `v_clausula_targets` de jugadores que no son de nadie.
--
-- El corte es el `max(ts)` GLOBAL, que es la convención del resto del proyecto: la
-- ingesta escribe los cuatro managers con el mismo `ts`.
CREATE OR REPLACE VIEW v_latest_squads AS
SELECT s.player_id, s.manager_id, s.buyout_clause, s.purchase_price,
       s.clause_lock_until, s.ts
FROM squad_snapshots s
WHERE s.ts = (SELECT max(ts) FROM squad_snapshots);

CREATE OR REPLACE VIEW v_latest_values AS
SELECT DISTINCT ON (player_id) player_id, value, delta_24h, ts
FROM market_values
ORDER BY player_id, ts DESC;

-- Quién es clausulable y sale barato por punto esperado.
--
-- `protegido` vale true cuando el jugador está dentro de sus 14 días, o sea cuando NO se
-- le puede clausular. El nombre importa: con uno que sugiera lo contrario, filtrar por él
-- devuelve exactamente los intocables.
--
-- Sin el DROP, una base ya creada falla al aplicar este fichero: `CREATE OR REPLACE
-- VIEW` no puede renombrar una columna ("cannot change name of view column").
DROP VIEW IF EXISTS v_clausula_targets;
CREATE VIEW v_clausula_targets AS
SELECT p.id            AS player_id,
       p.name,
       m.name          AS owner,
       s.buyout_clause,
       v.value         AS market_value,
       round(s.buyout_clause::numeric / NULLIF(v.value, 0), 2) AS clause_ratio,
       s.clause_lock_until,
       (s.clause_lock_until IS NOT NULL AND s.clause_lock_until > now()) AS protegido
FROM v_latest_squads s
JOIN players  p ON p.id = s.player_id
JOIN managers m ON m.id = s.manager_id
JOIN v_latest_values v ON v.player_id = p.id
WHERE m.is_me = false
ORDER BY clause_ratio ASC;

-- Mercado diario: quién está a la venta, a qué precio y cuándo expira. Es la vía normal
-- para tapar un hueco de plantilla; un clausulazo es la excepción.
CREATE TABLE IF NOT EXISTS market_offers (
    market_id     TEXT PRIMARY KEY,
    player_id     INTEGER NOT NULL REFERENCES players(id),
    sale_price    BIGINT NOT NULL,
    expires_at    TIMESTAMPTZ,
    num_bids      INTEGER DEFAULT 0,
    -- El mercado son dos: `marketPlayerLeague` (agente libre, la subasta resuelve sola)
    -- y `marketPlayerTeam` (lo vende otro manager, decide él). La cuenta de rivales se
    -- llama distinto en cada uno, de ahí las dos columnas.
    num_offers    INTEGER DEFAULT 0,
    tipo          TEXT,
    player_team_id TEXT,
    blindado      BOOLEAN,
    seller        TEXT,
    status        TEXT,
    seen_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_market_expira ON market_offers (expires_at);

-- Histórico de onces probables
-- `probable_lineups` guarda solo el último valor, que es lo correcto para alinear pero
-- deja sin datos cualquier medición hacia atrás: al pisar cada toma con la siguiente,
-- queda una sola jornada evaluable.
--
-- Esta tabla conserva todas. Unas 500 filas por scrapeo y dos al día son ~365.000 al
-- año: nada para Postgres, y permite repetir la medición con cada jornada nueva.
CREATE TABLE IF NOT EXISTS probable_lineups_historico (
    jornada     smallint    NOT NULL,
    player_id   integer     NOT NULL,
    probability real,
    source      text        NOT NULL,
    ts          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, jornada, source, ts)
);
CREATE INDEX IF NOT EXISTS idx_plh_jornada ON probable_lineups_historico (jornada, ts);
