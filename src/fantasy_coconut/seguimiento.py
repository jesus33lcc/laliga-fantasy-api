"""Tareas que VIGILAN hasta completarse, y sobreviven a que se apague el sistema.

En qué se diferencia de `/programar`

`/programar` es un disparo único: "ejecuta X a las 20:00". Un seguimiento es una
vigilancia: "vende a X y quédate mirando hasta que entre la oferta". Tres diferencias,
y cada una importa:

1. No termina al primer intento. Vender es cuestión de días, así que dar por hecho que
   en unas horas habrá oferta hunde cualquier plan que dependa de esa caja.
2. Al reiniciar se RETOMA, no se marca `perdida`. Abandonar lo vencido es correcto
   para un disparo puntual (una puja fuera de plazo es peor que ninguna puja) y es un
   error para una vigilancia: con el sistema apagado, la venta sigue ahí esperando.
3. Doble comprobación. El registro no basta: antes de actuar se pregunta a LaLiga si
   lo que dice sigue siendo verdad.

El modelo

Cada seguimiento es una máquina de estados diminuta. Su función devuelve una de tres:

    ("seguir",  motivo, segundos) duerme y vuelve a mirar
    ("hecho",   resultado) terminado
    ("fallido", motivo) no se pudo, y se dice por qué

Un solo bucle revisa todos los que tocan, en vez de una corrutina por tarea: al
arrancar solo hay que levantar el bucle, y no hay N tasks que rehidratar de uno en uno.

Las dos comprobaciones

El registro manda (es lo que sobrevive al apagón) pero siempre que se pueda se
contrasta contra la API en vivo antes de actuar. Es la misma regla que ya aplica el
piloto: entre decidir y ejecutar, un rival puede haber movido ficha. Si las dos fuentes
discrepan, gana la API y el registro se corrige.

Por qué esto en vez de más jobs

Un job nuevo por cada cosa que vigilar significa un cron más, una entrada más en el
scheduler y, tarde o temprano, otro contenedor. Esto es una tabla y un bucle: añadir
una vigilancia nueva es escribir una función y registrarla en `VIGILANTES`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import text

from . import entorno as _env
from .db import Session, ddl, json_param
from .tiempo import dentro_de

log = logging.getLogger("coconut.seguimiento")

DDL = """
CREATE TABLE IF NOT EXISTS seguimientos (
    id               text        PRIMARY KEY,
    tipo             text        NOT NULL,
    params           jsonb       NOT NULL DEFAULT '{}',
    estado           text        NOT NULL DEFAULT 'activo',
    fase             text        NOT NULL DEFAULT 'inicio',
    revisar_cada     integer     NOT NULL DEFAULT 300,
    proxima_revision timestamptz NOT NULL DEFAULT (now()),
    caduca           timestamptz,
    intentos         integer     NOT NULL DEFAULT 0,
    creada           timestamptz NOT NULL DEFAULT (now()),
    actualizada      timestamptz NOT NULL DEFAULT (now()),
    resultado        text,
    historial        jsonb       NOT NULL DEFAULT '[]',
    nota             text
)
"""
DDL_IDX = ("CREATE INDEX IF NOT EXISTS idx_seg_activos ON seguimientos "
           "(proxima_revision) WHERE estado = 'activo'")

# Cada cuánto despierta el bucle. No es la frecuencia de cada vigilancia (eso lo decide
# cada una con su `revisar_cada`), solo el pulso mínimo del reloj.
PULSO = int(_env.var("SEG_PULSO", "20"))

SEGUIR, HECHO, FALLIDO = "seguir", "hecho", "fallido"

# tipo -> función que da un paso. Registrar uno nuevo es añadir una entrada aquí.
VIGILANTES: dict[str, Callable[[dict], Awaitable[tuple]]] = {}


def vigilante(nombre: str):
    """Decorador para registrar un tipo de seguimiento."""
    def envolver(fn):
        VIGILANTES[nombre] = fn
        return fn
    return envolver


async def crear_tabla() -> None:
    async with Session() as s:
        await s.execute(text(ddl(DDL)))
        await s.execute(text(DDL_IDX))
        await s.commit()


async def crear(tipo: str, params: dict, *, revisar_cada: int = 300,
                caduca_en_horas: float | None = 72, nota: str = "") -> dict:
    """Registra una vigilancia. Se guarda antes de empezar a mirar, que es lo que
    permite retomarla si el sistema se cae en el segundo siguiente."""
    if tipo not in VIGILANTES:
        raise ValueError(f"tipo desconocido: {tipo!r}. Hay: {sorted(VIGILANTES)}")
    sid = uuid.uuid4().hex[:12]
    caduca = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=caduca_en_horas)
              if caduca_en_horas else None)
    async with Session() as s:
        await s.execute(text("""
            INSERT INTO seguimientos (id, tipo, params, revisar_cada, caduca, nota)
            VALUES (:i, :t, {P}, :r, :c, :n)""".replace("{P}", json_param(":p"))),
            {"i": sid, "t": tipo, "p": json.dumps(params, default=str),
             "r": revisar_cada, "c": caduca, "n": nota})
        await s.commit()
    log.info("seguimiento %s creado: %s cada %ds", sid, tipo, revisar_cada)
    return {"id": sid, "tipo": tipo, "estado": "activo", "revisar_cada": revisar_cada}


async def _anotar(sid: str, texto: str) -> None:
    """Añade una línea al historial. Es lo que permite reconstruir después por qué
    una tarea hizo lo que hizo, que es la mitad del valor de tener registro."""
    linea = {"ts": dt.datetime.now(dt.UTC).isoformat(), "texto": texto[:300]}
    async with Session() as s:
        # Se lee, se añade en Python y se vuelve a escribir, en vez de usar el
        # `||` de Postgres: ahí `||` concatena ARRAYS JSON, pero en SQLite concatena
        # TEXTO y dejaría "[…][…]", que ya no es JSON válido: y sin dar error, así que
        # el historial se iría corrompiendo en silencio. El historial son unas pocas
        # líneas por tarea, así que leerlo entero no cuesta nada.
        actual = (await s.execute(text(
            "SELECT historial FROM seguimientos WHERE id = :i"), {"i": sid})).scalar()
        if isinstance(actual, str):
            actual = json.loads(actual or "[]")
        historial = [*list(actual or []), linea]
        await s.execute(text(f"""
            UPDATE seguimientos
               SET historial = {json_param(':l')}, actualizada = now()
             WHERE id = :i"""), {"l": json.dumps(historial), "i": sid})
        await s.commit()


async def _cerrar(sid: str, estado: str, resultado: str) -> None:
    async with Session() as s:
        await s.execute(text("""
            UPDATE seguimientos SET estado=:e, resultado=:r, actualizada=now()
             WHERE id=:i"""), {"e": estado, "r": str(resultado)[:2000], "i": sid})
        await s.commit()
    await _anotar(sid, f"[{estado}] {resultado}")


async def _paso(fila: dict) -> None:
    """Un paso de una vigilancia. Nunca lanza: un fallo suelto no puede tumbar el bucle."""
    sid, tipo = fila["id"], fila["tipo"]
    fn = VIGILANTES.get(tipo)
    if fn is None:
        await _cerrar(sid, "fallido", f"no hay vigilante para el tipo {tipo!r}")
        return

    try:
        res = await fn(fila)
    except Exception as e:
        log.exception("seguimiento %s (%s) falló", sid, tipo)
        await _anotar(sid, f"error: {type(e).__name__}: {e}")
        # Un error transitorio (la API no responde) no debe matar la vigilancia: se
        # reintenta más tarde. Solo se abandona al llegar a `caduca`.
        async with Session() as s:
            await s.execute(text("""
                UPDATE seguimientos
                   SET intentos = intentos + 1,
                       proxima_revision = :proxima,
                       actualizada = now()
                 WHERE id = :i"""),
                {"i": sid,
                 "proxima": dentro_de(segundos=int(fila["revisar_cada"] or 60))})
            await s.commit()
        return

    accion = res[0]
    if accion == HECHO:
        await _cerrar(sid, "hecho", res[1])
        await _avisar(fila, " ", str(res[1]))
    elif accion == FALLIDO:
        await _cerrar(sid, "fallido", res[1])
        await _avisar(fila, " ", str(res[1]))
    else:
        motivo = res[1] if len(res) > 1 else ""
        espera = int(res[2]) if len(res) > 2 and res[2] else None
        async with Session() as s:
            await s.execute(text("""
                UPDATE seguimientos
                   SET intentos = intentos + 1, actualizada = now(),
                       revisar_cada = COALESCE(:nuevo, revisar_cada),
                       proxima_revision = :proxima
                 WHERE id = :i"""),
                {"i": sid, "nuevo": espera,
                 "proxima": dentro_de(
                     segundos=int(espera or fila["revisar_cada"] or 60))})
            await s.commit()
        if motivo:
            await _anotar(sid, motivo)


async def _avisar(fila: dict, icono: str, texto: str) -> None:
    from .alerts import enviar
    await enviar(f"{icono} <b>Seguimiento «{fila['tipo']}»</b>\n{texto}",
                 categoria="tarea", clave=f"seg:{fila['id']}", silencio=60)


async def _vencidos() -> list[dict]:
    async with Session() as s:
        filas = (await s.execute(text("""
            SELECT id, tipo, params, fase, intentos, revisar_cada, caduca, creada
              FROM seguimientos
             WHERE estado = 'activo' AND proxima_revision <= now()
             ORDER BY proxima_revision LIMIT 20"""))).all()
    return [{"id": r[0], "tipo": r[1], "params": r[2] or {}, "fase": r[3],
             "intentos": r[4], "revisar_cada": r[5], "caduca": r[6], "creada": r[7]}
            for r in filas]


async def _caducar() -> int:
    """Cierra los que han agotado su plazo. Insistir para siempre no es vigilar, es
    gastar peticiones contra un WAF que nos puede banear."""
    async with Session() as s:
        res = await s.execute(text("""
            UPDATE seguimientos SET estado='caducado', actualizada=now(),
                   resultado=COALESCE(resultado,'se agotó el plazo sin completarse')
             WHERE estado='activo' AND caduca IS NOT NULL AND caduca <= now()
             RETURNING id"""))
        n = len(res.all())
        await s.commit()
    if n:
        log.warning("%d seguimiento(s) caducados", n)
    return n


async def bucle() -> None:
    """El reloj. Un solo `asyncio.Task` para todas las vigilancias.

    Al arrancar no hace falta rehidratar nada: el estado vive entero en la tabla, así
    que basta con empezar a mirar. Eso es justo lo que hace que sobreviva a un apagón,
    una tarea a medias se retoma sola en el siguiente pulso.
    """
    await crear_tabla()
    async with Session() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM seguimientos WHERE estado='activo'"))).scalar()
    if n:
        log.warning("retomando %d seguimiento(s) que quedaron a medias", n)

    while True:
        try:
            await _caducar()
            for fila in await _vencidos():
                await _paso(fila)
        except Exception:
            log.exception("bucle de seguimientos: fallo en una vuelta")
        await asyncio.sleep(PULSO)


async def listar(estado: str | None = None, limite: int = 50) -> list[dict]:
    async with Session() as s:
        filas = (await s.execute(text("""
            SELECT id, tipo, estado, fase, intentos, creada, actualizada,
                   proxima_revision, caduca, resultado, nota, historial, params
              FROM seguimientos
             WHERE (CAST(:e AS text) IS NULL OR estado = CAST(:e AS text))
             ORDER BY (estado='activo') DESC, actualizada DESC
             LIMIT :n"""), {"e": estado, "n": limite})).all()
    return [{"id": r[0], "tipo": r[1], "estado": r[2], "fase": r[3], "intentos": r[4],
             "creada": str(r[5]), "actualizada": str(r[6]),
             "proxima_revision": str(r[7]), "caduca": str(r[8]) if r[8] else None,
             "resultado": r[9], "nota": r[10],
             "historial": (r[11] or [])[-8:], "params": r[12] or {}}
            for r in filas]


async def cancelar(sid: str) -> bool:
    async with Session() as s:
        res = await s.execute(text("""
            UPDATE seguimientos SET estado='cancelado', actualizada=now(),
                   resultado='cancelado a mano'
             WHERE id=:i AND estado='activo' RETURNING id"""), {"i": sid})
        ok = bool(res.first())
        await s.commit()
    return ok
