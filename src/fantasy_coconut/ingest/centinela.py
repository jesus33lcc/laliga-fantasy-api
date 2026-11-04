"""Vigilancia rápida de clausulazos contra tu equipo: una petición, y solo si hay riesgo.

`job_league` corre cada 2 h, y en ese hueco cabe que te roben un titular y que
`job_alineacion` guarde un once con él dentro: LaLiga lo descarta en silencio y quedan 10,
camino de jornada a cero. Bajar `job_league` a 10 min serían ~860 peticiones al día contra
un tope de 500.

Aquí se lee el tablón, que declara el clausulazo con ladrón, importe y hora, en vez de
deducirlo comparando plantillas. Si la ventana está cerrada no se gasta nada; si no se
sabe si lo está, se vigila igual, que una petición de más es barata.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import httpx
from sqlalchemy import text

from .. import alerts
from .. import entorno as _env
from ..clients.auth import tokens
from ..clients.via_api import cliente_lectura
from ..config import settings
from ..db import Session
from ..tipos_actividad import nombre as _nombre_tipo
from .league import INSERT_EVENT, _fecha, _num

log = logging.getLogger("coconut.centinela")

# El tipo 1 es el único evento del tablón con DOS usuarios: `user1Id` es quien paga la
# cláusula y `user2Id` a quién se la quitan.
CLAUSULAZO = 1

_YO = text("SELECT id FROM managers WHERE is_me LIMIT 1")

# No duplica avisos con `vigilante.py`: los dos insertan con `ON CONFLICT DO NOTHING` y
# el aviso lleva `clave`, que ya tiene su antirrebote.
_YA_VISTO = text("""
    SELECT 1 FROM league_events
    WHERE kind = 'clausulazo' AND ts = :ts AND player_id IS NOT DISTINCT FROM :player_id
    LIMIT 1
""")
_NOMBRES = text("""
    SELECT (SELECT name FROM players  WHERE id = :player_id)  AS jugador,
           (SELECT name FROM managers WHERE id = :ladron)     AS ladron
""")


async def _ventana_abierta() -> bool | None:
    """¿Se pueden dar clausulazos ahora mismo? `None` = no se ha podido averiguar.

    Se pregunta a la propia API en vez de recalcularlo: el endpoint ya cachea el
    calendario una hora, así que no cuesta ni una petición a LaLiga, y el cálculo vive en
    un solo sitio. Si la API no contesta, se devuelve `None` y el centinela vigila igual.
    """
    base = _env.var("API_INTERNA", "http://api:8000")
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.get(f"{base}/informe/ventana-clausulazo")
            r.raise_for_status()
            return r.json().get("abierta")
    except Exception as e:
        log.debug("no se pudo leer la ventana de clausulazo (%s): se vigila igual", e)
        return None


async def revisar() -> dict:
    """Mira la primera página del tablón y reacciona ante un clausulazo contra tu equipo."""
    abierta = await _ventana_abierta()
    if abierta is False:
        # Nadie puede clausular: cero peticiones. Es lo que abarata el tramo de 24 h
        # previas a cada jornada, no la jornada en sí (ahí el mercado está reanudado).
        return {"revisado": False, "motivo": "ventana de clausulazo cerrada"}

    async with Session() as s:
        yo = (await s.execute(_YO)).scalar_one_or_none()
    if yo is None:
        return {"revisado": False, "motivo": "no sé quién soy (managers.is_me vacío)"}

    async with cliente_lectura() as http:
        tok = await tokens.bearer()
        # ttl=0: aquí una copia cacheada es justo el fallo que se quiere evitar.
        pagina = await http.get(
            f"/v1/competition/1/leagues/{settings.fantasy_league_id}/activity/0",
            ttl=0, auth=tok)
    if isinstance(pagina, dict):
        pagina = pagina.get("data") or []

    robos = [e for e in (pagina or [])
             if int(e.get("activityTypeId") or 0) == CLAUSULAZO
             and str(e.get("user2Id") or "") == str(yo)]
    if not robos:
        return {"revisado": True, "robos": 0}

    nuevos = []
    async with Session() as s:
        for e in robos:
            cuando = _fecha(e.get("createdAt"))
            if not cuando:
                continue
            pid = _num(e.get("playerMasterId"))
            if (await s.execute(_YA_VISTO, {"ts": cuando, "player_id": pid})).first():
                continue  # ya lo vio el vigilante o una pasada anterior
            await s.execute(INSERT_EVENT, {
                "ts": cuando, "kind": _nombre_tipo(CLAUSULAZO),
                "manager_id": _num(e.get("user1Id")), "player_id": pid,
                "amount": _num(e.get("amount")),
                "raw": json.dumps(e, ensure_ascii=False),
            })
            nuevos.append({"ts": cuando, "player_id": pid,
                           "ladron": _num(e.get("user1Id")),
                           "amount": _num(e.get("amount"))})
        await s.commit()

    if not nuevos:
        return {"revisado": True, "robos": len(robos), "nuevos": 0}

    for r in nuevos:
        async with Session() as s:
            f = (await s.execute(_NOMBRES,
                                 {"player_id": r["player_id"], "ladron": r["ladron"]})).first()
        jugador = (f and f[0]) or f"jugador {r['player_id']}"
        ladron = (f and f[1]) or f"manager {r['ladron']}"
        minutos = (dt.datetime.now(dt.UTC) - r["ts"]).total_seconds() / 60
        log.error("CLAUSULAZO recibido: %s <- %s (hace %.0f min)",
                  jugador, ladron, minutos)
        await alerts.enviar(
            f"🚨 <b>TE HAN QUITADO A {jugador}</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
            f"   Se lo ha llevado <b>{ladron}</b> por "
            f"{(r['amount'] or 0) / 1e6:.2f}M\n"
            f"   Detectado {minutos:.0f} min después.",
            clave=f"robo:{r['player_id']}:{r['ts'].isoformat()}",
            nivel=alerts.EMERGENCIA, categoria="robo")

    # Reparar el once es lo urgente, no el aviso: un titular robado lo deja en 10, y un
    # once de 10 anula la jornada entera (tipo 7). Para cuando alguien lea el móvil, la
    # alineación puede haberse congelado ya.
    try:
        from ..guardia_once import revisar as revisar_once
        rep = await revisar_once(motivo="clausulazo detectado por el centinela")
        if rep.get("reparado"):
            log.warning("once reparado tras el robo: %s", rep.get("sustituciones"))
    except Exception:
        log.exception("no se pudo reparar el once tras el robo")

    return {"revisado": True, "robos": len(robos), "nuevos": len(nuevos)}
