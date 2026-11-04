"""Diagnóstico de la pila de Coconut dentro del teléfono.

Lo llama Kotlin desde la prueba instrumentada. Comprueba una por una las piezas de las que
depende que la app corra sin servidor, y cada paso falla de forma distinguible para que el
resultado diga *qué* no funciona y no solo *que* no funciona:

    1. Que los módulos importen (es donde sale una dependencia nativa que falta).
    2. Que SQLAlchemy hable con SQLite en modo async y sobre un fichero del
       almacenamiento de la app.
    3. Que FastAPI responda, valide y genere su OpenAPI con pydantic 1.
    4. Qué versión de SQLite hay: el proyecto usa `->>`, `FILTER` y funciones de ventana,
       y eso exige 3.38+.
    5. Que importe el código real del paquete, capa por capa.

El punto 4 es el que puede invalidar el enfoque sin avisar: la libsqlite del sistema puede
ser bastante anterior a la del servidor en un móvil viejo.
"""

from __future__ import annotations

import json
import platform
import sqlite3
import sys
import traceback


def _paso(nombre, fn):
    try:
        return {"paso": nombre, "ok": True, "detalle": fn()}
    except Exception as e:                                           # noqa: BLE001
        return {"paso": nombre, "ok": False,
                "detalle": f"{type(e).__name__}: {e}",
                "traza": traceback.format_exc()[-600:]}


def _importaciones():
    import apscheduler
    import fastapi
    import httpx
    import pydantic
    import sqlalchemy
    mods = {"fastapi": fastapi.__version__, "pydantic": pydantic.VERSION,
            "sqlalchemy": sqlalchemy.__version__, "httpx": httpx.__version__,
            "apscheduler": getattr(apscheduler, "__version__", "?")}
    # greenlet es el ÚNICO nativo de la lista: si falta, SQLAlchemy async no arranca.
    import greenlet
    mods["greenlet"] = greenlet.__version__
    return mods


def _sqlite():
    """La versión de SQLite disponible, y si soporta lo que el proyecto usa."""
    con = sqlite3.connect(":memory:")
    soporta = {}
    for etiqueta, sql in (
        ("operador ->>", "SELECT '{\"a\":1}'->>'a'"),
        ("FILTER (WHERE)", "SELECT count(*) FILTER (WHERE 1=1)"),
        ("row_number()", "SELECT row_number() OVER (ORDER BY 1)"),
        ("ON CONFLICT", "CREATE TABLE z(i INTEGER PRIMARY KEY); "
                        "INSERT INTO z VALUES(1) ON CONFLICT DO NOTHING"),
        ("RETURNING", "SELECT 1"),
    ):
        try:
            con.executescript(sql) if ";" in sql else con.execute(sql)
            soporta[etiqueta] = True
        except Exception as e:                                       # noqa: BLE001
            soporta[etiqueta] = f"NO: {e}"
    return {"version": sqlite3.sqlite_version, "soporta": soporta}


def _sqlalchemy_async(ruta_db: str):
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def trabajo():
        e = create_async_engine(f"sqlite+aiosqlite:///{ruta_db}")
        try:
            async with e.begin() as c:
                await c.execute(text("DROP TABLE IF EXISTS prueba"))
                await c.execute(text(
                    "CREATE TABLE prueba (id INTEGER PRIMARY KEY, raw TEXT, v INTEGER)"))
                await c.execute(text(
                    "INSERT INTO prueba VALUES (1, :r, 100) "
                    "ON CONFLICT(id) DO UPDATE SET v = excluded.v"),
                    {"r": json.dumps({"marketValue": 67128884})})
                fila = (await c.execute(text(
                    "SELECT raw->>'marketValue' AS valor,"
                    "       count(*) FILTER (WHERE v > 0) AS n,"
                    "       row_number() OVER (ORDER BY id) AS rn"
                    "  FROM prueba GROUP BY id"))).one()
                return {"valor": fila[0], "n": fila[1], "rn": fila[2]}
        finally:
            await e.dispose()

    return asyncio.new_event_loop().run_until_complete(trabajo())


# El modelo va A NIVEL DE MÓDULO, y con pydantic 1 no es indiferente: este fichero lleva
# `from __future__ import annotations`, así que la anotación es una cadena y pydantic 1 la
# resuelve contra los globales del módulo. Un modelo definido dentro de una función da
# `NameError`. Pydantic 2 es más permisivo, así que el fallo solo aparece en el móvil.
from pydantic import BaseModel, Field  # noqa: E402


class Cambios(BaseModel):
    valor: int = Field(..., ge=0)


def _fastapi():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI(title="Coconut (prueba)")

    @app.get("/estado")
    def estado():
        return {"ok": True}

    @app.post("/ajustes")
    def ajustes(c: Cambios):
        return {"recibido": c.valor}

    cli = TestClient(app)
    return {
        "GET /estado": cli.get("/estado").status_code,
        "POST válido": cli.post("/ajustes", json={"valor": 7}).status_code,
        "POST inválido (debe ser 422)": cli.post("/ajustes", json={"valor": -1}).status_code,
        "/openapi.json": cli.get("/openapi.json").status_code,
    }


def _coconut_importa(ruta_db: str, dir_web: str | None = None):
    """Importa el código real de Coconut dentro del móvil.

    Los pasos anteriores prueban la pila (FastAPI, SQLAlchemy, SQLite). Este prueba el
    proyecto: sus módulos, con sus `text(...)` y sus modelos de pydantic.

    Se importa por capas y se anota cuál falla, porque el orden dice mucho:
      · `config` es el único fichero que nota la diferencia entre pydantic 1 y 2;
      · `db` decide el motor y registra las funciones de SQLite;
      · los `models/*` son el SQL;
      · `api.main` monta la aplicación entera, que es lo que serviría uvicorn.
    """
    import os
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{ruta_db}"
    # Sin esto, `config.py` buscaría un `.env` que en el móvil no existe.
    os.environ.setdefault("COCONUT_MODO", "sombra")
    # Antes de importar: `api/main.py` resuelve la ruta de la interfaz al importarse.
    if dir_web:
        os.environ["COCONUT_WEB"] = dir_web

    capas = [
        ("config", "fantasy_coconut.config"),
        ("db + SQLite", "fantasy_coconut.db"),
        ("tiempo", "fantasy_coconut.tiempo"),
        ("clientes HTTP", "fantasy_coconut.clients.fantasy"),
        ("modelos", "fantasy_coconut.models.alineacion"),
        ("política y locks", "fantasy_coconut.politica"),
        ("ingesta", "fantasy_coconut.ingest.players"),
        ("API completa", "fantasy_coconut.api.main"),
    ]
    import importlib
    salida = {}
    for etiqueta, modulo in capas:
        try:
            importlib.import_module(modulo)
            salida[etiqueta] = "ok"
        except Exception as e:                                       # noqa: BLE001
            salida[etiqueta] = f"{type(e).__name__}: {e}"
            # Se sigue: interesa saber CUÁNTAS capas fallan, no solo la primera.
    from fantasy_coconut.config import PYDANTIC_V2
    salida["pydantic_v2"] = PYDANTIC_V2
    return salida


def diagnostico(ruta_db: str = ":memory:", dir_web: str | None = None) -> str:
    """Devuelve un JSON con el resultado. Lo llama Kotlin y lo pinta en pantalla."""
    pasos = [
        _paso("1· importar la pila", _importaciones),
        _paso("2· SQLite del sistema", _sqlite),
        _paso("3· SQLAlchemy async sobre SQLite", lambda: _sqlalchemy_async(ruta_db)),
        _paso("4· FastAPI + pydantic 1", _fastapi),
        _paso("5· importar el Coconut REAL", lambda: _coconut_importa(ruta_db, dir_web)),
    ]
    return json.dumps({
        "python": sys.version.split()[0],
        "plataforma": platform.machine(),
        # `todo_ok` mira SOLO los cuatro primeros, que son la pila. El quinto (importar el
        # Coconut real) es informativo: una capa suya puede fallar por razones ajenas a que
        # el móvil aguante la pila, y hacerlo bloqueante deja la prueba en rojo permanente.
        "todo_ok": all(p["ok"] for p in pasos[:4]),
        "pasos": pasos,
    }, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    print(diagnostico())
