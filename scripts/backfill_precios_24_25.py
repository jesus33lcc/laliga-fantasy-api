"""Trae la serie de precios de la temporada 24/25 desde un archivo público (MIT).

Qué resuelve

La serie propia es irrecuperable hacia atrás: empieza cuando se montó la ingesta.
`Mixtomeister/laliga-fantasy-api-archive` publica snapshots diarios de todos los
jugadores de la temporada 24/25 completa, con licencia MIT, y con eso se puede
contrastar el momentum contra una temporada entera y medir el efecto de los parones de
selecciones.

Va a una tabla APARTE

`market_values` alimenta el momentum con el que se opera. Mezclar ahí otra temporada
(otros jugadores, otros `player_id`, otro contexto) contaminaría esa señal. Va a
`market_values_historico`, con su columna `temporada`.

Los `player_id` no son comparables entre temporadas: el id del maestro puede
reutilizarse. Esta tabla sirve para medir el mecanismo (¿persiste el momentum? ¿cuánto
dura? ¿qué pasa en los parones?), no para predecir a un jugador concreto de hoy.

Peso y reanudación

El grueso de cada JSON diario es `playerStats`, que no se usa: se descarga día a día, se
extrae lo mínimo y se descarta, así que en disco no queda nada.

Es reanudable: cada día se marca al terminar, así que una ejecución cortada se retoma
donde se quedó en vez de volver a descargarlo todo.

    docker compose run --rm --no-deps worker python scripts/backfill_precios_24_25.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys

import httpx
from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fantasy_coconut.db import Session

REPO = "Mixtomeister/laliga-fantasy-api-archive"
CRUDO = f"https://raw.githubusercontent.com/{REPO}/main"
TEMPORADA = "2024-25"

DDL = """
CREATE TABLE IF NOT EXISTS market_values_historico (
    temporada  text    NOT NULL,
    fecha      date    NOT NULL,
    player_id  integer NOT NULL,
    nickname   text,
    position   smallint,
    value      bigint,
    points     integer,
    PRIMARY KEY (temporada, fecha, player_id)
);
CREATE INDEX IF NOT EXISTS idx_mvh_jugador ON market_values_historico (temporada, player_id, fecha);
CREATE TABLE IF NOT EXISTS backfill_dias (
    temporada text NOT NULL,
    fecha     date NOT NULL,
    filas     integer,
    ts        timestamptz NOT NULL DEFAULT (now()),
    PRIMARY KEY (temporada, fecha)
);
"""

INSERT = text("""
INSERT INTO market_values_historico
    (temporada, fecha, player_id, nickname, position, value, points)
VALUES (:t, :f, :pid, :nick, :pos, :val, :pts)
ON CONFLICT (temporada, fecha, player_id) DO NOTHING
""")


async def listar_dias(cli: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Los ficheros del archivo, como (ruta, fecha ISO). Un árbol, una petición."""
    r = await cli.get(f"https://api.github.com/repos/{REPO}/git/trees/main?recursive=1")
    r.raise_for_status()
    fuera = []
    for nodo in r.json().get("tree", []):
        ruta = nodo.get("path", "")
        if nodo.get("type") != "blob" or not ruta.startswith("archive/") \
                or not ruta.endswith(".json"):
            continue
        # archive/YYYYMM/DD.json
        try:
            _, aamm, dd = ruta.split("/")
            fuera.append((ruta, f"{aamm[:4]}-{aamm[4:6]}-{dd[:2]}"))
        except ValueError:
            continue
    return sorted(fuera, key=lambda x: x[1])


async def main() -> int:
    async with Session() as s:
        for sentencia in DDL.strip().split(";"):
            if sentencia.strip():
                await s.execute(text(sentencia))
        await s.commit()
        hechos = {str(r[0]) for r in await s.execute(
            text("SELECT fecha FROM backfill_dias WHERE temporada = :t"), {"t": TEMPORADA})}

    async with httpx.AsyncClient(timeout=120, follow_redirects=True,
                                 headers={"User-Agent": "coconut-backfill"}) as cli:
        dias = await listar_dias(cli)
        pendientes = [(r, f) for r, f in dias if f not in hechos]
        print(f"archivo: {len(dias)} días · ya hechos {len(hechos)} · "
              f"pendientes {len(pendientes)}")
        if not pendientes:
            print("nada que traer.")
            return 0

        total = 0
        for i, (ruta, fecha) in enumerate(pendientes, 1):
            try:
                r = await cli.get(f"{CRUDO}/{ruta}")
                r.raise_for_status()
                datos = r.json()
            except Exception as e:
                # Un día que falle no debe tumbar el barrido: se salta y se reintenta en
                # la siguiente ejecución, porque no queda marcado como hecho.
                print(f"{fecha}: {str(e)[:70]}")
                continue

            # asyncpg no convierte texto a `date`: quiere un `datetime.date` de verdad.
            # Con la cadena revienta con "'str' object has no attribute 'toordinal'", que
            # no se parece en nada a "esto debería ser una fecha".
            dia = dt.date.fromisoformat(fecha)

            filas = []
            for p in (datos if isinstance(datos, list) else []):
                try:
                    pid = int(p["id"])
                    val = int(p.get("marketValue") or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                if val <= 0:
                    continue        # sin valor no aporta nada a una serie de precios
                filas.append({
                    "t": TEMPORADA, "f": dia, "pid": pid,
                    "nick": (p.get("nickname") or "")[:80],
                    "pos": int(p.get("positionId") or 0) or None,
                    "val": val,
                    "pts": int(p.get("points") or 0),
                })

            async with Session() as s:
                if filas:
                    await s.execute(INSERT, filas)
                await s.execute(
                    text("INSERT INTO backfill_dias (temporada, fecha, filas) "
                         "VALUES (:t, :f, :n) ON CONFLICT (temporada, fecha) "
                         "DO UPDATE SET filas = EXCLUDED.filas"),
                    {"t": TEMPORADA, "f": dia, "n": len(filas)})
                await s.commit()

            total += len(filas)
            if i % 20 == 0 or i == len(pendientes):
                print(f"  {i}/{len(pendientes)} · {fecha} · {total:,} filas acumuladas")
            # Cortesía con raw.githubusercontent: no hay límite publicado para raw, pero
            # 337 descargas seguidas de 6 MB merecen un respiro.
            await asyncio.sleep(0.3)

    async with Session() as s:
        res = (await s.execute(text(
            "SELECT count(*), count(DISTINCT player_id), min(fecha), max(fecha) "
            "FROM market_values_historico WHERE temporada = :t"), {"t": TEMPORADA})).first()
    print(f"\n {res[0]:,} filas · {res[1]} jugadores · {res[2]} {res[3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
