"""Registro de lo que hace el worker, para que Coconut pueda enseñarlo.

El problema

Los jobs viven dentro del proceso del `worker` (APScheduler los tiene en memoria) y la
API no los ve. Sin esto, saber si la ingesta va bien es `docker compose logs`: solo
desde el terminal, solo quien tenga acceso a la máquina, y nunca desde el móvil, que es
como se opera.

La solución, sin inventar un canal nuevo

Postgres ya es la fuente compartida entre los dos procesos. El worker apunta cada
ejecución aquí y la API lo lee. No hace falta abrir un puerto en el worker ni montar un
segundo servidor HTTP.

Y para el camino de vuelta (lanzar un job desde Coconut) lo mismo al revés: la API deja
una petición en la tabla y el bucle de seguimientos la recoge en el siguiente pulso.
Es una cola de un solo consumidor, que es justo lo que hace falta aquí.

`ejecutando` se marca antes de correr, no después. Si se marcara al terminar, un job
lento se recogería dos veces en el siguiente pulso y correría por duplicado.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import bindparam, text

from .db import Session, ddl, json_param
from .tiempo import hace, horas_entre

log = logging.getLogger("coconut.trabajos")

DDL = [
    """CREATE TABLE IF NOT EXISTS jobs_log (
        id        bigserial   PRIMARY KEY,
        nombre    text        NOT NULL,
        inicio    timestamptz NOT NULL DEFAULT now(),
        fin       timestamptz,
        ms        integer,
        ok        boolean,
        detalle   text,
        origen    text        NOT NULL DEFAULT 'cron'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_jobs_log_nombre ON jobs_log (nombre, inicio DESC)",
    """CREATE TABLE IF NOT EXISTS jobs_config (
        nombre      text        PRIMARY KEY,
        config      jsonb       NOT NULL DEFAULT '{}'::jsonb,
        actualizado timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS jobs_peticiones (
        id        text        PRIMARY KEY,
        nombre    text        NOT NULL,
        pedida    timestamptz NOT NULL DEFAULT now(),
        estado    text        NOT NULL DEFAULT 'pendiente',
        resultado text
    )""",
]

# Los jobs que se pueden lanzar a mano. Lista blanca a propósito: dejar que el navegador
# nombre cualquier función del worker sería ejecución remota arbitraria.
LANZABLES = {
    "players": "Maestro de jugadores y precios",
    "league": "Liga, plantillas, mercado y tablón",
    "onces": "Onces probables (futbolfantasy)",
    "radar": "Radar: protección, chollos, lastre",
    "emergencia": "Huecos críticos de plantilla",
    "alineacion": "Recalcular y guardar el once",
    "alineacion_guardia": "Guardia: revisar y reparar el once",
    "ofertas": "Ofertas recibidas por mis jugadores",
    "salud": "Autodiagnóstico e invariantes",
    "recompensa": "Cobrar la recompensa diaria",
    "mercado_avisos": "Revisar mis búsquedas del mercado",
    "precios_oficiales": "Lote del histórico oficial de precios",
    "resumen_diario": "Componer y mandar el resumen",
    "respaldo": "Subir la copia de seguridad",
}


async def crear_tablas() -> None:
    async with Session() as s:
        for d in DDL:
            await s.execute(text(ddl(d)))
        await s.commit()


async def registrar(nombre: str, ok: bool, ms: int, detalle: str = "",
                    origen: str = "cron") -> None:
    try:
        async with Session() as s:
            # `inicio` se calcula hacia atrás desde `ms`, no se deja al DEFAULT.
            #
            # Esto se apunta al TERMINAR, así que con el defecto `now()` en las dos,
            # `inicio` y `fin` quedan idénticas y la que dice "inicio" lleva la hora
            # de fin. La pantalla de Motor enseña esa columna como la hora a la
            # que corrió el proceso, y en el más lento (106 s) eso son casi dos minutos
            # de desfase. El corte se calcula en Python porque restar un intervalo se
            # escribe distinto en cada motor.
            await s.execute(text("""
                INSERT INTO jobs_log (nombre, inicio, fin, ms, ok, detalle, origen)
                VALUES (:n, :ini, now(), :ms, :ok, :d, :o)"""),
                {"n": nombre, "ini": hace(segundos=ms / 1000), "ms": ms, "ok": ok,
                 "d": str(detalle)[:1000], "o": origen})
            # El log no puede crecer sin fin en un aparato pequeño: 500 ejecuciones por
            # job son historia de sobra para diagnosticar.
            await s.execute(text("""
                DELETE FROM jobs_log WHERE nombre = :n AND id NOT IN (
                    SELECT id FROM jobs_log WHERE nombre = :n ORDER BY id DESC LIMIT 500)"""),
                {"n": nombre})
            await s.commit()
    except Exception:
        # Que falle el registro nunca puede tumbar el job que registra.
        log.warning("no se pudo registrar la ejecución de %s", nombre, exc_info=True)


def envolver(nombre: str, fn: Callable) -> Callable:
    """Envuelve un job para que se apunte solo. Conserva el comportamiento original."""
    async def envuelto(*a, origen: str = "cron", **k):
        t0 = time.monotonic()
        try:
            res = await fn(*a, **k)
            ms = int((time.monotonic() - t0) * 1000)
            await registrar(nombre, True, ms, json.dumps(res, default=str)[:900]
                            if res is not None else "", origen)
            return res
        except Exception as e:
            ms = int((time.monotonic() - t0) * 1000)
            await registrar(nombre, False, ms, f"{type(e).__name__}: {e}", origen)
            raise
    envuelto.__name__ = getattr(fn, "__name__", nombre)
    envuelto.__doc__ = fn.__doc__
    return envuelto


async def leer_config(nombre: str | None = None) -> dict:
    """La configuración efectiva: el defecto del catálogo, pisado por lo guardado.

    Resolverlo así, y no guardando el defecto en la tabla, permite cambiar el valor por
    defecto de un proceso sin pisar lo que ya se haya elegido a mano.
    """
    from .catalogo_jobs import CATALOGO, defecto
    try:
        async with Session() as s:
            filas = (await s.execute(text("SELECT nombre, config FROM jobs_config"))).all()
        guardado = {r[0]: (r[1] or {}) for r in filas}
    except Exception:
        log.warning("no se pudo leer jobs_config; se usan los valores por defecto",
                    exc_info=True)
        guardado = {}

    todo = {n: {**defecto(n), **guardado.get(n, {})} for n in CATALOGO}
    return todo if nombre is None else todo.get(nombre, defecto(nombre))


async def guardar_config(nombre: str, cambios: dict) -> dict:
    """Guarda solo lo que cambia. Un campo ausente no borra lo ya guardado."""
    from .catalogo_jobs import CATALOGO, NUMERO
    if nombre not in CATALOGO:
        raise ValueError(f"no hay ningún proceso llamado {nombre!r}")

    # Solo se aceptan las claves que el catálogo declara (más `activo`): así el navegador
    # no puede meter basura en la tabla ni un ajuste de un proceso en otro.
    opciones = {o["clave"]: o for o in CATALOGO[nombre].get("opciones", [])}
    validas = set(opciones) | {"activo"}
    limpio = {k: v for k, v in cambios.items() if k in validas and v is not None}
    if not limpio:
        raise ValueError(f"nada que guardar. Se acepta: {sorted(validas)}")

    # Un número fuera del rango del catálogo se rechaza aquí. Sin esto se guarda y
    # `_triggers()` lo recorta: `cada_minutos: 60` pasa a `*/59`, que dispara en el
    # minuto 0 y en el 59, dos veces por hora en vez de una.
    for k, v in limpio.items():
        o = opciones.get(k)
        if not o or o.get("tipo") != NUMERO:
            continue
        try:
            n = float(v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{k} tiene que ser un número, no {v!r}") from e
        lo, hi = o.get("min"), o.get("max")
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            raise ValueError(f"{k}={v!r} fuera de rango: va de {lo} a {hi}")

    actual = (await leer_config(nombre)) or {}
    nuevo = {**actual, **limpio}
    async with Session() as s:
        await s.execute(text("""
            INSERT INTO jobs_config (nombre, config) VALUES (:n, {C})
            ON CONFLICT (nombre) DO UPDATE
               SET config = {C}, actualizado = now()""".replace("{C}", json_param(":c"))),
            {"n": nombre, "c": json.dumps(nuevo)})
        await s.commit()
    log.warning("config de %s guardada: %s", nombre, limpio)
    return nuevo


async def pedir(nombre: str) -> dict:
    """Coconut pide que se lance un job. El worker lo recoge en ≤20 s."""
    if nombre not in LANZABLES:
        raise ValueError(f"«{nombre}» no se puede lanzar a mano. "
                         f"Se puede: {sorted(LANZABLES)}")
    pid = uuid.uuid4().hex[:12]
    async with Session() as s:
        await s.execute(text(
            "INSERT INTO jobs_peticiones (id, nombre) VALUES (:i, :n)"),
            {"i": pid, "n": nombre})
        await s.commit()
    log.info("job %s pedido a mano (%s)", nombre, pid)
    return {"id": pid, "nombre": nombre, "estado": "pendiente"}


async def recoger_peticiones(jobs: dict[str, Callable]) -> int:
    """Lo llama el bucle del worker. Ejecuta lo que Coconut haya pedido."""
    async with Session() as s:
        filas = (await s.execute(text(
            "SELECT id, nombre FROM jobs_peticiones WHERE estado='pendiente' "
            "ORDER BY pedida LIMIT 5"))).all()
        if not filas:
            return 0
        # Se marca antes de ejecutar: si se marcara al terminar, un job lento se
        # recogería otra vez en el siguiente pulso y correría dos veces.
        await s.execute(text(
            "UPDATE jobs_peticiones SET estado='ejecutando' WHERE id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": [r[0] for r in filas]})
        await s.commit()

    for pid, nombre in filas:
        fn = jobs.get(nombre)
        if fn is None:
            estado, salida = "fallida", f"no hay job llamado {nombre!r}"
        else:
            try:
                res = await fn(origen="manual")
                estado, salida = "hecha", json.dumps(res, default=str)[:800] if res else "ok"
            except Exception as e:
                estado, salida = "fallida", f"{type(e).__name__}: {e}"[:800]
        async with Session() as s:
            await s.execute(text(
                "UPDATE jobs_peticiones SET estado=:e, resultado=:r WHERE id=:i"),
                {"e": estado, "r": salida, "i": pid})
            await s.commit()
        log.info("job %s lanzado a mano: %s", nombre, estado)
    return len(filas)


async def resumen() -> list[dict]:
    """Una fila por job: cuándo corrió, cuánto tardó, si falló y cuántas veces."""
    # `(array_agg(x ORDER BY id DESC))[1]` ("el valor de la fila más reciente") es de
    # Postgres. La forma estándar es quedarse con esa fila aparte, con `row_number()`, y
    # juntarla con los recuentos. Sale más largo pero funciona en los dos motores y, de
    # paso, dice lo que hace: un bloque son los totales y el otro la última ejecución.
    async with Session() as s:
        filas = (await s.execute(text("""
            WITH recientes AS (
                SELECT * FROM jobs_log WHERE inicio > :desde
            ), ultima_vez AS (
                SELECT nombre, ok, ms, detalle FROM (
                    SELECT nombre, ok, ms, detalle,
                           row_number() OVER (PARTITION BY nombre ORDER BY id DESC) AS rn
                      FROM recientes)
                 WHERE rn = 1
            ), totales AS (
                SELECT nombre, max(inicio) AS ultima, count(*) AS veces,
                       count(*) FILTER (WHERE NOT ok) AS fallos
                  FROM recientes GROUP BY nombre
            )
            SELECT t.nombre, t.ultima AS "ultima [TIMESTAMPTZ]",
                   u.ok, u.ms, u.detalle, t.veces, t.fallos
              FROM totales t JOIN ultima_vez u ON u.nombre = t.nombre
             ORDER BY t.ultima DESC"""), {"desde": hace(dias=7)})).all()
    ahora = dt.datetime.now(dt.UTC)
    return [{"nombre": r[0], "ultima": str(r[1]),
             "hace_min": round(horas_entre(ahora, r[1]) * 60, 1) if r[1] else None,
             "ok": r[2], "ms": r[3], "detalle": (r[4] or "")[:220],
             "veces_7d": r[5], "fallos_7d": r[6],
             "lanzable": r[0] in LANZABLES,
             "descripcion": LANZABLES.get(r[0], "")} for r in filas]


async def historial(nombre: str, limite: int = 20) -> list[dict]:
    async with Session() as s:
        filas = (await s.execute(text("""
            SELECT inicio, ms, ok, detalle, origen FROM jobs_log
             WHERE nombre = :n ORDER BY id DESC LIMIT :l"""),
            {"n": nombre, "l": limite})).all()
    return [{"inicio": str(r[0]), "ms": r[1], "ok": r[2],
             "detalle": (r[3] or "")[:300], "origen": r[4]} for r in filas]


async def peticiones(limite: int = 20) -> list[dict[str, Any]]:
    async with Session() as s:
        filas = (await s.execute(text("""
            SELECT id, nombre, pedida, estado, resultado FROM jobs_peticiones
             ORDER BY pedida DESC LIMIT :l"""), {"l": limite})).all()
    return [{"id": r[0], "nombre": r[1], "pedida": str(r[2]), "estado": r[3],
             "resultado": (r[4] or "")[:300]} for r in filas]
