"""Locks entre procesos. Con advisory locks en Postgres, con una tabla en SQLite.

Por qué no vale un `asyncio.Lock`

Hay tres actores que pueden tocar la misma caja a la vez, y viven en procesos
distintos: el `worker` (con `job_ofertas` cada 30 min y el piloto cada 2 h), las tareas
programadas de la API, y las acciones que se lanzan a mano. Un lock en memoria solo
protege dentro de un proceso, así que aquí no sirve.

El escenario que esto evita no es rebuscado: `job_ofertas` cobra una venta a las 12:20
mientras una acción calcula una puja con el saldo leído a las 12:19. Las dos leen "tengo
X" y las dos creen que cabe. Sin un lock compartido, nada lo impide.

Cómo funciona en cada motor

Postgres: `pg_try_advisory_lock` toma un lock de sesión, visible por todos los
procesos que comparten la BD. Se mantiene la conexión abierta mientras dura la operación
y se libera al salir, pase lo que pase.

SQLite: no tiene advisory locks, así que se usa una tabla: insertar una fila con
el recurso como clave primaria es atómico, y si ya existe salta la restricción de unicidad.
Es el mismo contrato.

Con una diferencia que obliga a caducar los locks. El de Postgres lo suelta el
servidor si el proceso muere; la fila de SQLite se queda. Sin caducidad, un worker
que se cae a mitad de una compra dejaría la caja bloqueada *para siempre* y el sistema
callado. Por eso cada lock lleva `expira` y uno vencido se puede pisar: se prefiere un
lock que en el peor caso se suelta solo a los `CADUCIDAD` segundos, antes que un
bloqueo permanente que nadie va a ver hasta que haga falta operar.

Es `try` y no bloqueante a propósito: si otra operación está usando la caja, es mejor
devolver un 409 claro que dejar una petición HTTP colgada esperando.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging

from sqlalchemy import text

from .db import ES_SQLITE, Session, ddl
from .tiempo import ahora, dentro_de

log = logging.getLogger("coconut.locks")

# Clave fija para la caja. Cualquier operación que gaste o ingrese pasa por aquí.
CLAVE_CAJA = 918_273_645

# Cuánto vive un lock de SQLite sin renovar. Generoso respecto a lo que tarda cualquier
# operación real (segundos), y corto respecto a "hasta que alguien se dé cuenta".
CADUCIDAD_S = 300

_DDL = """
CREATE TABLE IF NOT EXISTS locks (
    recurso TEXT PRIMARY KEY,
    tomado  TIMESTAMPTZ NOT NULL,
    expira  TIMESTAMPTZ NOT NULL,
    quien   TEXT
)
"""


def _clave(texto: str) -> int:
    """Texto -> entero de 63 bits (lo que aceptan los advisory locks)."""
    h = hashlib.sha256(texto.encode()).digest()
    return int.from_bytes(h[:8], "big") % (2 ** 63)


async def _tomar_sqlite(s, recurso: str, descripcion: str) -> bool:
    """Inserta la fila del lock. Devuelve si se consiguió.

    El `WHERE excluded.tomado > locks.expira` del UPSERT es lo que permite recuperar un
    lock huérfano: solo se pisa una fila ya vencida, nunca una viva.
    """
    await s.execute(text(ddl(_DDL)))
    n = (await s.execute(text("""
        INSERT INTO locks (recurso, tomado, expira, quien)
        VALUES (:r, :ahora, :expira, :quien)
        ON CONFLICT (recurso) DO UPDATE
           SET tomado = excluded.tomado, expira = excluded.expira,
               quien  = excluded.quien
         WHERE excluded.tomado > locks.expira
        RETURNING 1"""), {
        "r": recurso, "ahora": ahora(), "expira": dentro_de(segundos=CADUCIDAD_S),
        "quien": descripcion[:100],
    })).scalar()
    await s.commit()
    return bool(n)


@contextlib.asynccontextmanager
async def lock(nombre: str | int, descripcion: str = ""):
    """Toma el lock. Lanza `BloqueoOcupado` si ya lo tiene otro."""
    recurso = str(nombre)
    async with Session() as s:
        if ES_SQLITE:
            tomado = await _tomar_sqlite(s, recurso, descripcion or recurso)
        else:
            tomado = (await s.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": nombre if isinstance(nombre, int) else _clave(recurso)})).scalar()
        if not tomado:
            # `api/programar.py` reconoce este 409 por cómo empieza el texto.
            raise BloqueoOcupado(
                f"otra operación está en curso sobre {descripcion or nombre}; "
                f"reinténtalo en unos segundos")
        log.debug("lock tomado: %s", descripcion or nombre)
        try:
            yield
        finally:
            # El `finally` libera pase lo que pase. Si aquí fallara (la BD no responde)
            # el lock de SQLite caduca solo a los CADUCIDAD_S; el de Postgres se suelta
            # al cerrarse la sesión.
            try:
                if ES_SQLITE:
                    await s.execute(text("DELETE FROM locks WHERE recurso = :r"),
                                    {"r": recurso})
                else:
                    await s.execute(
                        text("SELECT pg_advisory_unlock(:k)"),
                        {"k": nombre if isinstance(nombre, int) else _clave(recurso)})
                await s.commit()
            except Exception:
                log.warning("no se pudo liberar el lock %s; caducará solo",
                            descripcion or nombre, exc_info=True)
            log.debug("lock liberado: %s", descripcion or nombre)


class BloqueoOcupado(RuntimeError):
    """Otro proceso está operando sobre el mismo recurso."""


@contextlib.asynccontextmanager
async def lock_caja():
    """Serializa TODO lo que mueve dinero: pujas, clausulazos, ventas y cobros."""
    async with lock(CLAVE_CAJA, "la caja"):
        yield


@contextlib.asynccontextmanager
async def lock_jugador(player_team_id: str):
    """Evita operar dos veces sobre el mismo jugador a la vez.

    Por ejemplo blindarlo y venderlo en paralelo: el orden decide el resultado y ninguno
    de los dos procesos sabría cuál ganó.
    """
    async with lock(f"jugador:{player_team_id}", f"el jugador {player_team_id}"):
        yield
