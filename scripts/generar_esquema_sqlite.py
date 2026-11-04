#!/usr/bin/env python3
"""Genera el esquema SQLite leyendo la BD viva de Postgres.

Por qué se introspecciona y no se traduce `db/001_schema.sql`

Porque ese fichero no declara todas las tablas: alrededor de la mitad (`ajustes`,
`jobs_log`, `seguimientos`, `tareas`, `usuarios`, `market_values_historico`…) las crea el
propio código con `CREATE TABLE IF NOT EXISTS` al arrancar, repartidas por los módulos que
las usan. Traducir el fichero produce una base a la que le falta la mitad, y el fallo no
sale hasta que algún job intenta escribir.

Leer `information_schema` da lo que hay de verdad, incluidas esas.

Qué no traduce, a propósito

Las vistas (`v_latest_squads`, `v_latest_values`, `v_clausula_targets`) usan
`DISTINCT ON`, que SQLite no tiene. Se reescriben a mano con `row_number()` en
`db/002_vistas_sqlite.sql`: son tres, y una traducción automática de algo que decide
qué fila gana es justo donde no conviene ahorrar.

Uso:
    docker compose run --rm --no-deps worker python scripts/generar_esquema_sqlite.py \
        > db/001_schema_sqlite.sql
"""

from __future__ import annotations

import asyncio
import re
import sys

from sqlalchemy import text

from fantasy_coconut.db import Session

# ---------- mapa de tipos ----------
# SQLite solo tiene 5 afinidades, pero conserva el nombre declarado, y de ahí lo
# lee `PARSE_DECLTYPES` para decidir el conversor. O sea que el nombre no es
# decorativo: escribir `TIMESTAMPTZ` es lo que hace que la columna vuelva como
# `datetime` en vez de como texto.
TIPOS = {
    "integer": "INTEGER", "smallint": "INTEGER", "bigint": "INTEGER",
    "text": "TEXT", "character varying": "TEXT", "character": "TEXT",
    # `JSONTEXT`, no `TEXT`: contiene "TEXT" (así que la afinidad es de texto y el
    # JSON se guarda tal cual, con `->>` funcionando encima), pero es un nombre propio,
    # y eso es lo que permite engancharle el conversor que lo devuelve como `dict`,
    # igual que hace asyncpg con `jsonb`. Con `TEXT` a secas volvería como cadena y
    # cada `fila.raw["campo"]` del proyecto dejaría de funcionar.
    "jsonb": "JSONTEXT", "json": "JSONTEXT",
    "timestamp with time zone": "TIMESTAMPTZ",  # dispara el conversor a datetime
    "timestamp without time zone": "TIMESTAMP",
    "date": "DATE",
    "boolean": "BOOLEAN",                       # dispara el conversor a bool
    "real": "REAL", "double precision": "REAL", "numeric": "REAL",
}

# Valores por defecto de Postgres que hay que traducir.
DEFECTOS = {
    "now()": "(now())",           # función registrada en sqlite_compat
    "CURRENT_TIMESTAMP": "(now())",
    "true": "1", "false": "0",
}


def _defecto(d: str | None) -> str:
    if d is None:
        return ""
    d = d.strip()
    if d.startswith("nextval("):          # lo aporta el AUTOINCREMENT del PK
        return ""
    for pg, lite in DEFECTOS.items():
        if d.lower().startswith(pg.lower()):
            return f" DEFAULT {lite}"
    # Recorta el cast que Postgres añade a los literales: 'pendiente'::text
    if "::" in d:
        d = d.split("::")[0].strip()
    return f" DEFAULT {d}"


def _limpiar_indice(ddl: str) -> str:
    """Pasa un `CREATE INDEX` de Postgres a SQLite.

    Lo único con trampa son los casts dentro de un índice parcial:
    `WHERE (estado = 'activo'::text)`. SQLite no entiende `::` y falla al crear la
    base entera. Aquí se quitan, que en un predicado es equivalente porque SQLite
    es de tipado dinámico: pero se avisa por stderr de cada uno, porque quitar un
    cast a ciegas es justo el tipo de cambio que no debería pasar desapercibido.
    """
    ddl = (ddl.replace("USING btree ", "").replace("USING hash ", "")
              .replace("public.", "")
              .replace(" NULLS FIRST", "").replace(" NULLS LAST", ""))
    casts = re.findall(r"::\s*\w+", ddl)
    if casts:
        print(f"cast(s) {set(casts)} quitados de: {ddl[:70]}…", file=sys.stderr)
        ddl = re.sub(r"::\s*\w+", "", ddl)
    # `x = ANY (ARRAY['a','b'])` es la forma de Postgres de un `IN`. SQLite no tiene
    # tipo array, pero `IN (…)` significa exactamente lo mismo.
    ddl = re.sub(r"=\s*ANY\s*\(\s*ARRAY\s*\[(.*?)\]\s*\)", r"IN (\1)", ddl, flags=re.S)
    ddl = ddl.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ")
    ddl = ddl.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
    return ddl.rstrip(";")


async def main() -> None:
    async with Session() as s:
        tablas = [r[0] for r in (await s.execute(text("""
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public' ORDER BY tablename"""))).all()]

        cols = {}
        for t in tablas:
            cols[t] = (await s.execute(text("""
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name=:t
                ORDER BY ordinal_position"""), {"t": t})).all()

        # Claves primarias
        pks = {}
        for t in tablas:
            pks[t] = [r[0] for r in (await s.execute(text("""
                SELECT a.attname
                FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid
                                   AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = CAST(:t AS regclass) AND i.indisprimary
                ORDER BY array_position(i.indkey, a.attnum)"""), {"t": t})).all()]

        # Índices (únicos y normales), saltando el que respalda la PK
        indices = (await s.execute(text("""
            SELECT tablename, indexname, indexdef FROM pg_indexes
            WHERE schemaname='public' ORDER BY tablename, indexname"""))).all()

        # Claves ajenas
        fks = {}
        for t in tablas:
            fks[t] = (await s.execute(text("""
                SELECT kcu.column_name, ccu.table_name, ccu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.constraint_column_usage ccu
                  ON ccu.constraint_name = tc.constraint_name
                WHERE tc.constraint_type='FOREIGN KEY'
                  AND tc.table_schema='public' AND tc.table_name=:t"""), {"t": t})).all()

    out = sys.stdout.write
    out("-- Esquema SQLite de Coconut: generado, no editar a mano.\n")
    out("--   docker compose run --rm --no-deps worker \\\n")
    out("--       python scripts/generar_esquema_sqlite.py > db/001_schema_sqlite.sql\n")
    out("--\n")
    out("-- Sale de introspeccionar la BD viva, porque db/001_schema.sql no declara\n")
    out("-- todas las tablas reales (el resto las crea el código al arrancar).\n")
    out("\n")

    for t in tablas:
        pk = pks[t]
        # Solo un PK de UNA columna entera se declara en línea: es la única forma que
        # SQLite acepta el AUTOINCREMENT que sustituye al BIGSERIAL de Postgres.
        # Todo lo demás (PK compuesta, o de una columna de texto) va como restricción
        # de tabla al final.
        #
        # Hay que cubrir los TRES casos. Emitir solo el de una columna entera y el
        # compuesto hace desaparecer una PK de una sola columna de texto sin dar
        # ningún error, y sin ella un `ON CONFLICT (clave) DO UPDATE` falla y la tabla
        # se llena de duplicados.
        pk_en_linea = len(pk) == 1 and TIPOS.get(
            {c[0]: c[1] for c in cols[t]}.get(pk[0], "")) == "INTEGER"

        lineas = []
        for nombre, tipo, nullable, defecto in cols[t]:
            tipo_lite = TIPOS.get(tipo)
            if tipo_lite is None:
                raise SystemExit(f" tipo sin mapear: {t}.{nombre} es {tipo!r}")
            col = f"    {nombre:<22} {tipo_lite}"
            es_serial = defecto and defecto.startswith("nextval(")
            if pk_en_linea and pk == [nombre]:
                col += " PRIMARY KEY"
                if es_serial:
                    col += " AUTOINCREMENT"
            else:
                if nullable == "NO":
                    col += " NOT NULL"
                col += _defecto(defecto)
            lineas.append(col)

        if pk and not pk_en_linea:
            lineas.append(f"    PRIMARY KEY ({', '.join(pk)})")
        elif not pk:
            print(f"{t}: sin PRIMARY KEY en Postgres", file=sys.stderr)
        for col, t_ref, col_ref in fks[t]:
            lineas.append(f"    FOREIGN KEY ({col}) REFERENCES {t_ref}({col_ref})")

        out(f"CREATE TABLE IF NOT EXISTS {t} (\n")
        out(",\n".join(lineas))
        out("\n);\n")

        for tabla, nombre, ddl in indices:
            if tabla != t or nombre == f"{t}_pkey":
                continue
            # Postgres respalda cada UNIQUE con un índice cuyo nombre acaba en _key.
            # `UNIQUE NULLS NOT DISTINCT` no existe en SQLite: se replica con un
            # índice sobre coalesce(), que sí trata dos NULL como iguales (verificado).
            if "NULLS NOT DISTINCT" in ddl.upper():
                dentro = ddl[ddl.index("(") + 1:ddl.rindex(")")]
                dentro = dentro.replace("NULLS NOT DISTINCT", "").strip()
                exprs = [f"coalesce({c.strip()}, '∅')" for c in dentro.split(",")]
                out(f"-- UNIQUE NULLS NOT DISTINCT: SQLite no lo tiene. Con un UNIQUE\n"
                    f"-- normal dos NULL cuentan como distintos y la tabla se llenaría\n"
                    f"-- de duplicados SIN dar error.\n"
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {nombre} ON {t} "
                    f"({', '.join(exprs)});\n")
                continue
            out(_limpiar_indice(ddl) + ";\n")
        out("\n")

    out("-- Las vistas van en db/002_vistas_sqlite.sql: usan DISTINCT ON y se\n")
    out("-- reescriben a mano con row_number().\n")


if __name__ == "__main__":
    asyncio.run(main())
