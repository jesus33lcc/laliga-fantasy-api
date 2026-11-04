"""¿Penalizar `lastSeasonPoints` a los jugadores TRASPASADOS mejora la predicción?

Los puntos de la temporada pasada son del equipo anterior: un jugador que cambia de club
y pierde titularidad queda sobreestimado por la referencia previa. Parece obvio, y por eso
hay que medirlo antes de tocar el modelo.

Cómo se sabe quién se traspasó

El archivo público de la temporada anterior trae el `team` de cada jugador, así que basta
un día (el último de esa temporada) para saber el equipo de cada uno. Los `id` del
maestro son estables entre temporadas, pero no del todo: se cruza además por NOMBRE y
se descarta a quien no coincida, porque un id reutilizado contaría como traspaso y sería
ruido puro.

La medición

Para cada jugador y jornada *j*, predecir con lo conocido hasta *j−1*, con control:

    base              = la mezcla actual (previa + ritmo, K=4)
    base penalizada   = igual, pero multiplicando la PREVIA por `factor` si se traspasó
    control           = igual, penalizando a jugadores AL AZAR en la misma proporción

El control importa: si penalizar al azar mejora lo mismo, lo que ayuda no es detectar
traspasos: es rebajar la referencia previa en general.
"""

from __future__ import annotations

import asyncio
import os
import random
import statistics as st
import sys

import httpx
from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fantasy_coconut.db import Session

URL_ULTIMO = ("https://raw.githubusercontent.com/Mixtomeister/"
              "laliga-fantasy-api-archive/main/archive/202508/28.json")
K = 4.0

SQL = text("""
WITH pts AS (
    SELECT p.id AS pid, CAST((w->>'weekNumber') AS INTEGER) AS jornada, CAST((w->>'points') AS INTEGER) AS puntos
    FROM players p, jsonb_array_elements(p.raw->'weekPoints') AS w
    WHERE jsonb_typeof(p.raw->'weekPoints') = 'array'
)
SELECT pid, jornada, puntos FROM pts WHERE jornada <= 4
""")
SQL_PREVIA = text("""
SELECT id, coalesce(CAST((raw->>'lastSeasonPoints') AS INTEGER), 0), team_id, nickname
FROM players
""")


def corr(xs, ys):
    mx, my = st.mean(xs), st.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys, strict=True))
    dx = sum((a - mx) ** 2 for a in xs) ** 0.5
    dy = sum((b - my) ** 2 for b in ys) ** 0.5
    return num / (dx * dy) if dx and dy else None


def esperado(previa: float, pts_hasta: int, jornadas: int, factor: float) -> float:
    """La misma mezcla que `models/alineacion.py`, con la previa escalada por `factor`."""
    previa = previa * factor
    if not jornadas:
        return previa
    actual = (pts_hasta / jornadas) * 38.0
    peso = jornadas / (jornadas + K)
    return peso * actual + (1 - peso) * previa


async def main() -> int:
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        viejo_raw = (await c.get(URL_ULTIMO)).json()
    viejo = {int(p["id"]): ((p.get("team") or {}).get("id"), p.get("nickname"))
             for p in viejo_raw if p.get("id")}

    async with Session() as s:
        filas = [tuple(r) for r in (await s.execute(SQL)).all()]
        info = {int(r[0]): (float(r[1]), r[2], r[3])
                for r in (await s.execute(SQL_PREVIA)).all()}

    traspasados = set()
    for pid, (_prev, team_act, nick) in info.items():
        v = viejo.get(pid)
        # Solo se fía si el NOMBRE coincide: sin eso, un id reutilizado se contaría como
        # traspaso y sería ruido puro.
        if not v or not v[1] or not nick:
            continue
        if v[1].lower() != nick.lower():
            continue
        if v[0] and team_act and int(v[0]) != int(team_act):
            traspasados.add(pid)
    print(f"traspasados detectados: {len(traspasados)}")

    por_jug: dict = {}
    for pid, j, p in filas:
        por_jug.setdefault(pid, {})[j] = p
    jornadas = sorted({j for _p, j, _ in filas})

    random.seed(7)
    azar = set(random.sample(sorted(por_jug), min(len(traspasados), len(por_jug))))

    for factor in (1.0, 0.7, 0.5, 0.3):
        base_x, azar_x, reales = [], [], []
        for j in jornadas[2:]:
            for pid, pts in por_jug.items():
                if j not in pts:
                    continue
                previos = [pts[k] for k in pts if k < j]
                if not previos or pid not in info:
                    continue
                prev = info[pid][0]
                f_tra = factor if pid in traspasados else 1.0
                f_azar = factor if pid in azar else 1.0
                base_x.append(esperado(prev, sum(previos), len(previos), f_tra))
                azar_x.append(esperado(prev, sum(previos), len(previos), f_azar))
                reales.append(pts[j])
        c_tra, c_azar = corr(base_x, reales), corr(azar_x, reales)
        etiqueta = "(sin penalizar = el modelo actual)" if factor == 1.0 else ""
        print(f"  factor {factor:.1f} → traspasados {c_tra:.4f} · al azar {c_azar:.4f}  {etiqueta}")

    print(f"\n  casos evaluados: {len(reales)}")
    print("  Si penalizar a los traspasados no bate a penalizar al azar, lo que mejora")
    print("  (si mejora) es rebajar la previa en general, no detectar el traspaso.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
