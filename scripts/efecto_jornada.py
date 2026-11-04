"""¿Puntuar hace subir el precio? Y si sí, ¿cuánto y cuándo?

Se mide sobre `market_values_historico`. Importa porque decide cuándo mirar el
mercado: si puntuar predice la subida, el momento de comprar es justo después de una
jornada buena, antes de que el recálculo lo refleje.

Lo que no se puede medir con estos datos: el efecto del recálculo de las 00:15.
El archivo son snapshots diarios (un valor por jugador y día), así que no hay intradía.
Para eso hace falta la serie propia, que toma 4 muestras al día: ver
`scripts/efecto_recalculo.py`.

    docker compose run --rm --no-deps worker python scripts/efecto_jornada.py
"""

from __future__ import annotations

import asyncio
import os
import statistics as st
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fantasy_coconut.db import Session

TEMPORADA = "2024-25"
SQL = text("SELECT fecha, player_id, value, points FROM market_values_historico "
           "WHERE temporada = :t AND value > 0 ORDER BY fecha")


def corr(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = st.mean(xs), st.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys, strict=True))
    dx = sum((a - mx) ** 2 for a in xs) ** 0.5
    dy = sum((b - my) ** 2 for b in ys) ** 0.5
    return num / (dx * dy) if dx and dy else None


async def main() -> int:
    async with Session() as s:
        filas = [tuple(r) for r in (await s.execute(SQL, {"t": TEMPORADA})).all()]
    por_dia: dict = {}
    for fecha, pid, val, pts in filas:
        por_dia.setdefault(fecha, {})[pid] = (val, pts or 0)
    dias = sorted(por_dia)
    print(f"{len(filas):,} observaciones · {len(dias)} días\n")

    # `points` es ACUMULADO de la temporada, así que la diferencia entre dos días es lo
    # que puntuó en medio. Un día sin jornada da 0 para todos.
    ganados_x, subida_y = [], []
    por_tramo: dict = {}
    dias_con_jornada = 0
    for i in range(1, len(dias) - 1):
        if (dias[i] - dias[i - 1]).days != 1 or (dias[i + 1] - dias[i]).days != 1:
            continue
        ayer, hoy, man = por_dia[dias[i - 1]], por_dia[dias[i]], por_dia[dias[i + 1]]
        com = [p for p in hoy if p in ayer and p in man and ayer[p][0] > 0 and hoy[p][0] > 0]
        if len(com) < 100:
            continue
        ganados = {p: hoy[p][1] - ayer[p][1] for p in com}
        if sum(1 for g in ganados.values() if g != 0) < 20:
            continue                      # día sin jornada: no aporta nada
        dias_con_jornada += 1
        for p in com:
            g = ganados[p]
            sub = (man[p][0] - hoy[p][0]) / hoy[p][0] * 100
            ganados_x.append(g)
            subida_y.append(sub)
            # Tramos de rendimiento, que es como se decide de verdad.
            if g <= 0:
                clave = "0 o negativo"
            elif g <= 3:
                clave = "1-3 pts"
            elif g <= 6:
                clave = "4-6 pts"
            elif g <= 10:
                clave = "7-10 pts"
            else:
                clave = ">10 pts"
            por_tramo.setdefault(clave, []).append(sub)

    if not ganados_x:
        print("no hay días de jornada evaluables")
        return 1

    print(f"días CON jornada evaluados: {dias_con_jornada} · "
          f"observaciones: {len(ganados_x):,}\n")
    c = corr(ganados_x, subida_y)
    print(f"  correlación puntos ganados subida del día siguiente: {c:+.4f}\n")

    print(f"  {'puntos de la jornada':<20} {'subida al día siguiente':>24} {'n':>8}")
    orden = ["0 o negativo", "1-3 pts", "4-6 pts", "7-10 pts", ">10 pts"]
    for k in orden:
        v = por_tramo.get(k)
        if v:
            print(f"  {k:<20} {st.mean(v):>+22.3f} % {len(v):>8,}")

    base = st.mean(por_tramo.get("0 o negativo", [0]))
    alto = st.mean(por_tramo.get(">10 pts", [0]))
    print(f"\n diferencia entre puntuar >10 y no puntuar: {alto - base:+.3f} pp")
    if alto - base > 1:
        print("     PUNTUAR MUEVE EL PRECIO. El momento de comprar es justo después de")
        print("     una jornada buena, antes de que el recálculo lo refleje del todo.")
    elif alto - base > 0.2:
        print("     Hay efecto, pero pequeño: no compensa por sí solo.")
    else:
        print("     NO hay efecto apreciable: el precio no sigue a los puntos del día.")

    # ¿Se agota enseguida o dura? Lo que decide si sirve para operar o llega tarde.
    print("\n  -- ¿Cuánto dura el efecto de una jornada buena (>6 pts)? --")
    for h in (1, 2, 3, 5):
        acum = []
        for i in range(1, len(dias) - h):
            if any((dias[i + k + 1] - dias[i + k]).days != 1 for k in range(h)):
                continue
            if (dias[i] - dias[i - 1]).days != 1:
                continue
            ayer, hoy, fin = por_dia[dias[i - 1]], por_dia[dias[i]], por_dia[dias[i + h]]
            com = [p for p in hoy if p in ayer and p in fin and hoy[p][0] > 0]
            buenos = [p for p in com if hoy[p][1] - ayer[p][1] > 6]
            if len(buenos) < 5 or len(com) < 100:
                continue
            r_b = st.mean((fin[p][0] - hoy[p][0]) / hoy[p][0] for p in buenos) * 100
            r_m = st.mean((fin[p][0] - hoy[p][0]) / hoy[p][0] for p in com) * 100
            acum.append(r_b - r_m)
        if acum:
            print(f"    +{h} día(s): {st.mean(acum):+6.2f} pp sobre la media  (n={len(acum)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
