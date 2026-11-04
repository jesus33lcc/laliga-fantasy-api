"""¿El valor de mercado cambia solo en el recálculo de las 00:15?

Se responde con la serie propia, que toma 4 muestras al día: un archivo de snapshots
diarios (un valor por jugador y día) no puede contestarlo porque no tiene intradía. Saca
dos cosas: cuántos valores distintos tiene un jugador en un mismo día, y entre qué tomas
cambia.

Importa porque decide cuándo mirar el mercado: si el precio se mueve más de una vez al
día, la foto con la que se decide caduca antes de lo que parece.

Dos precauciones sin las cuales el resultado es falso:

- La BD guarda en UTC y el juego va en hora de Madrid. El desplazamiento entre las
  dos cambia con el horario de verano, así que no se puede restar un número fijo de
  horas: la consulta convierte con `AT TIME ZONE 'Europe/Madrid'` antes de agrupar por
  día y hora local. Comparar sin convertir da un "no pasa nada a las 00:15" que no es
  cierto.
- La métrica se calcula en SQL con `lag()` dentro de cada (jugador, día), no
  acumulando en Python, que suma mal entre días con distinto número de tomas. Y los
  tramos `8h → 8h` y `14h → 14h` son el control: dos tomas de la misma hora tienen
  que dar 0 % de cambios, y si no lo dan es la consulta la que está mal.

    docker compose run --rm --no-deps worker python scripts/efecto_recalculo.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fantasy_coconut.db import Session

SQL = text("""
WITH t AS (
  SELECT (ts AT TIME ZONE 'Europe/Madrid') AS loc, player_id, value
  FROM market_values WHERE value > 0 AND ts > now() - make_interval(days => CAST(:dias AS int))
), s AS (
  SELECT date(loc) AS dia, CAST(extract(hour FROM loc) AS INTEGER) AS h, player_id, value,
         lag(value) OVER (PARTITION BY player_id, date(loc) ORDER BY loc) AS ant,
         lag(CAST(extract(hour FROM loc) AS INTEGER)) OVER (PARTITION BY player_id, date(loc) ORDER BY loc) AS h_ant
  FROM t
)
SELECT h_ant, h, count(*) AS comparaciones,
       count(*) FILTER (WHERE value <> ant) AS cambios,
       avg(CAST(abs(value - ant) AS FLOAT) / ant * 100) FILTER (WHERE value <> ant) AS medio
FROM s WHERE ant IS NOT NULL
GROUP BY 1, 2 HAVING count(*) > 500 ORDER BY h_ant, h
""")

SQL_DISTINTOS = text("""
WITH t AS (
  SELECT date((ts AT TIME ZONE 'Europe/Madrid')) AS dia, player_id, value
  FROM market_values WHERE value > 0 AND ts > now() - make_interval(days => CAST(:dias AS int))
), d AS (
  SELECT dia, player_id, count(DISTINCT value) AS valores
  FROM t GROUP BY 1, 2 HAVING count(*) >= 3
)
SELECT valores, count(*) FROM d GROUP BY 1 ORDER BY 1
""")


async def main() -> int:
    dias = 15
    async with Session() as s:
        tramos = [tuple(r) for r in (await s.execute(SQL, {"dias": dias})).all()]
        distintos = [tuple(r) for r in (await s.execute(SQL_DISTINTOS, {"dias": dias})).all()]

    total = sum(c for _v, c in distintos) or 1
    print(f"últimos {dias} días\n")
    print("  -- ¿cuántos valores DISTINTOS tiene un jugador en un mismo día? --")
    for v, c in distintos:
        print(f"    {v} valor(es): {c:>6,}  ({c / total * 100:>4.1f} %)")
    mas_de_uno = sum(c for v, c in distintos if v > 1) / total * 100
    print(f"\n el {mas_de_uno:.0f} % cambia MÁS DE UNA VEZ al día: el recálculo de las")
    print("     00:15 NO es el único momento en que se mueve el precio.")

    print("\n  -- ¿entre qué tomas cambia? --")
    print(f"  {'tramo':<12} {'comparaciones':>14} {'% cambia':>10} {'cambio medio':>14}")
    for h_ant, h, comp, cam, medio in tramos:
        control = " control: debe ser 0 %" if h_ant == h else ""
        print(f"  {f'{h_ant}h → {h}h':<12} {comp:>14,} {cam / comp * 100:>9.1f}% "
              f"{(float(medio) if medio else 0):>13.3f}%{control}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
