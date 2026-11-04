"""Mercado, precios y lo que se paga de verdad.

Parte de `/informe/*`. Ver `comun.py` para lo compartido."""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Query
from sqlalchemy import text

from ...config import settings
from ...db import Session
from ..deps import api, bearer, cliente, protegido
from .comun import CMP, POS, _saldo_real

log = logging.getLogger("coconut.api.informe.mercado")
router = APIRouter()


@router.get("/mercado", summary="Agentes libres analizados (marca entrenadores)")
async def mercado(jornada: int | None = None):
    tok = await bearer()
    liga = settings.fantasy_league_id
    semana = await protegido(api().jornada_actual())
    n = jornada or int(semana.get("nextWeek") if semana.get("isLive")
                       else semana.get("weekNumber") or 1)
    partidos = {}
    for c in await protegido(api().calendario(n)):
        for a in (c["localId"], c["visitorId"]):
            partidos[int(a)] = c["matchDate"]

    fichas = await protegido(api().mercado(tok, liga))
    fichas = fichas if isinstance(fichas, list) else fichas.get("data", [])
    saldo = await protegido(api().saldo(tok, settings.fantasy_team_id))
    libre = int(saldo["teamMoney"]) - int(saldo["teamInvestment"])

    libres = []
    for it in fichas:
        if it.get("discr") != "marketPlayerLeague":
            continue
        pm = it.get("playerMaster", {})
        precio = int(it.get("salePrice") or 0)
        valor = int(pm.get("marketValue") or 0)
        pts = int(pm.get("points") or 0)
        wp = pm.get("weekPoints") or []
        jugadas = len(wp) if isinstance(wp, list) else 0
        es_entrenador = pm.get("positionId") == 5
        libres.append({
            "nombre": pm.get("nickname"), "posicion": POS.get(pm.get("positionId"), "?"),
            "precio": precio, "valor": valor,
            # La puja debe ser >= VALOR, no >= precio de salida.
            "puja_minima": valor,
            "puntos_temporada": pts, "jornadas_jugadas": jugadas,
            "media": round(pts / jugadas, 2) if jugadas else None,
            "puntos_temporada_anterior": pm.get("lastSeasonPoints"),
            "pts_por_millon": round(pts / (precio / 1e6), 2) if precio else None,
            "pujas": it.get("numberOfBids"), "expira": it.get("expirationDate"),
            "juega_jornada": partidos.get(int(pm.get("teamId") or 0)),
            "asequible": valor <= libre,
            "es_entrenador": es_entrenador,
            "aviso": "ENTRENADOR: no se puede alinear" if es_entrenador else None,
        })
    return {"jornada_referencia": n, "caja_libre": libre,
            "agentes_libres": sorted(libres, key=lambda x: -(x["pts_por_millon"] or 0))}


@router.get("/mercado-historico", summary="A qué precio se vende de verdad en esta liga")
async def mercado_historico():
    """`GET {CMP}/league/{lg}/market/history`: las últimas 30 operaciones de la liga.

    Por qué importa más de lo que parece. El valor de mercado lo fija LaLiga, pero lo
    que de verdad se paga lo fijan los managers de tu liga. Este endpoint es la única fuente que
    dice cuánto se paga por encima o por debajo del valor, y con eso se calibran dos
    cosas que de otra forma son suposición:

    - Cuánto pujar para ganar una subasta sin regalar dinero. La subasta es a primer
      precio, así que cada euro de sobrepuja sale del bolsillo.
    - Cuánto paga el sistema al comprarte un jugador: oscila alrededor del valor, no
      es un descuento fijo.

    La respuesta trae el importe y el jugador, pero no el valor de mercado de aquel día:
    se cruza con la serie propia (`market_values`). Sin ese cruce el porcentaje no se
    puede calcular.
    """
    tok = await bearer()
    try:
        ops = await protegido(cliente().get(
            f"{CMP}/league/{settings.fantasy_league_id}/market/history", ttl=300, auth=tok))
    except Exception as e:
        return {"error": f"no se pudo leer el histórico: {type(e).__name__}", "operaciones": []}
    if not isinstance(ops, list):
        return {"operaciones": []}

    # Valor del jugador el día de la operación, de la serie propia.
    async with Session() as s:
        serie = {(int(r[0]), r[1]): int(r[2]) for r in (await s.execute(text("""
            SELECT player_id, date(ts), CAST(round(AVG(value)) AS INTEGER)
            FROM market_values WHERE value > 0 GROUP BY 1, 2"""))).all()}

    filas, con_valor = [], []
    for o in ops:
        p = o.get("player") or {}
        pid = int(p.get("id") or 0)
        fecha = (o.get("date") or "")[:10]
        importe = int(o.get("money") or 0)
        valor = None
        try:
            valor = serie.get((pid, dt.date.fromisoformat(fecha)))
        except ValueError:
            pass
        pct = round((importe - valor) / valor * 100, 2) if valor else None
        fila = {"jugador": p.get("nickname") or p.get("name"),
                "posicion": POS.get(p.get("positionId"), "?"),
                "fecha": o.get("date"), "operacion": o.get("operation"),
                "importe": importe, "valor_ese_dia": valor, "sobre_valor_pct": pct}
        filas.append(fila)
        if pct is not None:
            con_valor.append(fila)

    def media(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    ventas = [f["sobre_valor_pct"] for f in con_valor if f["operacion"] == "sale"]
    compras = [f["sobre_valor_pct"] for f in con_valor if f["operacion"] != "sale"]
    return {
        "operaciones": filas,
        "n": len(filas),
        "con_valor_conocido": len(con_valor),
        "media_sobre_valor_ventas_pct": media(ventas),
        "media_sobre_valor_compras_pct": media(compras),
        "nota": ("Porcentaje sobre el valor de mercado de ESE día, cruzado con la serie "
                 "propia. Las operaciones anteriores al inicio de la serie no lo tienen."),
    }


@router.get("/momentum", summary="Qué sube, y qué de eso se puede comprar")
async def momentum(top: int = Query(25, le=100)):
    """La señal de precio que sí funciona, con su aviso al lado.

    El backtest walk-forward dice que un modelo de 8 variables no bate a la inercia,
    pero que comprar lo que más subió ayer rinde +2,5 %/día sobre la media y persiste
    varios días (validado sobre una temporada entera: gana 322 de 326 días).

    Esta pantalla enseña las dos cosas: el ranking general y, aparte, lo que de verdad
    se puede comprar, que casi nunca coinciden.
    """
    from ...models import precio as _p
    ranking = await _p.momentum(minimo_pct=0.5)
    oport = await _p.oportunidades(top=99)
    caja = await _saldo_real() or 0
    for c in oport["comprables"]:
        c["me_llega"] = c["valor"] <= caja
    return {
        "caja": caja,
        "suben": ranking[:top],
        "comprables": oport["comprables"],
        "de_rival_ignorados": oport["en_mercado_pero_de_rival"],
        "nota": oport["nota"],
    }


@router.get("/mis-ofertas", summary="Ofertas recibidas por mis jugadores")
async def mis_ofertas():
    """Ofertas recibidas por los jugadores propios que están listados en el mercado.

    Devuelve las ofertas encontradas y, en `fallos`, los jugadores que no se pudieron
    consultar.

    Solo pregunta por los listados, cruzando la plantilla con `GET /market`: cuesta
    una petición por ficha consultada y el token bucket sirve a 0,5 req/s, así que
    recorrer la plantilla entera bloquearía la carga de la interfaz decenas de segundos.
    Un jugador que no está en venta no puede tener ofertas. Con nada listado, cero
    peticiones.

    Un jugador sin ofertas responde 403, no 200 con lista vacía. Es el "esas
    ofertas no son tuyas" aplicado a algo que sí lo es, y aquí se traduce a "sin ofertas".
    """
    tok = await bearer()
    liga = settings.fantasy_league_id
    plantilla = await protegido(api().plantilla(tok, liga, settings.fantasy_team_id))

    # Los `playerTeamId` que tengo puestos a la venta. Una sola petición, ya cacheada.
    try:
        mercado = await protegido(cliente().get(f"{CMP}/league/{liga}/market",
                                                ttl=120, auth=tok))
        fichas = [m for m in (mercado if isinstance(mercado, list) else [])
                  if m.get("discr") == "marketPlayerTeam"]
        # Dentro de `playerTeam` la clave es `playerTeamId`, no `id`. Con `id` el
        # conjunto sale `{""}`, no casa ninguna ficha y el endpoint devuelve cero
        # ofertas con jugadores propios listados.
        en_venta = {str(m.get("playerTeamId") or
                        (m.get("playerTeam") or {}).get("playerTeamId") or "")
                    for m in fichas} - {""}
        if fichas and not en_venta:
            # Hay fichas en venta y de ninguna sale el id: la clave ha vuelto a cambiar.
            # Un filtro que no encuentra nada que filtrar no se distingue de uno roto, así
            # que se pregunta por toda la plantilla, igual que el `except` de abajo.
            log.warning("ninguna de las %d fichas en venta trae playerTeamId: "
                        "se pregunta por toda la plantilla", len(fichas))
            en_venta = None
    except Exception as e:
        log.warning("sin mercado para filtrar mis ofertas (%s): se pregunta por todos", e)
        # `None` desactiva el filtro de abajo: se consultan las ofertas de toda la
        # plantilla, una petición por ficha.
        en_venta = None

    out, fallos = [], []
    for p in (plantilla.get("players") or []):
        ptid = str(p.get("playerTeamId") or "")
        pm = p.get("playerMaster") or {}
        if not ptid:
            continue
        if en_venta is not None and ptid not in en_venta:
            continue
        try:
            ofertas = await protegido(cliente().get(
                f"{CMP}/league/{liga}/playerTeam/{ptid}/offer", ttl=60, auth=tok))
        except Exception as e:
            # Solo el 403 significa "sin ofertas". Un `except Exception: continue` se
            # tragaría TODO (un timeout, un 500, el circuito abierto por WAF) y lo
            # presentaría como "este jugador no tiene ofertas": la pantalla diría "0
            # ofertas" con el sistema roto. Lo demás se recoge en `fallos`.
            codigo = getattr(getattr(e, "response", None), "status_code", None)
            if codigo == 403:
                continue                     # el caso normal y esperado
            fallos.append({"jugador": pm.get("nickname"), "ptid": ptid,
                           "error": f"{codigo or type(e).__name__}: {str(e)[:120]}"})
            log.warning("no se pudieron leer las ofertas de %s: %s", pm.get("nickname"), e)
            continue
        for o in (ofertas if isinstance(ofertas, list) else []):
            out.append({
                "player_team_id": ptid, "player_id": int(pm.get("id") or 0),
                "jugador": pm.get("nickname"),
                "posicion": POS.get(pm.get("positionId"), "?"),
                "valor": int(pm.get("marketValue") or 0),
                "oferta_id": str(o.get("id") or ""),
                "market_id": str(o.get("marketId") or o.get("market", {}).get("id") or ""),
                "importe": int(o.get("money") or 0),
                "del_sistema": bool(o.get("isFromMarket")),
                "de": ((o.get("team") or {}).get("manager") or {}).get("managerName"),
                "expira": o.get("expirationDate"),
            })
    out.sort(key=lambda o: -o["importe"])
    # `fallos` se expone: si algo no se pudo leer, la pantalla debe poder decir
    # "no lo sé" en vez de "no hay ofertas", que no es lo mismo.
    return {"ofertas": out, "n": len(out),
            **({"no_leidos": fallos} if fallos else {})}


@router.get("/backtest", summary="¿Acierta el sistema? Predicciones liquidadas")
async def backtest():
    """La nota de cada modelo sobre lo que ya se puede comprobar.

    Con 38 jornadas y cuatro jugadores la varianza es enorme: sin esto, al final de
    temporada gana alguien y no se sabe si fue el mejor sistema o el más afortunado.

    `n` importa tanto como el acierto. Un 100 % con n=3 no dice nada, y un 100 % con
    n grande suele ser un artefacto de la métrica, no un éxito: toda métrica necesita una
    referencia tonta al lado para saber si mide algo.
    """
    from ...models import backtest as _b
    await _b.liquidar_precio()
    notas = await _b.nota()
    async with Session() as s:
        pend = (await s.execute(text(
            "SELECT count(*) FROM predictions WHERE actual IS NULL"))).scalar()
    return {"modelos": notas, "pendientes_de_liquidar": pend or 0,
            "nota": ("«momentum» son predicciones hechas a ciegas; «momentum_backtest» es "
                     "la evaluación retroactiva sobre la serie, y por eso no se mezclan.")}
