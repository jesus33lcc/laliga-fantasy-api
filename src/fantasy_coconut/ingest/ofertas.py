"""Vigila las ofertas que recibo por los jugadores que tengo listados, y las cobra.

Listar no vende: el sistema manda su oferta cuando quiere y caduca en el siguiente ciclo
de mercado, así que sin nada mirando una ficha pasa días acumulando tiradas perdidas.

Por una ficha propia ofertan dos: LaLiga (`isFromMarket: true`), que paga entre el valor
y ±10 %, y otro manager. Solo se cobra sola la de LaLiga, y por delante van tres guardas,
numeradas donde se aplican: el origen, el precio mínimo y que quede equipo para alinear.
La política de cada jugador (`ventas.py`) decide DENTRO de lo que ellas ya permiten: las
dos primeras cortan antes de leerla, así que solo puede ser más restrictiva, nunca menos.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..alerts import EMERGENCIA, enviar
from ..clients.acciones import Acciones
from ..clients.via_api import cliente_lectura
from ..config import settings
from ..models.alineacion import margen_tras_vender
from ..politica import LIMITES, registrar
from ..ventas import decidir, politica_de

log = logging.getLogger("coconut.ofertas")

CMP = "/v1/competition/1"


async def _cobrar(acciones, market_id: str, offer_id: str, money: int):
    """El cobro va por `/accion/aceptar-oferta`, no contra LaLiga directamente.

    Así pasa por el lock de caja (un cobro y una puja a la vez leerían el mismo saldo),
    queda registrado en `decisions` y respeta la idempotencia.

    Si la API no responde se cobra por el camino directo: una oferta que expira sin
    cobrar es dinero perdido.
    """
    from ..clients.via_api import accion as accion_api
    from ..clients.via_api import activo as via_api
    if via_api():
        try:
            return await accion_api("aceptar-oferta",
                                    {"market_id": market_id, "offer_id": offer_id,
                                     "importe": money, "confirmar": True})
        except Exception as e:
            log.warning("no se pudo cobrar por /accion (%s); se hace directo",
                        type(e).__name__)
    return await acciones.aceptar_oferta(market_id, offer_id, money)


async def _jornada_congelada(token: str) -> bool:
    """Si la jornada en curso ya arrancó, o sea si el once ya está congelado.

    `isLive` de `week/current` lo dice, pero entre jornadas ese endpoint ya devuelve la
    siguiente, con `isLive` en falso. Los dos casos significan lo mismo aquí: no hay
    jornada rodando, así que lo que se venda no rompe ningún once que esté puntuando. Lo
    que no se puede es dar por congelada una jornada que aún no ha abierto.
    """
    try:
        from ..clients.fantasy import FantasyAPI
        async with cliente_lectura() as http:
            w = await FantasyAPI(http).jornada_actual()
        return bool(w.get("isLive"))
    except Exception as e:
        log.warning("no se pudo saber si la jornada está congelada (%s); se asume que no",
                    type(e).__name__)
        return False


async def revisar_ofertas(token: str, solo_avisar: bool = False) -> dict[str, int]:
    """Lee las ofertas por mis jugadores listados y cobra las que compensen.

    Con `solo_avisar` no cobra ninguna: las reporta y se aprueban a mano. Esperar a una
    oferta por encima del valor rinde +6,4 puntos porcentuales frente a aceptar la
    primera, medido sobre 8 ofertas.

    Lee el mercado de la API, no de la base: `market_offers` solo se refresca con
    `job_league` cada 30 min y puede decir `num_offers = 0` con una oferta ya encima. El
    mercado va cacheado 5 min, así que cuesta como mucho 1 petición por pasada.
    """
    yo = str(settings.fantasy_team_id)
    # Puntúa el once guardado en el `openingWeekDate`, así que con la jornada ya abierta
    # vender no puede romperla. Se pregunta una vez por pasada, no por jugador. Si no se
    # puede saber se asume que no: como mucho se retrasa una venta, y al revés se pierde
    # una jornada entera.
    congelada = await _jornada_congelada(token)
    fichas = []
    # Lecturas por el puente (un solo token bucket); el cobro va por /accion.
    async with cliente_lectura() as http:
        from ..clients.fantasy import FantasyAPI
        mercado = await FantasyAPI(http).mercado(token, settings.fantasy_league_id)
        for it in (mercado if isinstance(mercado, list) else mercado.get("data", [])):
            if str((it.get("sellerTeam") or {}).get("id") or "") != yo:
                continue
            if not int(it.get("numberOfOffers") or 0):
                continue
            pm = it.get("playerMaster") or {}
            fichas.append({
                "market_id": str(it.get("id")),
                "player_team_id": str((it.get("playerTeam") or {}).get("playerTeamId") or ""),
                "nickname": pm.get("nickname"),
                "valor": int(pm.get("marketValue") or 0),
            })

        # Sin ofertas no se sale todavía: el relistado tiene que correr igual, y es
        # cuando más falta hace. Si una ficha caducó no hay ofertas justamente porque ya
        # no está en el mercado.
        aceptadas = 0
        acciones = Acciones(http, token)
        for f in fichas:
            ptid = str(f["player_team_id"] or "")
            nombre = f["nickname"]
            if not ptid:
                log.warning("%s: ficha sin player_team_id, no se puede consultar", nombre)
                continue

            # Un 403 aquí sería lógica de negocio (ficha ajena), no un bloqueo del WAF:
            # se captura para no abrir el circuito de parada por una respuesta normal.
            try:
                d = await http.get(f"{CMP}/league/{settings.fantasy_league_id}"
                                   f"/playerTeam/{ptid}/offer", ttl=0, auth=token)
                ofertas = d if isinstance(d, list) else (d.get("data") or [])
            except Exception as e:
                log.warning("no se pudieron leer las ofertas de %s: %s", nombre, e)
                continue
            if not ofertas:
                continue

            valor = int(f["valor"] or 0)
            minimo = int(valor * LIMITES.min_precio_venta) if valor else 0

            # Guarda 1: solo se cobra sola la oferta de LaLiga, que va entre el valor y
            # ±10 % y no depende de nadie. La de otro manager la decide una persona: quien
            # ofrece elige a quién y por cuánto, así que aceptar sin mirar es aceptar
            # justo en los casos malos.
            del_sistema = [o for o in ofertas if o.get("isFromMarket")]
            if not del_sistema:
                alta = max(ofertas, key=lambda o: int(o.get("money") or 0))
                await enviar(
                    f"🤝 <b>Oferta de otro manager por {nombre}</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
                    f"{int(alta.get('money') or 0)/1e6:.2f}M (vale {valor/1e6:.2f}M)\n"
                    "No se cobra sola: decídelo tú.",
                    clave=f"oferta-rival:{nombre}:{alta.get('id')}", categoria="oferta")
                continue

            mejor = max(del_sistema, key=lambda o: int(o.get("money") or 0))
            money = int(mejor.get("money") or 0)
            de_quien = "el sistema"

            # Guarda 2: el suelo del 90 % del valor. Va antes de leer la política, así
            # que ningún `modo` puede bajar de aquí.
            if money < minimo:
                log.info("%s: mejor oferta %.2fM de %s, por debajo del mínimo %.2fM",
                         nombre, money / 1e6, de_quien, minimo / 1e6)
                await enviar(
                    f"💰 <b>Oferta baja por {nombre}</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
                    f"{money/1e6:.2f}M de {de_quien} (vale {valor/1e6:.2f}M)\n"
                    f"Por debajo del 90%, no se acepta sola.",
                    clave=f"oferta-baja:{nombre}:{money}", nivel=EMERGENCIA, categoria="oferta")
                continue

            # La política del jugador decide, y va antes de leer la plantilla para no
            # gastar una petición en algo que no se va a cobrar. `solo_avisar` la pisa:
            # es el freno general del proceso, por encima de cada caso.
            pol = await politica_de(ptid)
            if solo_avisar:
                pol = {**pol, "modo": "avisar"}
            que, porque = decidir(pol, importe=money, valor=valor,
                                  jornada_congelada=congelada)
            if que != "cobrar":
                prima = (money / valor - 1) * 100 if valor else 0
                icono = "💰" if que == "avisar" else "⏳"  # sin-rastro-ok: lo lee el usuario en Telegram
                await enviar(
                    f"{icono} <b>Oferta por {nombre}</b>\n"
                    f"{money/1e6:.2f}M de {de_quien} · vale {valor/1e6:.2f}M "
                    f"({prima:+.1f} %)\n{porque}.",
                    clave=f"oferta:{nombre}:{money}", categoria="oferta")
                log.info("%s: oferta de %.2fM (%+.1f %%) -> %s: %s",
                         nombre, money / 1e6, prima, que, porque)
                continue

            # Guarda 3: ¿queda equipo? Se relee con `ttl=0` en vez de fiarse de una
            # foto, porque entre el principio de la pasada y este punto puede haberse
            # cobrado otra ficha, y son las ventas encadenadas las que dejan sin once.
            # Un once de diez anula la jornada entera.
            plantilla = await http.get(
                f"{CMP}/leagues/{settings.fantasy_league_id}"
                f"/teams/{settings.fantasy_team_id}", ttl=0, auth=token)
            hay_margen, motivo = margen_tras_vender(plantilla, ptid)
            if not hay_margen:
                log.warning("%s: NO se cobra (%.2fM), %s", nombre, money / 1e6, motivo)
                await enviar(
                    f"🛑 <b>Oferta NO cobrada por {nombre}</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
                    f"{money/1e6:.2f}M de {de_quien}, pero {motivo}.\n"
                    "Ficha un recambio o véndelo a mano si lo asumes.",
                    clave=f"venta-sin-margen:{nombre}", categoria="oferta")
                continue

            log.warning("%s: aceptando %.2fM de %s (vale %.2fM)",
                        nombre, money / 1e6, de_quien, valor / 1e6)
            try:
                res = await _cobrar(acciones, str(f["market_id"]), str(mejor.get("id")), money)
                ok = bool(res.get("ok"))
                await registrar("venta", f"{nombre} (oferta de {de_quien})", money,
                                f"cobrada automáticamente; valía {valor/1e6:.2f}M",
                                ejecutada=ok, resultado=str(res)[:300])
                if ok:
                    aceptadas += 1
                    await enviar(f"💰 <b>Vendido {nombre}</b> por {money/1e6:.2f}M\n"  # sin-rastro-ok: lo lee el usuario en Telegram
                                 f"Oferta de {de_quien} · valía {valor/1e6:.2f}M",
                                 nivel=EMERGENCIA, categoria="oferta")
            except Exception as e:
                log.exception("fallo aceptando la oferta de %s", nombre)
                await enviar(f"❌ <b>Fallo cobrando {nombre}</b>\n{e}", nivel=EMERGENCIA,  # sin-rastro-ok: lo lee el usuario en Telegram
                             categoria="sistema")

    relistados = await _relistar_caducados(token)
    log.info("ofertas: %d fichas con oferta, %d cobradas, %d relistadas",
             len(fichas), aceptadas, relistados)
    return {"con_oferta": len(fichas), "aceptadas": aceptadas, "relistadas": relistados}


async def _relistar_caducados(token: str) -> int:
    """Vuelve a listar a quien tiene política con `relistar` y ya no está en el mercado.

    La ficha dura 3 días; si caduca sin vender se acaban las tiradas diarias y el jugador
    se queda en la plantilla sin que nadie se entere. Esperar una oferta buena solo es una
    estrategia si alguien vuelve a poner la ficha.

    Se relista al valor de mercado del día, no al precio de la vez anterior: el valor se
    recalcula cada madrugada y repetirlo es pedir un precio de hace tres días.
    """
    from ..ventas import listar as listar_politicas

    politicas = [p for p in await listar_politicas() if p.get("relistar")]
    if not politicas:
        return 0

    yo = str(settings.fantasy_team_id)
    async with cliente_lectura() as http:
        from ..clients.fantasy import FantasyAPI
        api = FantasyAPI(http)
        mercado = await api.mercado(token, settings.fantasy_league_id)
        listados = {str((it.get("playerTeam") or {}).get("playerTeamId") or "")
                    for it in (mercado if isinstance(mercado, list) else mercado.get("data", []))
                    if str((it.get("sellerTeam") or {}).get("id") or "") == yo}
        # "No está en el mercado" tiene dos causas y solo una se arregla relistando: la
        # ficha caducó (sigue en la plantilla) o se vendió (ya no está). Sin mirar la
        # plantilla, una venta buena se trataría como ficha caducada y se intentaría
        # listar a un jugador ajeno en cada pasada, para siempre.
        plantilla = await api.plantilla(token, settings.fantasy_league_id, yo, ttl=0)
        mios = {str(j.get("playerTeamId") or "") for j in (plantilla.get("players") or [])}

    from ..ventas import borrar as borrar_politica

    n = 0
    for p in politicas:
        ptid = str(p["player_team_id"])
        if ptid in listados:
            continue
        if mios and ptid not in mios:
            # Vendido, o te lo han quitado: la política ya no gobierna nada. Se borra en
            # vez de dejarla huérfana, porque borrarla por la API exige encontrar al
            # jugador, y un jugador que ya no está en ninguna plantilla no se encuentra.
            await borrar_politica(ptid)
            log.info("%s ya no está en la plantilla; se retira su política de venta",
                     p.get("nombre"))
            continue
        hasta = p.get("hasta")
        if hasta:
            from ..ventas import _aware
            if _aware(hasta) <= dt.datetime.now(dt.UTC):
                # El plazo venció: no se relista sola. Seguir listando a alguien cuyo
                # plazo pasó es tomar por él una decisión que ya no es automática.
                await enviar(f"⏳ <b>{p.get('nombre')} sigue sin venderse</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
                             "Se acabó el plazo de su política: decide si bajas el listón, "
                             "amplías el plazo o lo retiras.",
                             clave=f"venta-plazo:{ptid}", categoria="oferta")
                continue
        try:
            r = await _accion_api("vender", {"jugador": p.get("nombre")})
            if r.get("ok"):
                n += 1
                log.warning("relistado: %s", p.get("nombre"))
        except Exception as e:
            log.warning("no se pudo relistar a %s: %s", p.get("nombre"), e)
    return n


async def _accion_api(nombre: str, cuerpo: dict) -> dict:
    """Por `/accion/*`, que es donde viven los frenos, los locks y el candado del once."""
    from ..clients.via_api import accion
    return await accion(nombre, cuerpo)
