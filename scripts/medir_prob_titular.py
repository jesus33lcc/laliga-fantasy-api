"""Mide si `prob_titular` mejora la predicción de puntos, que es lo que decide `esperado`.

Para cada jornada se predice con lo conocido antes de que abra: la media de puntos de las
jornadas anteriores y el último scrapeo de onces etiquetado con esa jornada. Se compara
con los puntos que hizo. Cuatro predictores, con su correlación de Pearson:

  media             la media previa sola
  media x prob      lo que usa `alineacion.py` cuando hay dato de la fuente
  pisando el 0      un 0 de la fuente sustituido por la participación previa
  media x azar      control: un factor uniforme al azar en vez de la probabilidad

Solo lee la base. Uso:
  docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts:ro" worker \\
      python scripts/medir_prob_titular.py [--desde 4] [--hasta 11]
"""
from __future__ import annotations

import argparse
import asyncio
import random
import statistics

from sqlalchemy import text

from fantasy_coconut.db import Session

# El último scrapeo de cada jornada es el de antes de que abra: la ingesta etiqueta con
# la jornada siguiente en cuanto la anterior está en juego.
SQL_PROB = text("""
    SELECT h.player_id, h.jornada, h.probability
    FROM probable_lineups_historico h
    JOIN (SELECT player_id, jornada, max(ts) AS ts FROM probable_lineups_historico
          WHERE jornada BETWEEN :desde AND :hasta GROUP BY 1, 2) u
      ON u.player_id = h.player_id AND u.jornada = h.jornada AND u.ts = h.ts
""")
SQL_PUNTOS = text("SELECT id, raw->'weekPoints' FROM players WHERE raw->'weekPoints' IS NOT NULL")


def _pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3 or statistics.pstdev(xs) == 0 or statistics.pstdev(ys) == 0:
        return float("nan")
    return statistics.correlation(xs, ys)


async def medir(desde: int, hasta: int, semilla: int) -> None:
    async with Session() as s:
        probs = {(int(p), int(j)): v for p, j, v in
                 (await s.execute(SQL_PROB, {"desde": desde, "hasta": hasta})).all()}
        puntos: dict[int, dict[int, float]] = {}
        for pid, wp in (await s.execute(SQL_PUNTOS)).all():
            puntos[int(pid)] = {int(w["weekNumber"]): float(w.get("points") or 0)
                                for w in (wp or []) if w.get("weekNumber") is not None}

    azar = random.Random(semilla)
    filas = []
    for (pid, j), prob in probs.items():
        serie = puntos.get(pid) or {}
        # Sin registro de puntos en esa jornada no hay con qué comparar. Un aplazado sí
        # tiene registro, con 0, y entra como si no hubiera jugado.
        if prob is None or j not in serie:
            continue
        previas = [v for w, v in serie.items() if w < j]
        if not previas:
            continue
        media = sum(previas) / len(previas)
        # Aproxima el `jornadas / jornadas_liga` de `alineacion.py`, que la base no
        # guarda por jornada.
        participacion = sum(1 for v in previas if v != 0) / len(previas)
        filas.append((serie[j], media, media * prob,
                      media * (prob if prob > 0 else participacion),
                      media * azar.random(), j))

    if not filas:
        print("sin casos: no hay scrapeos y puntos de las mismas jornadas")
        return
    reales = [f[0] for f in filas]
    print(f"jornadas {desde}-{hasta} · {len(filas)} casos · "
          f"{len({f[5] for f in filas})} jornadas con datos · semilla {semilla}")
    for i, nombre in enumerate(("media", "media x prob", "pisando el 0",
                                "media x azar"), start=1):
        print(f"  {nombre:14} {_pearson([f[i] for f in filas], reales):+.4f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", type=int, default=4)
    ap.add_argument("--hasta", type=int, default=11)
    ap.add_argument("--semilla", type=int, default=0)
    a = ap.parse_args()
    asyncio.run(medir(a.desde, a.hasta, a.semilla))


if __name__ == "__main__":
    main()
