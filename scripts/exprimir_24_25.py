"""Calibra el momentum de precio contra una temporada entera de `market_values_historico`.

Da tres respuestas que, si no, hay que elegir a ojo al configurar `models/trading.py`:

1. ¿Cuántos días persiste la racha? Es lo que fija la ventana de salida.
2. ¿Cambia por tramo de precio? Si el efecto vive solo en los baratos, el tope de
   capital de trading debería mirar el precio, no solo el momentum.
3. ¿Y por posición?

Los tres bloques exigen días consecutivos: con un hueco en el archivo, tres días de
calendario pueden ser uno solo de datos y el retorno saldría inflado.

    docker compose run --rm --no-deps worker python scripts/exprimir_24_25.py
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
POS = {1: "POR", 2: "DEF", 3: "MED", 4: "DEL", 5: "ENT"}

SQL = text("SELECT fecha, player_id, value, position FROM market_values_historico "
           "WHERE temporada = :t AND value > 0 ORDER BY fecha")


async def main() -> int:
    async with Session() as s:
        filas = [tuple(r) for r in (await s.execute(SQL, {"t": TEMPORADA})).all()]
    por_dia: dict = {}
    pos_de: dict = {}
    for fecha, pid, val, pos in filas:
        por_dia.setdefault(fecha, {})[pid] = val
        pos_de[pid] = pos
    dias = sorted(por_dia)
    print(f"{len(filas):,} observaciones · {len(dias)} días\n")

    def consecutivos(i, n):
        """¿Hay n+1 días seguidos desde dias[i]? Sin esto, un hueco en el archivo haría
        pasar 3 días por 1 y el retorno saldría inflado."""
        return all((dias[i + k + 1] - dias[i + k]).days == 1 for k in range(n))

    # ---------- 1. ¿cuántos días persiste? ----------
    print("-- Persistencia: comprar el top-50 de ayer y mantener N días --")
    print(f"  {'días':>4} {'cartera':>9} {'medio':>9} {'ventaja':>9} {'n':>5}")
    mejor = (0, -99)
    for horizonte in (1, 2, 3, 4, 5, 7, 10):
        ventajas = []
        for i in range(1, len(dias) - horizonte):
            if (dias[i] - dias[i - 1]).days != 1 or not consecutivos(i, horizonte):
                continue
            ayer, hoy, fin = por_dia[dias[i - 1]], por_dia[dias[i]], por_dia[dias[i + horizonte]]
            com = [p for p in hoy if p in ayer and p in fin and ayer[p] > 0 and hoy[p] > 0]
            if len(com) < 100:
                continue
            subida = {p: (hoy[p] - ayer[p]) / ayer[p] for p in com}
            ret = {p: (fin[p] - hoy[p]) / hoy[p] for p in com}
            top = sorted(com, key=lambda p: -subida[p])[:TOP]
            ventajas.append((st.mean(ret[p] for p in top) - st.mean(ret[p] for p in com)) * 100)
        if ventajas:
            v = st.mean(ventajas)
            print(f"  {horizonte:>4} {st.mean(ventajas) + 0:>8.2f}% {'':>9} {v:>8.2f}% {len(ventajas):>5}")
            if v > mejor[1]:
                mejor = (horizonte, v)
    print(f"\n la ventaja acumulada es máxima a los **{mejor[0]} días** ({mejor[1]:+.2f} %)")
    print("     `models/trading.py` vende hoy a los 3 días.")

    # ---------- 2. por tramo de precio ----------
    print("\n-- ¿Sobrevive en todos los tramos de precio? (horizonte 1 día) --")
    tramos = [(0, 1e6, "<1M"), (1e6, 3e6, "1-3M"), (3e6, 8e6, "3-8M"),
              (8e6, 20e6, "8-20M"), (20e6, 9e12, ">20M")]
    acum: dict = {t[2]: [] for t in tramos}
    for i in range(1, len(dias) - 1):
        if (dias[i] - dias[i - 1]).days != 1 or (dias[i + 1] - dias[i]).days != 1:
            continue
        ayer, hoy, man = por_dia[dias[i - 1]], por_dia[dias[i]], por_dia[dias[i + 1]]
        com = [p for p in hoy if p in ayer and p in man and ayer[p] > 0 and hoy[p] > 0]
        if len(com) < 100:
            continue
        subida = {p: (hoy[p] - ayer[p]) / ayer[p] for p in com}
        ret = {p: (man[p] - hoy[p]) / hoy[p] for p in com}
        for lo, hi, nombre in tramos:
            dentro = [p for p in com if lo <= hoy[p] < hi]
            if len(dentro) < 20:
                continue
            top = sorted(dentro, key=lambda p: -subida[p])[:max(5, TOP * len(dentro) // len(com))]
            acum[nombre].append((st.mean(ret[p] for p in top)
                                 - st.mean(ret[p] for p in dentro)) * 100)
    for _lo, _hi, nombre in tramos:
        v = acum[nombre]
        if v:
            print(f"  {nombre:>6}: {st.mean(v):+6.2f} %/día  (n={len(v)})")

    # ---------- 3. por posición ----------
    print("\n-- ¿Y por posición? (horizonte 1 día) --")
    porpos: dict = {}
    for i in range(1, len(dias) - 1):
        if (dias[i] - dias[i - 1]).days != 1 or (dias[i + 1] - dias[i]).days != 1:
            continue
        ayer, hoy, man = por_dia[dias[i - 1]], por_dia[dias[i]], por_dia[dias[i + 1]]
        com = [p for p in hoy if p in ayer and p in man and ayer[p] > 0 and hoy[p] > 0]
        if len(com) < 100:
            continue
        subida = {p: (hoy[p] - ayer[p]) / ayer[p] for p in com}
        ret = {p: (man[p] - hoy[p]) / hoy[p] for p in com}
        for pos in (1, 2, 3, 4):
            dentro = [p for p in com if pos_de.get(p) == pos]
            if len(dentro) < 20:
                continue
            top = sorted(dentro, key=lambda p: -subida[p])[:max(5, len(dentro) // 8)]
            porpos.setdefault(pos, []).append(
                (st.mean(ret[p] for p in top) - st.mean(ret[p] for p in dentro)) * 100)
    for pos in sorted(porpos):
        v = porpos[pos]
        print(f"  {POS.get(pos, pos):>4}: {st.mean(v):+6.2f} %/día  (n={len(v)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
