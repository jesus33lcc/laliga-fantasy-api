"""¿El momentum de precio es una ley, o solo un régimen pasajero?

`models/precio.py` mide que comprar lo que más subió ayer rinde más que el jugador medio,
pero lo hace sobre pocos días de evaluación, y eso puede ser un régimen y no una ley. Con
una temporada entera en `market_values_historico` se contrasta contra otros jugadores,
otro calendario y un año de datos.

Se mide lo mismo que `precio.walk_forward()`, sin mirar el futuro en ningún punto:

    para cada día D:
        top-50 por subida de D−1 ¿cuánto rinden en D?
        frente a: el jugador medio de ese mismo día

Y de paso comprueba la hipótesis de los parones de selecciones, donde se supone que
hay ventas de pánico con descuento predecible.

    docker compose run --rm --no-deps worker python scripts/validar_momentum_24_25.py
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
TOP = 50

SQL = text("""
SELECT fecha, player_id, value
FROM market_values_historico
WHERE temporada = :t AND value > 0
ORDER BY fecha, player_id
""")


async def main() -> int:
    async with Session() as s:
        filas = [tuple(r) for r in (await s.execute(SQL, {"t": TEMPORADA})).all()]
    if len(filas) < 10000:
        print(f"solo {len(filas)} filas: el backfill no ha terminado. Espera y repite.")
        return 1

    # {fecha: {player_id: valor}}
    por_dia: dict = {}
    for fecha, pid, val in filas:
        por_dia.setdefault(fecha, {})[pid] = val
    dias = sorted(por_dia)
    print(f"{len(filas):,} observaciones · {len(dias)} días · {dias[0]} → {dias[-1]}")

    ventaja, r_top, r_medio, saltos = [], [], [], 0
    for i in range(2, len(dias)):
        ayer2, ayer, hoy = dias[i - 2], dias[i - 1], dias[i]
        # Si faltan días en el archivo, el "retorno de ayer" abarcaría más de 24 h y no
        # sería comparable. Se exige que los tres días sean consecutivos.
        if (hoy - ayer).days != 1 or (ayer - ayer2).days != 1:
            saltos += 1
            continue
        a2, a1, h = por_dia[ayer2], por_dia[ayer], por_dia[hoy]
        comunes = [p for p in a1 if p in a2 and p in h and a2[p] > 0 and a1[p] > 0]
        if len(comunes) < 100:
            continue
        subida_ayer = {p: (a1[p] - a2[p]) / a2[p] for p in comunes}
        hoy_pct = {p: (h[p] - a1[p]) / a1[p] for p in comunes}
        top = sorted(comunes, key=lambda p: -subida_ayer[p])[:TOP]
        rt = st.mean(hoy_pct[p] for p in top) * 100
        rm = st.mean(hoy_pct[p] for p in comunes) * 100
        r_top.append(rt)
        r_medio.append(rm)
        ventaja.append(rt - rm)

    if not ventaja:
        print("no hay suficientes días consecutivos para evaluar")
        return 1

    print(f"días evaluados: {len(ventaja)} (saltados por hueco en el archivo: {saltos})\n")
    print(f"  cartera del top-{TOP} por subida de ayer : {st.mean(r_top):+.3f} %/día")
    print(f"  el jugador medio                        : {st.mean(r_medio):+.3f} %/día")
    print(f"  VENTAJA                                 : {st.mean(ventaja):+.3f} %/día")
    print(f"  mediana de la ventaja                   : {st.median(ventaja):+.3f} %")
    gana = sum(1 for v in ventaja if v > 0)
    print(f"  días en que la cartera bate a la media  : {gana}/{len(ventaja)} "
          f"({gana / len(ventaja) * 100:.0f} %)")

    print("\n  Comparación con lo medido en 2026 (8 días, arranque de liga):")
    print("    2026: cartera +3,2 a +6,6 %/día · medio −0,16 a −0,48 % · ventaja ~+5,5 %")
    v = st.mean(ventaja)
    if v > 3:
        print(f"\n  VEREDICTO: el momentum PERSISTE en otra temporada ({v:+.2f} %/día).")
        print("             No era el régimen de agosto: es una propiedad del mercado.")
    elif v > 0.5:
        print(f"\n  VEREDICTO: existe pero MÁS DÉBIL que en 2026 ({v:+.2f} %/día).")
        print("             El +5,5 % de agosto estaba inflado por el arranque de liga.")
    else:
        print(f"\n  VEREDICTO: NO se reproduce ({v:+.2f} %/día). Lo de 2026 era un RÉGIMEN,")
        print("             y la operativa de momentum no debería darse por validada.")

    # ---------- parones de selecciones ----------
    # Contrasta la hipótesis de "ventas de pánico = descuento predecible". Son las fechas
    # FIFA de la 24/25: al cambiar de temporada hay que actualizarlas.
    parones = [("2024-10-07", "2024-10-15"), ("2024-11-11", "2024-11-19"),
               ("2025-03-17", "2025-03-25")]
    print("\n  -- Parones de selecciones --")
    dentro, fuera = [], []
    for i in range(1, len(dias)):
        ayer, hoy = dias[i - 1], dias[i]
        if (hoy - ayer).days != 1:
            continue
        a, h = por_dia[ayer], por_dia[hoy]
        comunes = [p for p in a if p in h and a[p] > 0]
        if len(comunes) < 100:
            continue
        medio = st.mean((h[p] - a[p]) / a[p] for p in comunes) * 100
        iso = hoy.isoformat()
        (dentro if any(d <= iso <= f for d, f in parones) else fuera).append(medio)
    if dentro and fuera:
        print(f"    durante el parón : {st.mean(dentro):+.3f} %/día  (n={len(dentro)})")
        print(f"    resto del año    : {st.mean(fuera):+.3f} %/día  (n={len(fuera)})")
        dif = st.mean(dentro) - st.mean(fuera)
        print(f"    diferencia       : {dif:+.3f} pp  →  "
              + ("hay caída extra: el descuento del parón es REAL" if dif < -0.15
                 else "sin caída extra: la hipótesis del parón NO se sostiene"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
