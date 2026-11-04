#!/usr/bin/env python3
"""Ejecuta el código real contra Postgres y contra SQLite, y compara los resultados.

Por qué esto y no solo el auditor

`auditar_sql_sqlite.py` comprueba que el SQL compile. Eso caza un `::` o una función
que no existe, pero no caza lo peligroso: una consulta que compila en los dos motores y
devuelve cosas distintas. Tres casos concretos:

- `CAST(x AS date)` compila en SQLite y devuelve el año, no una fecha.
- `CAST(x AS jsonb)` compila y devuelve 0 en vez del objeto.
- `buyout_clause / value` sin cast compila y hace división entera: todos los ratios a 0 o 1.

Ninguno da error: los tres producen un número plausible y falso. La única forma de
descartarlos es ejecutar y comparar.

Uso:
    # 1) Vuelca lo que da cada motor
    DATABASE_URL=postgresql+asyncpg://…  python scripts/comparar_motores.py /tmp/pg.json
    DATABASE_URL=sqlite+aiosqlite:////…  python scripts/comparar_motores.py /tmp/lite.json
    # 2) Compara
    python scripts/comparar_motores.py --comparar /tmp/pg.json /tmp/lite.json
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys


def _limpiar(o):
    """Normaliza para poder comparar los dos volcados.

    Las fechas se pasan a texto (Postgres da `datetime`, SQLite a veces texto), los
    booleanos a int (0/1 frente a True/False) y los flotantes se redondean: `sum()` de
    Postgres sobre un `real` acumula en precisión simple y difiere en el último dígito
    sin que eso signifique nada.
    """
    if isinstance(o, dt.datetime):
        return o.astimezone(dt.UTC).isoformat(timespec="seconds")
    if isinstance(o, dt.date):
        return o.isoformat()
    if isinstance(o, bool):
        return int(o)
    if isinstance(o, float):
        return round(o, 4)
    if type(o).__name__ == "Decimal":
        # Postgres devuelve `Decimal` de `round(numeric, n)` y SQLite devuelve `float`:
        # el mismo valor con otro tipo de Python, pero se serializa distinto ('0.90'
        # frente a 0.9). Hay que igualarlo para comparar el dato y no el formato.
        # No es solo cosmético: al llegar a la web un `"0.90"` y un `0.9` se pintan
        # distinto, y el redondeo del último euro puede mover un importe una unidad.
        return round(float(o), 4)
    if isinstance(o, dict):
        return {k: _limpiar(v) for k, v in sorted(o.items())}
    if isinstance(o, (list, tuple)):
        return [_limpiar(x) for x in o]
    if hasattr(o, "__dict__"):
        return _limpiar(vars(o))
    return o


async def recoger() -> dict:
    """Llama a lo que de verdad usa el sistema para decidir."""
    from fantasy_coconut import salud, trabajos
    from fantasy_coconut.models import (
        alineacion,
        clausulas,
        dificultad,
        precio,
        saldos,
        trading,
        vigilancia,
    )

    # Cada entrada es (nombre, corutina). Si una falla se anota el error en vez de
    # abortar: interesa el cuadro completo, no el primer tropiezo.
    pruebas = {
        "saldos.estimar":            saldos.estimar(),
        "vigilancia.mi_saldo":       vigilancia.mi_saldo(),
        "vigilancia.exposicion":     vigilancia.mi_exposicion(100_000_000),
        "vigilancia.proximas":       vigilancia.proximas_liberaciones(),
        "clausulas.objetivos":         clausulas.objetivos_baratos(50_000_000, 10),
        "clausulas.exposicion":        clausulas.exposicion_propia(100_000_000),
        "dificultad.fuerzas":        dificultad.fuerzas(),
        "alineacion.mejor_once":     alineacion.mejor_once(),
        "precio.momentum":           precio.momentum(),
        "saldos.presupuesto_rival":  saldos.presupuesto_rival_maximo(),
        "vigilancia.objetivos":      vigilancia.objetivos(50_000_000),
        "precio.oportunidades":      precio.oportunidades(),
        "trading.cartera":           trading.cartera(),
        "trading.balance":           trading.balance(),
        "trabajos.resumen":          trabajos.resumen(),
        "trabajos.leer_config":      trabajos.leer_config(),
        "salud.revisar":             salud.revisar(),
    }
    salida = {}
    for nombre, coro in pruebas.items():
        try:
            salida[nombre] = _limpiar(await coro)
        except Exception as e:
            salida[nombre] = {"__error__": f"{type(e).__name__}: {e}"}
    return salida


def comparar(a: str, b: str) -> int:
    with open(a) as fa, open(b) as fb:
        pg, lite = json.load(fa), json.load(fb)
    claves = sorted(set(pg) | set(lite))
    fallos = 0
    for k in claves:
        va, vb = pg.get(k), lite.get(k)
        err_pg = isinstance(va, dict) and "__error__" in va
        err_li = isinstance(vb, dict) and "__error__" in vb
        if err_pg and err_li:
            print(f"{k:<26} falla en LOS DOS: {va['__error__'][:70]}")
        elif err_li:
            fallos += 1
            print(f"{k:<26} SOLO falla en SQLite: {vb['__error__'][:90]}")
        elif err_pg:
            print(f"{k:<26} solo falla en Postgres: {va['__error__'][:70]}")
        elif va == vb:
            n = len(va) if isinstance(va, (list, dict)) else 1
            print(f"{k:<26} idéntico ({n} elem.)")
        else:
            fallos += 1
            print(f"{k:<26} DIFIERE")
            ta, tb = json.dumps(va, ensure_ascii=False), json.dumps(vb, ensure_ascii=False)
            print(f"       pg     : {ta[:180]}")
            print(f"       sqlite : {tb[:180]}")
    print(f"\n{len(claves) - fallos}/{len(claves)} coinciden")
    return 1 if fallos else 0


def main() -> None:
    if "--comparar" in sys.argv:
        i = sys.argv.index("--comparar")
        sys.exit(comparar(sys.argv[i + 1], sys.argv[i + 2]))
    destino = sys.argv[1]
    datos = asyncio.run(recoger())
    with open(destino, "w") as f:
        json.dump(datos, f, ensure_ascii=False, indent=1, default=str)
    fallos = sum(1 for v in datos.values() if isinstance(v, dict) and "__error__" in v)
    print(f"{len(datos)} pruebas en {destino} ({fallos} con error)")


if __name__ == "__main__":
    main()
