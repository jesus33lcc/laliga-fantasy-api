"""Rellena `squad_snapshots.purchase_price` desde el tablón y mide las subidas de cláusula.

Uso:
    docker compose run --rm --no-deps worker python /app/scripts/backfill_purchase_price.py
    ... --escribir            aplica el relleno (sin esto solo informa)
    ... --control <euros>     contrasta las subidas del equipo propio con una cifra conocida

La API no expone `purchasePrice`, así que el precio de compra sale del tablón: un evento
`market_buy` (compra al sistema) o `clausulazo` (compra a otro manager) donde el dueño del
snapshot es quien paga. Para cada ficha sin precio se coge el evento de adquisición más
reciente anterior a ese snapshot.

Lo que NO se puede rellenar así son las fichas de la plantilla inicial: nunca se compraron,
así que no tienen evento. Para esas, el segundo bloque deduce el gasto en cláusulas
comparando la serie de cláusulas con la de valores: una cláusula que sube cuando el valor
sigue por debajo de la anterior solo puede haberla subido su dueño pagando, y con
`factor=2` la subida cuesta la mitad de lo que sube.

Ese gasto es el único movimiento de dinero ajeno que el tablón no publica, o sea lo que
convierte la caja rival estimada de cota superior en cifra.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys

sys.path.insert(0, "/app/src")

from sqlalchemy import text

from fantasy_coconut.db import Session

# Subconsulta correlacionada y no `UPDATE ... FROM`: SQLite no acepta alias en la tabla
# que actualiza (`near "s": syntax error`) y `DISTINCT ON` es solo de Postgres. Es además
# la misma expresión que usa `ingest/league.py` al escribir cada snapshot, así que el
# histórico se rellena con el criterio con el que se escribe lo nuevo.
#
# El `EXISTS` evita recorrer las filas que no tienen evento: sin él se les asigna el NULL
# que ya tenían y el recuento de filas rellenadas cuenta de más.
RELLENAR = text("""
    UPDATE squad_snapshots
       SET purchase_price = (
           SELECT ev.amount FROM league_events ev
            WHERE ev.player_id = squad_snapshots.player_id
              AND ev.manager_id = squad_snapshots.manager_id
              AND ev.kind IN ('market_buy', 'clausulazo')
              AND ev.amount IS NOT NULL
              AND ev.ts <= squad_snapshots.ts
            ORDER BY ev.ts DESC LIMIT 1)
     WHERE purchase_price IS NULL
       AND EXISTS (
           SELECT 1 FROM league_events ev
            WHERE ev.player_id = squad_snapshots.player_id
              AND ev.manager_id = squad_snapshots.manager_id
              AND ev.kind IN ('market_buy', 'clausulazo')
              AND ev.amount IS NOT NULL
              AND ev.ts <= squad_snapshots.ts)
""")

SERIE = text("""
    SELECT m.name AS manager, p.nickname AS jugador, s.player_id, s.ts, s.buyout_clause
    FROM squad_snapshots s
    JOIN managers m ON m.id = s.manager_id
    JOIN players  p ON p.id = s.player_id
    WHERE s.buyout_clause IS NOT NULL
    ORDER BY m.name, s.player_id, s.ts
""")

# La serie de precios entera, para comparar cada cláusula con el valor que tenía el jugador
# por esas fechas. Dos formas que no funcionan y por qué:
#
#   - el valor del propio snapshot: la serie tiene 4 tomas al día y los snapshots muchas
#     más, así que el valor leído va un tick por detrás de la subida y TODA subida sale
#     clasificada como manual;
#   - coincidencia al euro con algún valor de la serie: el 58 % de los jugadores cambia de
#     precio más de una vez al día, así que la cláusula sigue a valores que la serie no
#     llegó a muestrear y aparecen subidas manuales de +276 €.
#
# Lo que sí discrimina es el MÁXIMO de la ventana: el recálculo no puede dejar la cláusula
# por encima de ningún valor que el jugador haya tenido esos días, y una subida pagada sí.
VALORES = text("SELECT player_id, ts, value FROM market_values ORDER BY player_id, ts")
VENTANA_H = 36

MIO = text("SELECT name FROM managers WHERE is_me LIMIT 1")

FALTAN = text("""
    SELECT m.name AS manager, count(*) AS sin_precio
    FROM squad_snapshots s
    JOIN managers m ON m.id = s.manager_id
    WHERE s.ts = (SELECT max(ts) FROM squad_snapshots) AND s.purchase_price IS NULL
    GROUP BY m.name ORDER BY m.name
""")


async def main(escribir: bool, control: int = 0) -> int:
    async with Session() as s:
        mio = (await s.execute(MIO)).scalar()
        if escribir:
            filas = (await s.execute(RELLENAR)).rowcount
            await s.commit()
            print(f"filas rellenadas: {filas}")
        else:
            print("modo informe: no se escribe nada (usa --escribir)")

        print("\n--- fichas del último snapshot que siguen sin precio de compra ---")
        for r in (await s.execute(FALTAN)).all():
            print(f"  {r[0]:<16} {r[1]}")

        print("\n--- subidas de cláusula deducidas de la serie ---")
        filas = (await s.execute(SERIE)).all()
        valores: dict[int, list[tuple[dt.datetime, int]]] = {}
        for pid, ts, valor in (await s.execute(VALORES)).all():
            valores.setdefault(pid, []).append((ts, valor))

    def techo(pid: int, ts: dt.datetime) -> int | None:
        """El valor más alto que tuvo el jugador en las 36 h a cada lado."""
        serie = valores.get(pid)
        if not serie:
            return None
        desde, hasta = ts - dt.timedelta(hours=VENTANA_H), ts + dt.timedelta(hours=VENTANA_H)
        cerca = [v for t, v in serie if desde <= t <= hasta]
        return max(cerca) if cerca else None

    anterior: dict[int, int] = {}
    manual: dict[str, int] = {}
    por_valor: dict[str, int] = {}
    detalle: list[tuple[str, str, int, int]] = []

    for manager, jugador, pid, ts, clausula in filas:
        clausula_prev = anterior.get(pid)
        anterior[pid] = clausula
        if clausula_prev is None:
            continue
        subida = clausula - clausula_prev
        if subida <= 0:
            continue
        tope = techo(pid, ts)
        if tope is not None and clausula <= tope:
            por_valor[manager] = por_valor.get(manager, 0) + subida
        else:
            manual[manager] = manual.get(manager, 0) + subida
            detalle.append((manager, jugador, clausula_prev, clausula))

    managers = sorted(set(manual) | set(por_valor))
    print(f"  {'manager':<16}{'subido a mano':>16}{'coste (mitad)':>16}{'por el valor':>16}")
    for m in managers:
        a_mano = manual.get(m, 0)
        print(f"  {m:<16}{a_mano:>16,}{a_mano // 2:>16,}{por_valor.get(m, 0):>16,}")

    # El método necesita contraste o es solo una cifra plausible. El único gasto en
    # cláusulas que se conoce al euro es el del equipo propio, que se despeja del desvío
    # del estimador de saldos. Se pasa con `--control <euros>` porque no sale de la API.
    if control and mio:
        propio = manual.get(mio, 0) // 2
        desvio = propio - control
        print(f"\n  control sobre el equipo propio: medido al euro en {control:,}, "
              f"este método da {propio:,} ({desvio:+,}, {abs(desvio) / control:.1%})")

    print(f"\n  subidas a mano detectadas: {len(detalle)}")
    for manager, jugador, antes, despues in detalle[:30]:
        print(f"    {manager:<16}{jugador:<16}{antes:>14,} -> {despues:>14,}"
              f"  (+{despues - antes:,})")
    return 0


def _control(argv: list[str]) -> int:
    if "--control" in argv:
        i = argv.index("--control")
        if i + 1 < len(argv):
            return int(argv[i + 1].replace("_", "").replace(".", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--escribir" in sys.argv, _control(sys.argv))))
