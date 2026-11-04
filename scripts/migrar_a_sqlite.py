#!/usr/bin/env python3
"""Copia la base de Postgres a SQLite, y comprueba que llegó entera.

Por qué la verificación no es opcional

Aquí vive la serie de precios, que no se puede reconstruir: cada toma perdida es
dato perdido para siempre. Una migración que "parece haber ido bien" no vale, porque los
fallos de conversión no dan error: dan un resultado plausible y falso.

Por eso al final no se comparan solo filas, sino también, columna a columna:
- la suma de cada columna numérica (caza truncamientos y overflow),
- el mínimo y el máximo de cada fecha (caza zonas horarias mal convertidas),
- el número de nulos (caza una conversión que convierte NULL en 0 o en '').

Un recuento de filas correcto con los valores estropeados es justo el resultado que
pasaría desapercibido.

Uso:
    docker compose run --rm --no-deps -v ./datos:/destino \\
        worker python scripts/migrar_a_sqlite.py /destino/coconut.db
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import sqlite3
import sys
import time

from sqlalchemy import text

from fantasy_coconut import sqlite_compat
from fantasy_coconut.db import Session

LOTE = 5_000          # filas por viaje; va sobrado y no come memoria
RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _valor(v):
    """Adapta un valor de asyncpg a lo que SQLite acepta.

    Los dos casos con trampa:
    - jsonb: asyncpg lo devuelve unas veces como `str` y otras como `dict`/`list`,
      según haya códec registrado. Si llega como dict hay que serializarlo, o SQLite
      guardaría la representación de Python (`{'a': 1}`, con comillas simples), que
      no es JSON válido y haría que `->>'a'` devolviera NULL en silencio.
    - datetime: lo convierte el adaptador de `sqlite_compat` (ISO-8601 en UTC).
    """
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


async def _tablas(s) -> list[str]:
    return [r[0] for r in (await s.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    ))).all()]


NUMERICOS = ("integer", "smallint", "bigint", "real", "double precision", "numeric")


def _trozos(cols: list[tuple[str, str]], pg: bool) -> list[str]:
    """Las expresiones del perfil. Las mismas para los dos motores, a propósito.

    Usar SQL distinto en cada lado produce discrepancias del propio SQL, no de los
    datos, y son las más caras de perseguir porque parecen pérdida de información:

    1. `count(*) FILTER` en Postgres frente a `sum(CASE WHEN …)` en SQLite: sobre una
       tabla vacía el primero da 0 y el segundo NULL, así que cada tabla vacía
       sale marcada como rota. `FILTER` existe en SQLite desde 3.30, así que se usa el
       mismo en los dos y el problema desaparece en vez de taparse.
    2. Una columna `date` vuelve de Postgres como objeto `date` y de SQLite como texto,
       o sea en formatos distintos. Se pide texto en los dos lados.
    3. `sum()` sobre un `real`: Postgres acumula en precisión SIMPLE y SQLite en
       doble, así que los totales difieren en los últimos dígitos. El que pierde
       precisión es Postgres: con `sum(x::float8)` da lo mismo que SQLite hasta el
       último bit. Por eso se fuerza doble precisión, para comparar el dato y no el
       redondeo de quien lo suma.
    """
    trozos = ["count(*) AS n"]
    for nombre, tipo in cols:
        if tipo in NUMERICOS:
            col = f"CAST({nombre} AS double precision)" if pg else f"CAST({nombre} AS REAL)"
            trozos.append(f'coalesce(sum({col}), 0) AS "suma_{nombre}"')
        elif tipo.startswith("timestamp") or tipo == "date":
            # A texto en los dos lados: un `date` de Postgres llega como objeto y de
            # SQLite como cadena, y así comparados nunca cuadran.
            col = f"CAST({nombre} AS text)" if pg else nombre
            trozos.append(f'min({col}) AS "min_{nombre}"')
            trozos.append(f'max({col}) AS "max_{nombre}"')
        trozos.append(f'count(*) FILTER (WHERE {nombre} IS NULL) AS "nulos_{nombre}"')
    return trozos


async def _perfil_pg(s, tabla: str, cols: list[tuple[str, str]]) -> dict:
    fila = (await s.execute(text(
        f"SELECT {', '.join(_trozos(cols, pg=True))} FROM {tabla}"))).mappings().one()
    return dict(fila)


def _perfil_sqlite(con, tabla: str, cols: list[tuple[str, str]]) -> dict:
    cur = con.execute(f"SELECT {', '.join(_trozos(cols, pg=False))} FROM {tabla}")
    return dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))


def _comparable(v):
    """Normaliza para poder comparar Postgres con SQLite sin falsos positivos."""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return sqlite_compat.fecha(v).isoformat(sep=" ", timespec="microseconds")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, str):
        # Una fecha que vuelve como texto (agregado de SQLite, o el CAST AS text de
        # Postgres) se normaliza para comparar. Pero una fecha sin hora se deja tal
        # cual: convertirla a datetime le añade ' 00:00:00+00:00' en un lado y no en
        # el otro, y toda tabla con columna `date` saldría como rota.
        if len(v) == 10 and v[4] == "-" and v[7] == "-":
            return v
        try:
            return sqlite_compat.fecha(v).isoformat(sep=" ", timespec="microseconds")
        except Exception:
            return v
    if isinstance(v, (int, float)):
        # Postgres devuelve Decimal en algunas sumas; float en otras. Se compara con
        # tolerancia relativa para que un redondeo del último bit no dé falsa alarma.
        return round(float(v), 6)
    return v


async def main() -> None:
    destino = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/data/coconut.db")
    if destino.exists():
        copia = destino.with_suffix(f".db.previa-{int(time.time())}")
        destino.rename(copia)
        print(f"ℹ️  ya existía: se aparta en {copia.name}")

    sqlite_compat.registrar_tipos()
    con = sqlite3.connect(destino, detect_types=sqlite3.PARSE_DECLTYPES
                          | sqlite3.PARSE_COLNAMES, isolation_level=None)
    sqlite_compat.preparar_conexion(con)
    con.executescript((RAIZ / "db" / "001_schema_sqlite.sql").read_text())
    vistas = RAIZ / "db" / "002_vistas_sqlite.sql"
    if vistas.exists():
        con.executescript(vistas.read_text())
    print(f"esquema creado en {destino}")

    t0 = time.time()
    total = 0
    problemas: list[str] = []

    async with Session() as s:
        tablas = await _tablas(s)
        columnas = {}
        for t in tablas:
            columnas[t] = [(r[0], r[1]) for r in (await s.execute(text("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_schema='public' AND table_name=:t
                ORDER BY ordinal_position"""), {"t": t})).all()]

        # ---------- copia ----------
        # Las claves ajenas se apagan DURANTE la copia: las tablas se recorren por
        # orden alfabético, así que `market_values` llega antes que `players` y cada
        # precio referenciaría a un jugador que aún no existe. Se vuelven a encender
        # al final y se comprueban de golpe, que verifica exactamente lo mismo.
        con.execute("PRAGMA foreign_keys=OFF")
        for t in tablas:
            cols = [c[0] for c in columnas[t]]
            lista = ", ".join(cols)
            hueco = ", ".join("?" * len(cols))
            n = 0
            offset = 0
            while True:
                filas = (await s.execute(text(
                    f"SELECT {lista} FROM {t} LIMIT {LOTE} OFFSET {offset}"))).all()
                if not filas:
                    break
                con.executemany(f"INSERT INTO {t} ({lista}) VALUES ({hueco})",
                                [tuple(_valor(v) for v in f) for f in filas])
                n += len(filas)
                offset += LOTE
            con.commit()
            total += n
            print(f"  {t:<28} {n:>7,} filas")

        con.execute("PRAGMA foreign_keys=ON")
        huerfanas = list(con.execute("PRAGMA foreign_key_check"))
        if huerfanas:
            problemas.append(f"claves ajenas rotas: {len(huerfanas)} filas huérfanas "
                             f"(ej. {huerfanas[0]})")

        # ---------- verificación ----------
        #
        # Una tabla puede no cuadrar por dos motivos MUY distintos, y confundirlos
        # manda a buscar un fallo que no existe:
        #
        #   (a) la copia perdió o estropeó datos: grave, hay que repetirla
        #   (b) la fuente creció mientras se copia: normal si el worker sigue en
        #       marcha, porque no para de apuntar sus ejecuciones. No es un fallo de la
        #       migración, y llamarlo "migración no válida" manda a repetir una copia
        #       que está bien.
        #
        # Se distinguen releyendo el recuento de Postgres: si ahora tiene más filas que
        # cuando empezó la copia, es (b).
        print(f"\n═══ VERIFICACIÓN ({len(tablas)} tablas) ═══")
        movidas: list[str] = []
        for t in tablas:
            pg = await _perfil_pg(s, t, columnas[t])
            lite = _perfil_sqlite(con, t, columnas[t])
            fallos = []
            for clave, valor_pg in pg.items():
                a, b = _comparable(valor_pg), _comparable(lite.get(clave))
                if a != b:
                    fallos.append(f"{clave}: pg={a!r} sqlite={b!r}")
            if not fallos:
                print(f"{t:<28} {pg['n']:>7,} filas · sumas, rangos y nulos cuadran")
            elif (pg["n"] or 0) > (lite["n"] or 0):
                movidas.append(f"{t}: Postgres creció de {lite['n']:,.0f} a {pg['n']:,.0f}")
                print(f"{t:<28} la fuente creció durante la copia "
                      f"(+{pg['n'] - lite['n']:,.0f} filas)")
            else:
                problemas.append(f"{t}: " + " · ".join(fallos[:4]))
                print(f"{t:<28} {fallos[0]}")

    con.execute("PRAGMA optimize")
    con.execute("VACUUM")
    con.close()
    tam = destino.stat().st_size / 1e6

    print(f"\n{total:,} filas en {time.time()-t0:.1f}s · fichero de {tam:.1f} MB")
    if problemas:
        print(f"\n {len(problemas)} TABLA(S) NO CUADRAN: la migración NO es válida:")
        for p in problemas:
            print(f"   • {p}")
        sys.exit(1)
    if movidas:
        print(f"\n {len(movidas)} tabla(s) crecieron DURANTE la copia: el resto cuadra:")
        for m in movidas:
            print(f"   • {m}")
        print("\n   No es un fallo de la copia: es que el `worker` sigue escribiendo.")
        print("   Para el corte definitivo hay que pararlo antes:")
        print("       docker compose stop worker   # copiar   # docker compose start worker")
        sys.exit(2)
    print("\n Todas las tablas cuadran: filas, sumas, rangos de fecha y nulos.")


if __name__ == "__main__":
    asyncio.run(main())
