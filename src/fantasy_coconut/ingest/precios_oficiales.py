"""Histórico oficial de precios: `GET {CMP}/player/{id}/market-value`.

Llega bastante más atrás que `market_values`, con un valor por día, que es lo que el modelo
necesita para entrenar. El campo `bids` no sirve: viene a 0 para todos los jugadores y
todas las fechas, el mismo caso que `bpc`/`bpp`. Se guarda por si algún día se rellena.

Es una petición por jugador, así que la liga entera serían cientos y el consumo diario se
iría por las nubes. Se va por lotes: mi plantilla primero, luego los rivales, luego el
resto. El pasado no cambia, así que dentro de cada grupo manda hace cuánto que no se mira.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import text

from ..clients.fantasy import FantasyAPI
from ..db import Session, ddl

log = logging.getLogger("coconut.ingest.precios_oficiales")

DDL = """
CREATE TABLE IF NOT EXISTS market_values_oficial (
    player_id  integer     NOT NULL,
    fecha      date        NOT NULL,
    valor      bigint      NOT NULL,
    pujas      integer,
    PRIMARY KEY (player_id, fecha)
);
CREATE INDEX IF NOT EXISTS idx_mvo_player ON market_values_oficial (player_id, fecha DESC)
"""

# Cuántos jugadores por pasada. 20 son 20 peticiones: se nota, pero cabe de sobra en el
# presupuesto diario y en cinco días cubre a los ~100 que de verdad importan.
POR_LOTE = 20

# Grupos: 0 mi plantilla, 2 la de los rivales, 3 el resto de jugadores en activo.
# Falta el grupo de los que hoy están en el mercado, que son los que se decide fichar:
# caen en el 3 junto a los ~700 restantes y pueden tardar semanas en tener histórico.
SQL_PRIORIDAD = """
WITH mios AS (
    SELECT DISTINCT s.player_id, CASE WHEN m.is_me THEN 0 ELSE 2 END AS prio
    FROM squad_snapshots s JOIN managers m ON m.id = s.manager_id
    WHERE s.ts = (SELECT max(ts) FROM squad_snapshots)
),
candidatos AS (
    SELECT player_id, prio FROM mios
    UNION
    SELECT id, 3 FROM players WHERE status = 'ok'
)
SELECT c.player_id,
       -- `DATE '2000-01-01'` (literal con tipo) es de Postgres. Sin el `DATE` vale en
       -- los dos: la columna ya es de tipo fecha, así que cada motor interpreta el
       -- literal como tal.
       COALESCE(MAX(o.fecha), '2000-01-01') AS ultima
FROM candidatos c
LEFT JOIN market_values_oficial o ON o.player_id = c.player_id
GROUP BY c.player_id, c.prio
ORDER BY c.prio, ultima ASC
LIMIT :n
"""


async def crear_tabla() -> None:
    async with Session() as s:
        for sentencia in ddl(DDL).split(";"):
            if sentencia.strip():
                await s.execute(text(sentencia))
        await s.commit()


async def ingerir_jugador(api: FantasyAPI, token: str, player_id: int) -> int:
    """Trae el histórico de uno y lo guarda. Devuelve cuántos días nuevos entraron."""
    datos: Any = await api.http.get(
        api._p(f"/player/{player_id}/market-value"), ttl=6 * 3600, auth=token)
    if not isinstance(datos, list):
        return 0

    filas = []
    for d in datos:
        crudo = (d.get("date") or "")[:10]
        if not crudo:
            continue
        try:
            # asyncpg exige un `date` de verdad: con la cadena falla ("'str' object
            # has no attribute 'toordinal'") aunque la consulta lleve CAST.
            fecha = dt.date.fromisoformat(crudo)
        except ValueError:
            continue
        filas.append({"p": player_id, "f": fecha,
                      "v": int(d.get("marketValue") or 0),
                      "b": int(d.get("bids") or 0)})
    if not filas:
        return 0

    async with Session() as s:
        # `DO NOTHING`: el pasado no cambia, y si cambiara conviene enterarse por otra
        # vía antes que sobrescribirlo en silencio.
        await s.execute(text("""
            INSERT INTO market_values_oficial (player_id, fecha, valor, pujas)
            VALUES (:p, :f, :v, :b)
            ON CONFLICT (player_id, fecha) DO NOTHING"""), filas)
        await s.commit()
    return len(filas)


async def ingerir_lote(api: FantasyAPI, token: str, n: int = POR_LOTE) -> dict[str, int]:
    """Un lote priorizado: primero los míos, luego rivales, luego el resto."""
    await crear_tabla()
    async with Session() as s:
        ids = [r[0] for r in (await s.execute(text(SQL_PRIORIDAD), {"n": n})).all()]

    dias = 0
    for pid in ids:
        try:
            dias += await ingerir_jugador(api, token, pid)
        except Exception:
            # Un jugador que falla no puede tumbar el lote: se salta y sigue.
            log.warning("no se pudo traer el histórico de %s", pid, exc_info=True)

    async with Session() as s:
        total = (await s.execute(text(
            "SELECT count(DISTINCT player_id) FROM market_values_oficial"))).scalar()
    log.info("precios oficiales: %d jugadores pedidos, %d días guardados, %d con histórico",
             len(ids), dias, total)
    return {"pedidos": len(ids), "dias": dias, "jugadores_con_historico": total or 0}
