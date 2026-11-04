"""Puntos por jornada de cada jugador en plantilla, leídos de su ficha individual.

El maestro público (`/players`) viene incompleto para algunos jugadores y no da ninguna
señal: las sumas cuadran, solo faltan jornadas. Se han visto las dos formas, fichas a las
que les falta una o dos y titulares que llegan con `points: 0` y `weekPoints: []`. La
ficha `/player/{id}` sí las trae, y su `playerStats` viene con todas de golpe.

El hueco se retroalimenta, que es lo que obliga a recorrer la plantilla entera: con
`jornadas = 0` la referencia de `alineacion.py` cae sobre `lastSeasonPoints` y el titular
se sienta, y `_cachear_puntos` solo pide los puntos de los once alineados, así que al que
se sienta no se le vuelven a pedir.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from ..clients.fantasy import FantasyAPI
from ..db import Session

log = logging.getLogger("coconut.ingest.puntos")

SQL_PLANTILLAS = """
SELECT DISTINCT s.player_id
FROM squad_snapshots s
JOIN managers m ON m.id = s.manager_id
WHERE s.ts = (SELECT max(ts) FROM squad_snapshots)
  AND (:solo_mios = 0 OR m.is_me)
ORDER BY s.player_id
"""


async def ingerir_jugador(api: FantasyAPI, token: str, player_id: int,
                          hasta_jornada: int) -> int:
    """Guarda las jornadas cerradas de un jugador. Devuelve cuántas escribió.

    `hasta_jornada` es la actual y el corte es estricto, así que la jornada en curso no
    entra: una foto a mitad de partido da parciales (un −1 que acabará en 7) y el parcial
    se queda guardado. De la jornada viva se encarga `_cachear_puntos`.
    """
    ficha = await api.http.get(api._p(f"/player/{player_id}"), ttl=3600, auth=token)
    if not isinstance(ficha, dict):
        return 0

    filas = []
    for e in ficha.get("playerStats") or []:
        if not isinstance(e, dict):
            continue
        try:
            n = int(e.get("weekNumber"))
        except (TypeError, ValueError):
            continue
        pts = e.get("totalPoints")
        # `is None` y no `or 0`: una jornada de 0 puntos es un dato, y tratarla como
        # ausente la dejaría fuera y contaría una jornada menos en la media.
        if pts is None or n >= hasta_jornada:
            continue
        filas.append({"p": player_id, "n": n, "v": int(pts)})
    if not filas:
        return 0

    async with Session() as s:
        # `DO UPDATE` y no `DO NOTHING`: si una fila se guardó a medias por otra vía, esta
        # fuente es la buena y debe corregirla. Una jornada cerrada ya no cambia, así que
        # sobrescribir con el valor definitivo no puede empeorar nada.
        await s.execute(text(
            "INSERT INTO player_points (player_id, jornada, points) "
            "VALUES (:p, :n, :v) ON CONFLICT (player_id, jornada) "
            "DO UPDATE SET points = EXCLUDED.points"), filas)
        await s.commit()
    return len(filas)


async def ingest_puntos(solo_mios: bool = False) -> dict[str, int]:
    """Rellena `player_points` con la ficha individual de cada jugador en plantilla.

    Por defecto recorre las cuatro plantillas, decenas de peticiones frente al techo de
    500 al día: los puntos de un rival deciden si su cláusula sale a cuenta. Con
    `solo_mios` se queda en la propia, que es el mínimo que arregla el once.
    """
    from ..clients.via_api import cliente_lectura

    async with cliente_lectura() as http:
        api = FantasyAPI(http)
        from ..clients.auth import tokens
        token = await tokens.bearer()

        semana = await api.jornada_actual()
        actual = int(semana.get("weekNumber") or 1)
        # Si la jornada está en juego, la actual es ella; si ya cerró, `weekNumber` apunta
        # a la siguiente y la última cerrada es la anterior. El corte es estricto en los
        # dos casos, así que basta con no restar nada: `n >= actual` se descarta.

        async with Session() as s:
            ids = [r[0] for r in (await s.execute(
                text(SQL_PLANTILLAS), {"solo_mios": 1 if solo_mios else 0})).all()]

        escritas, fallos = 0, 0
        for pid in ids:
            try:
                escritas += await ingerir_jugador(api, token, pid, actual)
            except Exception:
                # Un jugador que falla no tumba la pasada: se anota y se sigue. Quedarse
                # sin los otros 55 por uno sería peor que el hueco que se intenta tapar.
                fallos += 1
                log.warning("no se pudieron leer los puntos de %s", pid, exc_info=True)

    async with Session() as s:
        total = (await s.execute(text(
            "SELECT count(*) FROM player_points WHERE points IS NOT NULL"))).scalar()

    res = {"jugadores": len(ids), "jornadas_escritas": escritas,
           "fallos": fallos, "filas_totales": int(total or 0)}
    log.info("puntos por jornada: %s", res)
    return res
