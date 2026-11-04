"""Ingesta del maestro público de jugadores.

El endpoint es público: no gasta token ni arriesga la cuenta, y trae el valor de mercado de
todos en una sola petición. De aquí sale la serie de precios.

El payload es una lista plana con los campos numéricos como texto y sin objeto `team`
anidado, solo `teamId`. El `playerStatus` de aquí no es el bueno: `league.py` lo pisa con
el de la plantilla, que es el que ve la app.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from ..clients.fantasy import FantasyAPI
from ..db import Session, json_param
from ..tiempo import hace

log = logging.getLogger("coconut.ingest.players")

# Los equipos se derivan de los jugadores para satisfacer la FK, con nombre de relleno.
# El `DO NOTHING` protege los 20 nombres reales, que ya están puestos (salen de
# `{API}/v3/teams-master`): con `DO UPDATE` los machacaría con `team-2`, `team-3`...
UPSERT_TEAM = text("""
    INSERT INTO teams (id, name)
    VALUES (:id, :name)
    ON CONFLICT (id) DO NOTHING
""")

# Quien aparece en el último snapshot de plantillas tiene dueño, y para ese la fuente
# buena es la plantilla, no el maestro.
_EN_PLANTILLA = ("EXISTS (SELECT 1 FROM squad_snapshots q WHERE q.player_id = players.id "
                 "AND q.ts = (SELECT max(ts) FROM squad_snapshots))")

UPSERT_PLAYER = text("""
    INSERT INTO players (id, name, nickname, team_id, position, status, raw, updated_at)
    VALUES (:id, :name, :nickname, :team_id, :position, :status, {RAW}, now())
    ON CONFLICT (id) DO UPDATE SET
        name       = EXCLUDED.name,
        nickname   = EXCLUDED.nickname,
        team_id    = EXCLUDED.team_id,
        -- `position` y `status` NO se pisan a quien está en una plantilla: ahí manda
        -- `ingest/league.py`, que los lee de `/leagues/{lg}/teams` con `ttl=0`. Este
        -- maestro se sirve con `Cache-Control: max-age=28800`, así que puede traerlos
        -- con hasta 8 h de retraso. Sin la condición, este UPSERT deshace la corrección
        -- de la plantilla y el modelo vuelve a alinear al jugador en su línea vieja:
        -- LaLiga lo descarta al guardar el once y quedan diez.
        position   = CASE WHEN {EN_PLANTILLA} THEN players.position
                          ELSE EXCLUDED.position END,
        status     = CASE WHEN {EN_PLANTILLA} THEN players.status
                          ELSE EXCLUDED.status END,
        raw        = EXCLUDED.raw,
        updated_at = now()
""".replace("{RAW}", json_param(":raw")).replace("{EN_PLANTILLA}", _EN_PLANTILLA))

# El delta se calcula contra la muestra más reciente de hace >=24 h. Así el valor
# queda persistido y las consultas no tienen que recalcularlo cada vez.
INSERT_VALUE = text("""
    INSERT INTO market_values (player_id, ts, value, delta_24h)
    SELECT :player_id, now(), :value,
           :value - (SELECT mv.value FROM market_values mv
                      WHERE mv.player_id = :player_id
                        AND mv.ts <= :hace_24h
                      ORDER BY mv.ts DESC LIMIT 1)
    ON CONFLICT (player_id, ts) DO NOTHING
""")


def _num(valor: Any) -> int | None:
    """La API manda los números como string; algunos vienen vacíos o nulos."""
    if valor in (None, "", "null"):
        return None
    try:
        return int(float(valor))
    except (TypeError, ValueError):
        return None


async def ingest_players(api: FantasyAPI) -> dict[str, int]:
    """Una petición pública -> equipos, jugadores y una muestra de precio de cada uno."""
    datos = await api.jugadores()
    if isinstance(datos, dict):  # por si algún día lo envuelven en {"data": [...]}
        datos = datos.get("data") or next(
            (v for v in datos.values() if isinstance(v, list)), []
        )
    if not isinstance(datos, list) or not datos:
        raise ValueError(f"payload de jugadores inesperado: {type(datos).__name__}")

    equipos = {p.get("teamId") for p in datos if p.get("teamId")}
    guardados = precios = 0

    async with Session() as s:
        for tid in sorted(equipos, key=lambda x: int(x) if str(x).isdigit() else 0):
            await s.execute(UPSERT_TEAM, {"id": _num(tid), "name": f"team-{tid}"})

        for p in datos:
            pid = _num(p.get("id"))
            if pid is None:
                continue
            nombre = p.get("nickname") or p.get("name") or f"player-{pid}"
            await s.execute(UPSERT_PLAYER, {
                "id": pid,
                "name": nombre,
                "nickname": p.get("nickname"),
                "team_id": _num(p.get("teamId")),
                "position": _num(p.get("positionId")),
                "status": p.get("playerStatus"),
                "raw": json.dumps(p, ensure_ascii=False),
            })
            guardados += 1

            valor = _num(p.get("marketValue"))
            if valor is not None:
                await s.execute(INSERT_VALUE,
                                {"player_id": pid, "value": valor,
                                 "hace_24h": hace(horas=24)})
                precios += 1

        await s.commit()

    log.info("jugadores=%d precios=%d equipos=%d", guardados, precios, len(equipos))
    return {"jugadores": guardados, "precios": precios, "equipos": len(equipos)}
