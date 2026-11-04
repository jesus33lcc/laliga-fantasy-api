"""`/trading/*`: comprar para vender, en modo PROPUESTA.

Nada de aquí compra solo. Detecta, propone y registra; ejecutar sigue pasando por
`/accion/pujar` y `/accion/vender`, con sus frenos.

Es deliberado, y el motivo está en `models.trading`: la señal de momentum es fuerte
(77,6 % de persistencia) pero solo el 10,6 % de los agentes libres está en racha, y el
retorno medido de la operación real se apoya en seis casos. Con esa muestra se mide, no
se automatiza: el coste de equivocarse no es solo el dinero, es tener una plaza ocupada y
la caja retenida el día que aparezca un clausulazo que valga la pena.

`GET /trading/balance` es el que decide el futuro de esto. Si tras unas semanas el retorno
real no se parece al +5,31 % del backtest, se apaga.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..models import trading
from .deps import api, bearer, cliente, protegido

log = logging.getLogger("coconut.api.trading")
router = APIRouter(prefix="/trading", tags=["trading"])

CMP = "/v1/competition/1"


async def _contexto(tok: str) -> tuple[list[dict], int, int]:
    """Mercado, caja libre y plazas libres. Tres lecturas que todo esto necesita."""
    mercado = await protegido(cliente().get(
        f"{CMP}/league/{settings.fantasy_league_id}/market", ttl=120, auth=tok))
    saldo = await protegido(api().saldo(tok, settings.fantasy_team_id))
    # La caja de verdad descuenta lo retenido en pujas vivas.
    caja = int(saldo.get("teamMoney") or 0) - int(saldo.get("teamInvestment") or 0)
    plantilla = await protegido(cliente().get(
        f"{CMP}/leagues/{settings.fantasy_league_id}/teams/{settings.fantasy_team_id}",
        ttl=120, auth=tok))
    # El tope de 24 CUENTA las pujas y ofertas vivas (`030.01.40`), y una puja abierta no
    # aparece en `players`: vive en `teamInvestment`. No hay forma de contarlas desde
    # aquí, así que esto es una COTA SUPERIOR de las plazas que quedan. Con pujas vivas
    # sale de más, nunca de menos.
    ocupadas = len(plantilla.get("players") or [])
    return (mercado if isinstance(mercado, list) else []), caja, 24 - ocupadas


@router.get("/oportunidades", summary="Qué comprar hoy para revender (propuesta)")
async def oportunidades():
    """Los agentes libres que cumplen el criterio, con el importe exacto a pujar.

    Devuelve también los descartados con su motivo: en un mercado donde el 56,7 % de
    lo que sale viene bajando, saber *por qué* no se compra nada es tan útil como la
    propuesta: evita volver a mirarlo a mano.
    """
    tok = await bearer()
    mercado, caja, plazas = await _contexto(tok)
    res = await trading.oportunidades(mercado, caja, plazas)
    res["caja_libre"] = caja
    return res


@router.get("/cartera", summary="Posiciones de trading abiertas")
async def cartera():
    ops = await trading.cartera()
    return {"abiertas": ops, "n": len(ops),
            "a_vender": [o for o in ops if o["toca_vender"]],
            "invertido": sum(o["precio_compra"] for o in ops)}


@router.get("/balance", summary="¿Esto funciona de verdad? Retorno REALIZADO")
async def balance():
    """El backtest dice +5,31 % con n=6. Esto dice lo que ha pasado de verdad."""
    return await trading.balance()


@router.post("/registrar", summary="Anota una compra hecha como operación de trading")
async def registrar(player_id: int, nombre: str, precio: int, valor: int,
                    momentum: float | None = None, nota: str = ""):
    """Se llama después de que `/accion/pujar` haya ganado la subasta.

    Separado a propósito de la puja: una puja puede perderse, y anotar la operación al
    pujar llenaría la tabla de posiciones que nunca existieron y falsearía el balance.
    """
    if precio <= 0 or valor <= 0:
        raise HTTPException(400, "precio y valor deben ser positivos")
    op = await trading.registrar(player_id, nombre, precio, valor, momentum, nota=nota)
    return {"ok": True, "id": op}


@router.post("/{op_id}/estado", summary="Cambia el estado de una operación")
async def estado(op_id: int, nuevo: str, precio_venta: int | None = None):
    if nuevo not in ("comprada", "listada", "cerrada", "abandonada"):
        raise HTTPException(400, "estado no válido")
    if nuevo == "cerrada" and not precio_venta:
        raise HTTPException(400, "para cerrar hace falta `precio_venta`")
    await trading.marcar(op_id, nuevo, precio_venta)
    return {"ok": True}
