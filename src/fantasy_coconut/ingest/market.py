"""Consultas sobre la serie de precios.

La ingesta vive en `players.py`: el maestro público trae el valor de mercado de todos los
jugadores en una sola petición. Aquí solo hay lecturas.
"""

from __future__ import annotations

from sqlalchemy import text

from ..db import Session


async def biggest_movers(limit: int = 20):
    """Top subidas y bajadas de valor en las últimas 24 h."""
    async with Session() as s:
        rows = await s.execute(text("""
            SELECT p.name, v.value, v.delta_24h,
                   round(100.0 * v.delta_24h / NULLIF(v.value - v.delta_24h, 0), 2) AS pct
            FROM v_latest_values v JOIN players p ON p.id = v.player_id
            WHERE v.delta_24h IS NOT NULL
            ORDER BY abs(v.delta_24h) DESC LIMIT :lim
        """), {"lim": limit})
        return [dict(r._mapping) for r in rows]
