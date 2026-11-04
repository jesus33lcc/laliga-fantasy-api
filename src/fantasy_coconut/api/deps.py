"""Recursos compartidos de la API: un solo cliente HTTP para todo el proceso.

Por qué esto importa más de lo que parece

Cada proceso que cree su propio `ClienteEducado` trae su propio token bucket y su propia
caché, así que el límite de 0,5 req/s no se aplica entre procesos: diez comandos
seguidos serían diez clientes arrancando con el cubo lleno.

Con un proceso de vida larga hay UN cliente, así que:
  · el rate limit es real y global,
  · la caché se comparte (el mercado se pide una vez cada 5 min, no una por consulta),
  · el contador de peticiones al origen cuenta de verdad lo que gastamos al día,
  · y el kill switch (`/data/STOP`) corta absolutamente todo.

El cliente se abre en el `lifespan` de FastAPI y se cierra al apagar.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import HTTPException

from ..clients.auth import SinCredenciales, tokens
from ..clients.fantasy import FantasyAPI
from ..clients.http import BloqueoDetectado, ClienteEducado, ParadaManual

log = logging.getLogger("coconut.api.deps")

_cliente: ClienteEducado | None = None


@asynccontextmanager
async def ciclo_vida(app):
    """Abre el cliente al arrancar y lo cierra al apagar."""
    global _cliente
    _cliente = ClienteEducado()
    await _cliente.__aenter__()
    log.info("cliente HTTP compartido abierto")
    # Las tareas programadas viven en Postgres: al arrancar se reprograman las pendientes
    # y se marcan como perdidas las que vencieron con el proceso caído.
    try:
        from .programar import rehidratar
        await rehidratar()
    except Exception:
        log.exception("no se pudieron rehidratar las tareas programadas")
    # La liga y el equipo, antes de que nada los lea. En el servidor vienen del `.env` y
    # esto no hace nada; en un móvil es lo único que los pone.
    #
    # Va en su propio `try`, con su propio aviso: compartir el `except` de las tareas
    # programadas taparía este fallo, y el login moriría luego con un 500 sin pista.
    try:
        from ..identidad import cargar as cargar_identidad
        await cargar_identidad()
    except Exception:
        log.exception("no se pudo cargar la identidad (liga/equipo)")

    try:
        from .sesion import crear_tablas
        await crear_tablas()
        from ..ajustes import crear_tabla
        await crear_tabla()
        from .bloqueo import crear_tabla as crear_bloqueos
        await crear_bloqueos()
        from ..models.trading import crear_tabla as crear_trading
        await crear_trading()
    except Exception:
        log.exception("no se pudieron crear las tablas de sesión, ajustes, bloqueo o "
                      "trading: el login y los candados no funcionarán")
    try:
        yield
    finally:
        await _cliente.__aexit__(None, None, None)
        _cliente = None
        log.info("cliente HTTP compartido cerrado")


def cliente() -> ClienteEducado:
    if _cliente is None:
        raise HTTPException(503, "el cliente HTTP aún no está listo")
    return _cliente


def api() -> FantasyAPI:
    return FantasyAPI(cliente())


async def bearer() -> str:
    try:
        return await tokens.bearer()
    except SinCredenciales as e:
        # 503 y no 500: es un estado recuperable, hace falta `scripts/login.py`.
        raise HTTPException(503, f"sesión caducada: {e}") from e


async def protegido(coro) -> Any:
    """Traduce los errores del transporte a códigos HTTP con sentido.

    Sin esto, un kill switch activo o una parada por WAF salen como un 500 genérico y hay
    que ir al log para enterarse de qué pasa.
    """
    try:
        return await coro
    except ParadaManual as e:
        raise HTTPException(503, f"parada manual activa: {e}") from e
    except BloqueoDetectado as e:
        raise HTTPException(503, f"BLOQUEO por WAF, sistema detenido: {e}") from e
