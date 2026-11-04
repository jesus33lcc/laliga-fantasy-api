"""Ensayo de restauración: exporta, restaura en una base VACÍA y compara.

Por qué existe

El fallo típico de una copia de seguridad no es perder el fichero: es descubrir el día
que hace falta que no se puede volver a meter. Este script hace el viaje entero contra
una base nueva y falla si algo no cuadra.

Y de paso cubre dos cosas que no se ven de otra forma:

- Cruza los dos motores. Exporta de Postgres y restaura en SQLite, que es a donde va
  la app del móvil. Si alguna columna se traduce mal (un `jsonb` que vuelve como texto, una
  fecha que pierde la zona) aquí se ve, y no en el teléfono de alguien.
- Comprueba que la copia es determinista: la exportación de la base restaurada tiene
  que ser idéntica byte a byte a la original. Si no lo fuera, ni el hash serviría para
  saber si hay que subir, ni git podría hacer diffs pequeños.

    docker compose run --rm --no-deps worker python scripts/probar_respaldo.py
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import sys
import tempfile


async def main() -> int:
    from fantasy_coconut import respaldo

    print("1· exportando de la base viva…")
    copia = await respaldo.exportar(completa=False)
    meta = json.loads(copia["meta.json"])
    h_origen = respaldo.huella(copia)
    print(f"   {sum(meta['filas'].values())} filas · huella {h_origen}")

    # La base de destino es nueva y desechable. Se cambia por variable de entorno y se
    # recarga el módulo: así el segundo tramo habla con SQLite sin tocar la instalación.
    tmp = pathlib.Path(tempfile.mkdtemp()) / "ensayo.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:////{tmp}"
    for m in [m for m in list(sys.modules) if m.startswith("fantasy_coconut")]:
        del sys.modules[m]
    from sqlalchemy import text

    from fantasy_coconut import respaldo as r2
    from fantasy_coconut.db import Session as S2
    from fantasy_coconut.db import ddl

    print(f"2· creando una base VACÍA en {tmp}…")
    raiz = pathlib.Path(__file__).resolve().parents[1] / "db"
    esquemas = sorted(raiz.glob("*_sqlite.sql"))
    # Sin esta comprobación la prueba MIENTE: si `db/` no está montado en el contenedor,
    # el `glob` devuelve lista vacía, no se crea ni una tabla y el script sigue hasta
    # reventar mucho más abajo con un "no such table", que manda a buscar el fallo al
    # sitio equivocado.
    if not esquemas:
        print(f"no hay ningún *_sqlite.sql en {raiz}. ¿Falta montar db/ en el "
              f"contenedor?  -v \"$PWD/db:/app/db:ro\"")
        return 1
    async with S2() as s:
        for f in esquemas:
            # Los comentarios `--` fuera antes de partir por `;`: `002_vistas_sqlite.sql`
            # tiene punto y coma dentro de comentarios y trocearía las sentencias.
            limpio = re.sub(r"--[^\n]*", "", f.read_text(encoding="utf-8"))
            for sentencia in limpio.split(";"):
                if sentencia.strip():
                    await s.execute(text(ddl(sentencia)))
        await s.commit()
    # `db/*.sql` no trae todas las tablas: hay módulos que crean las suyas al arrancar.
    # Sin este paso la restauración muere con un "no such table", que es exactamente lo
    # que se encontraría una restauración de verdad.
    from fantasy_coconut.db import asegurar_tablas
    modulos = await asegurar_tablas()
    async with S2() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"))).scalar()
    print(f"   {n} tablas ({len(modulos)} módulos aportaron las suyas)")

    print("3· restaurando la copia…")
    metido = await r2.importar(copia, reemplazar=True)
    print("   " + " · ".join(f"{k}:{v}" for k, v in sorted(metido.items()) if v))

    print("4· exportando de la base restaurada y comparando…")
    vuelta = await r2.exportar(completa=False)
    h_vuelta = r2.huella(vuelta)

    fallos = []
    for ruta, original in copia.items():
        if ruta == "meta.json":
            continue
        copiado = vuelta.get(ruta, "")
        if copiado == original:
            continue
        a, b = original.splitlines(), copiado.splitlines()
        if len(a) != len(b):
            fallos.append(f"{ruta}: {len(a)} filas exportadas, {len(b)} al volver")
            continue
        # Mismo número de filas pero distinto contenido. Una copia que cuadra en cantidad
        # pasa una revisión por encima, así que se comparan fila a fila.
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            if x != y:
                fallos.append(f"{ruta}: la fila {i + 1} vuelve distinta\n"
                              f"       original: {x[:150]}\n"
                              f"       restaurada: {y[:150]}")
                break

    print()
    if fallos:
        print("LA RESTAURACIÓN NO ES FIEL:")
        for f in fallos:
            print("  ·", f)
        return 1
    print(f"viaje completo fiel · huella {h_origen} -> {h_vuelta}")
    print("   exportado de Postgres, restaurado en SQLite, y vuelve idéntico.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
