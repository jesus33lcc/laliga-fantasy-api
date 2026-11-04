#!/usr/bin/env python3
"""Reescribe el SQL de Postgres a una forma que entienden los dos motores.

No traduce a SQLite: traduce a un subconjunto neutro que Postgres y SQLite
interpretan igual. Así no hay dos versiones del SQL que mantener: el mismo código corre
sobre uno u otro motor según `DATABASE_URL`.

Las equivalencias, y por qué cada una

| Postgres | Neutro | Por qué |
|---|---|---|
| `x::int` · `x::bigint` | `CAST(x AS INTEGER)` | directo |
| `x::text` | `CAST(x AS TEXT)` | directo |
| `x::numeric` · `x::float` | `CAST(x AS FLOAT)` | no `REAL`: en Postgres `REAL` es de precisión simple y perdería dígitos en importes de millones. `FLOAT` es doble en los dos |
| `x::date` | `date(x)` | cambia el resultado, no solo la sintaxis: ver abajo |
| `'{}'::jsonb` | `'{}'` | el tipo de la columna ya lo dice |
| `= ANY (ARRAY[…])` | `IN (…)` | mismo significado |
| `DEFAULT now()` | `DEFAULT (now())` | SQLite exige paréntesis al llamar a una función en un DEFAULT; Postgres los acepta |

Por qué `x::date` no puede ser `CAST(x AS date)`

Es la traducción aparente y corrompe el dato sin dar error. En SQLite el tipo `DATE`
tiene afinidad NUMÉRICA, así que `CAST('2026-09-11 22:33:13' AS date)` no da una fecha:
da 2026, el número. Una agrupación por día pasaría a agrupar por año, y el modelo de
precios promediaría la temporada entera en una fila.

`date(x)` existe en los dos motores y devuelve `'YYYY-MM-DD'` en ambos.

Uso:
    python scripts/convertir_sql_neutro.py --ver       # enseña el diff, no toca nada
    python scripts/convertir_sql_neutro.py --aplicar   # escribe los ficheros
"""

from __future__ import annotations

import pathlib
import re
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent

# Ficheros que hablan con Postgres a propósito y no deben tocarse: son los que
# introspeccionan `pg_tables` / `information_schema` para poder migrar.
EXCLUIDOS = {"generar_esquema_sqlite.py", "migrar_a_sqlite.py",
             "convertir_sql_neutro.py", "auditar_sql_sqlite.py"}

CASTS = {
    "int": "CAST({} AS INTEGER)", "integer": "CAST({} AS INTEGER)",
    "bigint": "CAST({} AS INTEGER)", "smallint": "CAST({} AS INTEGER)",
    "text": "CAST({} AS TEXT)",
    "numeric": "CAST({} AS FLOAT)", "float": "CAST({} AS FLOAT)",
    "double precision": "CAST({} AS FLOAT)", "real": "CAST({} AS FLOAT)",
    "date": "date({})",
}

CIERRA = {")": "(", "]": "["}


def _operando(texto: str, fin: int) -> int:
    """Dónde empieza el operando que termina en `fin` (exclusivo).

    Se recorre hacia atrás contando paréntesis, porque el operando de un `::` puede ser
    cualquiera de estas formas y una expresión regular no las distingue:

        ts::date                        -> un identificador
        s.buyout_clause::numeric        -> con punto
        AVG(value)::bigint              -> una llamada a función
        (p.raw->>'points')::int         -> una expresión entre paréntesis
        (SELECT id FROM m LIMIT 1)::text-> una subconsulta entera

    Coger de menos deja SQL roto; coger de más cambia el significado. Por eso se
    equilibran los corchetes en vez de adivinar.
    """
    i = fin
    profundidad = 0
    # Salta espacios pegados al `::`
    while i > 0 and texto[i - 1] in " \t\n":
        i -= 1
    while i > 0:
        c = texto[i - 1]
        # Una cadena literal es ATÓMICA: se salta entera hasta su comilla de apertura.
        #
        # Sin esto, el recorrido se corta en el primer carácter no alfanumérico que haya
        # dentro del literal, y el operando sale a medias: `'2026-09-11'::date` produce
        # SQL roto o, peor, un cast aplicado a otra cosa.
        if c == "'":
            j = texto.rfind("'", 0, i - 1)
            if j == -1:
                break
            i = j
            continue
        if c in CIERRA:
            profundidad += 1
        elif c in "([":
            if profundidad == 0:
                break
            profundidad -= 1
        elif profundidad == 0:  # noqa: SIM102, el comentario de dentro explica el corte
            # Fuera de paréntesis, el operando lo forman identificadores, puntos y
            # dígitos. Cualquier otra cosa (operador, coma, espacio) lo corta.
            if not (c.isalnum() or c in "._\""):
                break
        i -= 1
    return i


def _quitar_casts(sql: str) -> tuple[str, list[str]]:
    cambios = []
    while True:
        m = re.search(r"::\s*(double precision|[a-z]+)", sql)
        if not m:
            break
        tipo = m.group(1).lower()
        plantilla = CASTS.get(tipo)
        ini = _operando(sql, m.start())
        operando = sql[ini:m.start()].strip()
        if plantilla is None:
            # `::jsonb` en un DEFAULT: el tipo de la columna ya lo fija, sobra.
            if tipo in ("jsonb", "json"):
                nuevo = operando
            else:
                raise SystemExit(f" cast sin equivalencia: ::{tipo} en …{sql[max(0,ini-40):m.end()]}")
        else:
            nuevo = plantilla.format(operando)
        cambios.append(f"{operando}::{tipo} → {nuevo}")
        sql = sql[:ini] + nuevo + sql[m.end():]
    return sql, cambios


def _resto(sql: str) -> tuple[str, list[str]]:
    cambios = []
    # `x = ANY (ARRAY['a','b'])` `x IN ('a','b')`
    def _any(m):
        cambios.append("= ANY (ARRAY[…]) IN (…)")
        return f"IN ({m.group(1)})"
    sql = re.sub(r"=\s*ANY\s*\(\s*ARRAY\s*\[(.*?)\]\s*\)", _any, sql, flags=re.S)

    # `DEFAULT now()` `DEFAULT (now())`. SQLite exige los paréntesis; Postgres los
    # acepta. Se evita tocar el que ya los tenga.
    def _def(m):
        cambios.append("DEFAULT now() DEFAULT (now())")
        return "DEFAULT (now())"
    sql = re.sub(r"DEFAULT\s+now\(\)(?!\s*\))", _def, sql, flags=re.I)

    # BIGSERIAL / SERIAL como PK: la forma que sirve en los dos es declarar la columna
    # y dejar que cada motor ponga el autoincremento.
    if re.search(r"\b(big)?serial\b", sql, re.I):
        cambios.append(" BIGSERIAL: requiere revisión MANUAL, no se toca")
    return sql, cambios


def main() -> None:
    aplicar = "--aplicar" in sys.argv
    ficheros = sorted(p for r in ("src", "scripts") for p in (RAIZ / r).rglob("*.py")
                      if p.name not in EXCLUIDOS)
    tocados = 0
    total_cambios = 0
    for f in ficheros:
        original = f.read_text()
        if "::" not in original and "ANY (ARRAY" not in original \
                and not re.search(r"DEFAULT\s+now\(\)(?!\s*\))", original, re.I):
            continue
        nuevo, c1 = _quitar_casts(original)
        nuevo, c2 = _resto(nuevo)
        cambios = c1 + c2
        if nuevo == original:
            continue
        tocados += 1
        total_cambios += len(cambios)
        print(f"\n═══ {f.relative_to(RAIZ)}  ({len(cambios)} cambios) ═══")
        for c in dict.fromkeys(cambios):
            print(f"   {c}")
        if aplicar:
            f.write_text(nuevo)
    print(f"\n{tocados} ficheros · {total_cambios} cambios"
          f"{' APLICADOS' if aplicar else ' (simulación: usa --aplicar)'}")


if __name__ == "__main__":
    main()
