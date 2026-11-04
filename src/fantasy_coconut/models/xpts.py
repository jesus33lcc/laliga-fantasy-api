"""Puntos esperados para el resto de la temporada, de cualquier jugador con dueño.

Es el denominador de `models/clausulas.py`: `coste_por_punto = cláusula / xPts_restantes`.
Sin filas de este modelo en `predictions`, `coste_por_punto_m` sale NULL para todos los
objetivos y `/clausulas/objetivos` los devuelve sin ordenar, así que hay que rankearlos
a mano.

La magnitud es la de `alineacion.Jugador.esperado` por las jornadas que quedan, y se usa
el mismo dataclass a propósito: así el coste por punto de un objetivo rival es comparable
con la caída del once que provoca perder a uno propio. Un xPts calculado aparte, aunque
fuera mejor, haría que las dos cifras no se pudieran restar.

No son puntos reales de fantasy: son unidades relativas, igual que `esperado`. Sirven para
ordenar objetivos entre sí, no para prometer un marcador.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text

from ..db import Session, json_param
from .alineacion import Jugador

log = logging.getLogger("coconut.xpts")

JORNADAS_LIGA = 38
MODELO = "xpts_season_v1"

# Los mismos campos que `_MI_PLANTILLA` y sin el filtro `is_me`: los objetivos de
# `clausulas.py` son de los otros managers, así que hacen falta todas las plantillas.
_CON_DUENO = text("""
    SELECT p.id, p.name, p.position, p.status,
           CAST((p.raw->>'lastSeasonPoints') AS INTEGER) AS puntos,
           COALESCE((SELECT sum(pp.points) FROM player_points pp
                      WHERE pp.player_id = p.id AND pp.points IS NOT NULL),
                    CAST((p.raw->>'points') AS INTEGER), 0) AS puntos_temporada,
           COALESCE(NULLIF((SELECT count(*) FROM player_points pp
                             WHERE pp.player_id = p.id AND pp.points IS NOT NULL), 0),
                    jsonb_array_length(p.raw->'weekPoints'), 0) AS jornadas,
           (SELECT MAX(pl.probability) FROM probable_lineups pl
             WHERE pl.player_id = p.id
               AND pl.jornada = (SELECT MAX(jornada) FROM probable_lineups)) AS prob,
           v.value AS valor
    FROM squad_snapshots s
    JOIN players p ON p.id = s.player_id
    LEFT JOIN v_latest_values v ON v.player_id = p.id
    WHERE s.ts = (SELECT max(ts) FROM squad_snapshots)
""")

_GUARDAR = text("""
    INSERT INTO predictions (model, player_id, horizon, value, features)
    VALUES (:model, :player_id, 'xpts_resto', :value, {FEATURES})
""".replace("{FEATURES}", json_param(":features")))


async def calcular() -> list[dict]:
    """Un xPts restante por jugador con dueño. No escribe nada."""
    async with Session() as s:
        filas = (await s.execute(_CON_DUENO)).all()

    jugadores = [
        Jugador(player_id=r[0], nombre=r[1], posicion=r[2] or 0, estado=r[3],
                player_team_id=None, puntos_previos=r[4],
                puntos_temporada=int(r[5] or 0), jornadas=int(r[6] or 0),
                prob_titular=float(r[7]) if r[7] is not None else None,
                valor=int(r[8]) if r[8] else None)
        for r in filas
    ]
    # Mismo denominador que `mi_plantilla`: el máximo de partidos que ha jugado cualquiera.
    # El maestro público va una jornada por detrás, así que las restantes salen una de más:
    # eso infla el xPts y abarata el coste por punto, y los objetivos aparecen algo más
    # atractivos de lo que son.
    disputadas = max((j.jornadas for j in jugadores), default=0)
    restantes = max(JORNADAS_LIGA - disputadas, 1)
    for j in jugadores:
        j.jornadas_liga = disputadas

    return [{"player_id": j.player_id,
             "nombre": j.nombre,
             "value": round(j.esperado * restantes, 4),
             "features": {"esperado": round(j.esperado, 4),
                          "jornadas_restantes": restantes,
                          "prob_titular": j.prob_titular,
                          "puntos_referencia": round(j.puntos_referencia, 2),
                          "estado": j.estado}}
            for j in jugadores]


async def guardar() -> int:
    """Escribe una tanda en `predictions`. Devuelve cuántas filas."""
    filas = await calcular()
    if not filas:
        log.warning("sin jugadores con dueño: no se escribe nada")
        return 0
    async with Session() as s:
        for f in filas:
            await s.execute(_GUARDAR, {"model": MODELO,
                                       "player_id": f["player_id"],
                                       "value": f["value"],
                                       "features": json.dumps(f["features"])})
        await s.commit()
    log.info("xpts: %d jugadores, %d jornadas restantes",
             len(filas), filas[0]["features"]["jornadas_restantes"])
    return len(filas)
