"""Ingesta del mercado diario.

Es la vía normal para tapar un hueco: el clausulazo es caro y ruidoso. Cada oferta trae
precio de salida, cuándo expira y cuántas pujas lleva, que es lo que dice si habrá pelea.

El campo `discr` parte el mercado en dos y hay que guardarlo. `marketPlayerLeague` es
agente libre: cuenta en `numberOfBids`, se compra pujando y la subasta resuelve sola.
`marketPlayerTeam` lo vende un manager: cuenta en `numberOfOffers`, que es otro campo, y
decide el vendedor. Leer `numberOfBids` para las dos deja las fichas de rival siempre a 0
pujas, o sea pareciendo chollos sin competencia.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text

from ..alerts import enviar
from ..clients.fantasy import FantasyAPI
from ..config import settings
from ..db import Session
from ..tiempo import hace

log = logging.getLogger("coconut.ingest.mercado")

_UPSERT = text("""
    INSERT INTO market_offers (market_id, player_id, sale_price, expires_at,
                               num_bids, num_offers, tipo, player_team_id, blindado,
                               seller, status, seen_at)
    VALUES (:mid, :pid, :precio, :expira, :pujas, :ofertas, :tipo, :ptid, :blindado,
            :vendedor, :estado, now())
    ON CONFLICT (market_id) DO UPDATE SET
        sale_price     = EXCLUDED.sale_price,
        expires_at     = EXCLUDED.expires_at,
        num_bids       = EXCLUDED.num_bids,
        num_offers     = EXCLUDED.num_offers,
        tipo           = EXCLUDED.tipo,
        player_team_id = EXCLUDED.player_team_id,
        blindado       = EXCLUDED.blindado,
        status         = EXCLUDED.status,
        seen_at        = now()
""")

# Lo que ya expiró no se borra: el histórico de precios de venta y de pujas es lo que
# permite estimar cuánto suele sobrepujar cada rival.
# El corte llega como parámetro en vez de `now() - interval '7 days'`: el `interval`
# de Postgres no existe en SQLite y no hay forma de escribirlo que valga en los dos.
_LIMPIAR = text("DELETE FROM market_offers WHERE expires_at < :corte")


def _num(v: Any) -> int | None:
    if v in (None, "", "null"):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _fecha(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


async def ingest_mercado(api: FantasyAPI, token: str,
                         liga: str | None = None) -> dict[str, int]:
    liga = liga or settings.fantasy_league_id
    datos = await api.mercado(token, liga)
    items = datos if isinstance(datos, list) else (datos.get("data") or [])

    guardadas = 0
    disputadas: list[dict[str, Any]] = []
    async with Session() as s:
        for it in items:
            pm = it.get("playerMaster") or {}
            pid = _num(pm.get("id"))
            mid = str(it.get("id") or "")
            if pid is None or not mid:
                continue
            await s.execute(_UPSERT, {
                "mid": mid,
                "pid": pid,
                "precio": _num(it.get("salePrice")) or 0,
                "expira": _fecha(it.get("expirationDate")),
                "pujas": _num(it.get("numberOfBids")) or 0,
                "ofertas": _num(it.get("numberOfOffers")) or 0,
                "tipo": it.get("discr"),
                # Solo viene en fichas de rival. `player_team_id` es lo que hay que
                # mandar para ofertar (el id del maestro da 400), y `blindado` marca a
                # los que rechazan el clausulazo con un 400 `030.01.62`.
                "ptid": (it.get("playerTeam") or {}).get("playerTeamId"),
                "blindado": (it.get("playerTeam") or {}).get("isShielded"),
                "vendedor": (it.get("sellerTeam") or {}).get("id") if isinstance(
                    it.get("sellerTeam"), dict) else None,
                "estado": it.get("status"),
            })
            guardadas += 1
            if it.get("bid"):
                disputadas.append(it)
        await s.execute(_LIMPIAR, {"corte": hace(dias=7)})
        await s.commit()

    avisados = await _avisar_pujas_disputadas(disputadas)
    log.info("mercado: %d ofertas", guardadas)
    return {"ofertas": guardadas, "pujas_disputadas": avisados}


async def _avisar_pujas_disputadas(fichas: list[dict[str, Any]]) -> int:
    """Avisa cuando alguien más puja por una ficha en la que ya tienes puja.

    La subasta es ciega: solo se sabe cuántas pujas hay, no de quién. Aun así cambia la
    decisión, porque se paga lo que se puja y una puja al valor exacto la bate cualquiera
    que ponga un euro más. `numberOfBids` incluye la tuya, así que hay rival a partir de 2.

    La `clave` lleva el número dentro para que el antirrebote haga de memoria: avisa al
    primer competidor y al tercero, no cada media hora por lo mismo.
    """
    n = 0
    for it in fichas:
        pujas = _num(it.get("numberOfBids")) or 0
        if pujas < 2:
            continue
        pm = it.get("playerMaster") or {}
        nombre = pm.get("nickname") or pm.get("name") or "?"
        mia = _num((it.get("bid") or {}).get("money")) or 0
        valor = _num(pm.get("marketValue")) or 0
        cierra = str(it.get("expirationDate") or "")[11:16]
        # Una puja por debajo del valor la rechaza la propia LaLiga (030.01.31), así que
        # ahí el aviso no es "hay competencia" sino "vas a perderla seguro".
        alerta = ("\n⚠️ Tu puja está POR DEBAJO del valor: súbela o la pierdes."  # sin-rastro-ok: lo lee el usuario en Telegram
                  if mia and valor and mia < valor else "")
        await enviar(
            f"⚔️ <b>Te disputan a {nombre}</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
            f"{pujas - 1} puja(s) rival(es) además de la tuya ({mia / 1e6:.2f}M · "
            f"vale {valor / 1e6:.2f}M). Se resuelve a las {cierra}.\n"
            f"La subasta es a primer precio: pagas lo que pujes.{alerta}",
            clave=f"puja-disputada:{it.get('id')}:{pujas}",
            categoria="puja_rival")
        n += 1
    return n
