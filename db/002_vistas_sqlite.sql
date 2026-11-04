-- Vistas de Coconut para SQLite: ESTAS SÍ se escriben a mano.
--
-- El generador de esquema traduce tablas e índices solo. Estas tres no, porque usan
-- `DISTINCT ON (col)`, una extensión de Postgres que **no tiene equivalente directo**:
-- significa "de cada grupo, quédate la PRIMERA fila según el ORDER BY".
--
-- El sustituto estándar es `row_number() OVER (PARTITION BY … ORDER BY …) = 1`, que
-- SQLite soporta desde 3.25. La traducción es mecánica pero decide **qué fila gana**,
-- y equivocarse ahí no da error: da el precio de ayer en vez del de hoy.

-- El dueño ACTUAL de cada jugador: la fila más reciente de squad_snapshots.
DROP VIEW IF EXISTS v_latest_squads;
-- Ver la nota de `db/001_schema.sql`: el corte es el `max(ts)` global, no la última fila
-- de cada jugador. Con lo segundo, un vendido al mercado sigue saliendo como de su
-- antiguo dueño porque no tiene fila posterior.
CREATE VIEW v_latest_squads AS
SELECT s.player_id, s.manager_id, s.buyout_clause, s.purchase_price,
       s.clause_lock_until, s.ts
FROM squad_snapshots s
WHERE s.ts = (SELECT max(ts) FROM squad_snapshots);

-- El precio ACTUAL de cada jugador.
DROP VIEW IF EXISTS v_latest_values;
CREATE VIEW v_latest_values AS
SELECT player_id, value, delta_24h, ts
FROM (SELECT player_id, value, delta_24h, ts,
             row_number() OVER (PARTITION BY player_id ORDER BY ts DESC) AS rn
      FROM market_values)
WHERE rn = 1;

-- Quién es clausulable y sale barato por punto esperado.
--
-- Dos diferencias con la versión de Postgres, las dos deliberadas:
--   · `round(x::numeric, 2)` → `round(CAST(x AS REAL), 2)`. El CAST **no sobra**: sin
--     él, `buyout_clause / value` es una división entera y el ratio sale 1 o 0 para
--     todo el mundo, que es un resultado plausible y completamente falso.
--   · `m.is_me = false` → `coalesce(m.is_me, 0) = 0`. En SQLite el booleano es 0/1 y
--     `is_me` admite NULL; sin el coalesce, un manager con is_me NULL desaparecería
--     de la lista de objetivos sin que nadie lo note.
DROP VIEW IF EXISTS v_clausula_targets;
CREATE VIEW v_clausula_targets AS
SELECT p.id AS player_id,
       p.name,
       m.name AS owner,
       s.buyout_clause,
       v.value AS market_value,
       round(CAST(s.buyout_clause AS REAL) / nullif(v.value, 0), 2) AS clause_ratio,
       s.clause_lock_until,
       -- Vale true cuando el jugador está dentro de sus 14 días, o sea cuando NO se le
       -- puede clausular. El nombre tiene que decir eso o se filtra al revés.
       (s.clause_lock_until IS NOT NULL AND s.clause_lock_until > now()) AS protegido
FROM v_latest_squads s
JOIN players  p ON p.id = s.player_id
JOIN managers m ON m.id = s.manager_id
JOIN v_latest_values v ON v.player_id = p.id
WHERE coalesce(m.is_me, 0) = 0
ORDER BY clause_ratio ASC;

-- Los locks entre procesos. En Postgres son advisory locks (no hay tabla); en SQLite
-- hacen falta como tabla, porque no existe ese mecanismo. `locks.py` la crea sola al
-- arrancar, pero se declara aquí también para que una base recién creada la traiga y
-- no parezca que falta algo. Ver `locks.py` para por qué los locks CADUCAN.
CREATE TABLE IF NOT EXISTS locks (
    recurso TEXT PRIMARY KEY,
    tomado  TIMESTAMPTZ NOT NULL,
    expira  TIMESTAMPTZ NOT NULL,
    quien   TEXT
);
