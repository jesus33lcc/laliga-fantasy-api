"""Los tipos de seguimiento concretos. Cada uno es una función y una línea de registro.

Todos siguen la misma regla: el registro dice en qué fase está, pero antes de actuar se
comprueba contra la API en vivo. Si las dos discrepan, gana la API. Es lo mismo que ya
hace el piloto antes de ejecutar, y por el mismo motivo: entre una comprobación y la
siguiente pueden pasar horas, y en ese hueco un rival mueve ficha.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import text

from .clients.acciones import cuerpo_de
from .db import Session
from .seguimiento import FALLIDO, HECHO, SEGUIR, vigilante

log = logging.getLogger("coconut.vigilantes")

CMP = "/v1/competition/1"


async def _api():
    from .clients.auth import tokens
    from .clients.via_api import cliente_lectura
    return cliente_lectura(), await tokens.bearer()


async def _accion(nombre: str, cuerpo: dict | None, params: dict | None = None):
    """Ejecuta por `/accion/*`, que es donde viven los frenos y los locks."""
    from .clients.via_api import accion
    return await accion(nombre, cuerpo, params=params)


def _deja_equipo(plantilla: dict, ptid_que_sale: str) -> tuple[bool, str]:
    """Delega en `models.alineacion`: la regla vive en un solo sitio para las dos vías."""
    from .models.alineacion import margen_tras_vender
    return margen_tras_vender(plantilla, ptid_que_sale)


#  1. VENTA: vigila hasta que entra la oferta y la cobra


@vigilante("venta")
async def venta(seg: dict) -> tuple:
    """Vigila a un jugador ya listado hasta que entra una oferta del sistema, y la acepta.

    Se lanza una vez por jugador y sigue hasta tres días, que es lo que dura la ficha de
    venta. La oferta del sistema (`isFromMarket`, entre el valor y ±10 %) puede tardar
    días en llegar, de ahí la vigilancia en vez de una comprobación puntual.

    Tres cosas que no acepta, cada una por un motivo distinto:

    - una oferta de otro manager: a quién se le vende es una decisión, no un cobro de
      trámite, así que se avisa en vez de cobrarla;
    - una por debajo del mínimo fijado al crear la vigilancia;
    - una que dejaría al equipo sin margen para alinear, aunque el precio sea bueno.
    """
    ptid = str(seg["params"].get("player_team_id") or "")
    nombre = seg["params"].get("nombre") or ptid
    if not ptid:
        return (FALLIDO, "falta el player_team_id")

    cli, tok = await _api()
    async with cli as http:
        # ---------- comprobación en vivo nº 1: ¿sigue siendo mío? ----------
        from .config import settings
        plantilla = await http.get(
            f"{CMP}/leagues/{settings.fantasy_league_id}/teams/{settings.fantasy_team_id}",
            ttl=30, auth=tok)
        sigue = any(str(p.get("playerTeamId")) == ptid
                    for p in (plantilla.get("players") or []))
        if not sigue:
            # Ya no está: o se vendió por otra vía, o se lo llevaron.
            return (HECHO, f"{nombre} ya no está en la plantilla: la venta se completó "
                           "o el jugador salió por otra vía")

        # ---------- comprobación en vivo nº 2: ¿hay ofertas? ----------
        try:
            ofertas = await http.get(
                f"{CMP}/league/{settings.fantasy_league_id}/playerTeam/{ptid}/offer",
                ttl=15, auth=tok)
        except Exception as e:
            return (SEGUIR, f"no se pudieron leer las ofertas ({type(e).__name__})", None)

    ofertas = ofertas if isinstance(ofertas, list) else []
    if not ofertas:
        # Una venta tarda días, no segundos. A 30 s fijos salen 3.700
        # comprobaciones en dos días: ruido puro y peticiones gastadas en algo que no
        # puede cambiar tan rápido. El ritmo se relaja solo: empieza fino por si la oferta
        # llega pronto y se espacia hasta 30 min cuando queda claro que no.
        espera = min(1800, 60 * (1 + int(seg.get("intentos") or 0) // 10))
        return (SEGUIR, f"{nombre}: sin ofertas todavía", espera)

    del_sistema = [o for o in ofertas if o.get("isFromMarket")]
    if not del_sistema:
        return (SEGUIR,
                f"{nombre}: hay {len(ofertas)} oferta(s), pero de otros managers y no del "
                "sistema. Esas no se cobran solas: decídelo tú", None)

    mejor = max(del_sistema, key=lambda o: int(o.get("money") or 0))
    importe = int(mejor.get("money") or 0)
    minimo = int(seg["params"].get("minimo") or 0)
    if minimo and importe < minimo:
        return (SEGUIR, f"{nombre}: la oferta ({importe:,} €) no llega al mínimo "
                        f"({minimo:,} €)".replace(",", "."), None)

    # ---------- comprobación en vivo nº 3: ¿queda equipo después de vender? ----------
    #
    # Las vigilancias no se ven entre sí. Con tres jugadores listados hay tres bucles
    # independientes, y nada impide que acepten el mismo día: cada uno mira su oferta y
    # ninguno mira la plantilla. Vender de más no se nota hasta que hay que alinear, y
    # entonces ya no tiene arreglo, porque un once de diez anula la jornada entera.
    #
    # Se mide sobre la plantilla que ya se pidió arriba, así que no cuesta ni una
    # petición. Y se exige margen para UNA BAJA, no solo que el once exista: quedarse con
    # los once justos es quedarse a merced de la siguiente lesión.
    hueco, motivo = _deja_equipo(plantilla, ptid)
    if not hueco:
        return (SEGUIR, f"{nombre}: la oferta ({importe:,} €) NO se acepta, {motivo}. "
                        "Se sigue vigilando por si entra un refuerzo".replace(",", "."),
                None)

    # `/accion/aceptar-oferta` exige `market_id`, `offer_id` e `importe`. Con solo el
    # nombre responde 422, la excepción sube y el seguimiento reintenta hasta caducar a
    # las 72 h: la venta no se cobraba nunca. Los tres salen de la oferta ya elegida, con
    # el mismo cruce que hace `/informe/mis-ofertas`.
    market_id = str(mejor.get("marketId") or (mejor.get("market") or {}).get("id") or "")
    offer_id = str(mejor.get("id") or "")
    if not market_id or not offer_id:
        return (SEGUIR, f"{nombre}: la oferta de {importe:,} € no trae market_id ni "
                        "offer_id, y sin ellos no se puede aceptar".replace(",", "."),
                None)

    res = await _accion("aceptar-oferta", {"market_id": market_id, "offer_id": offer_id,
                                           "importe": importe, "confirmar": True})
    return (HECHO, f"{nombre} vendido por {importe:,} €".replace(",", ".") +
                   f" · {str(res)[:120]}")


#  2. PUJA: comprueba si la ganaste, en vez de que te enteres al mirar el saldo


@vigilante("puja")
async def puja(seg: dict) -> tuple:
    """Vigila una puja por un agente libre hasta que la subasta resuelve (13:48).

    Sin esto el resultado se ve al día siguiente, revisando la plantilla a ojo: si la puja
    se pierde, el hueco que iba a tapar sigue abierto y nadie lo sabe hasta el siguiente
    ciclo.
    """
    pid = seg["params"].get("player_id")
    nombre = seg["params"].get("nombre") or str(pid)
    if not pid:
        return (FALLIDO, "falta el player_id")

    from .config import settings
    cli, tok = await _api()
    async with cli as http:
        plantilla = await http.get(
            f"{CMP}/leagues/{settings.fantasy_league_id}/teams/{settings.fantasy_team_id}",
            ttl=30, auth=tok)
        mio = any(str((p.get("playerMaster") or {}).get("id")) == str(pid)
                  for p in (plantilla.get("players") or []))
        if mio:
            return (HECHO, f"🎉 Subasta GANADA: {nombre} ya está en tu plantilla")  # sin-rastro-ok: lo lee el usuario en Telegram

        # ¿Sigue viva la ficha en el mercado? Si desapareció y no es tuyo, la perdiste.
        mercado = await http.get(
            f"{CMP}/league/{settings.fantasy_league_id}/market", ttl=60, auth=tok)
        sigue = any(str((f.get("playerMaster") or {}).get("id")) == str(pid)
                    for f in (mercado if isinstance(mercado, list) else []))
    if sigue:
        return (SEGUIR, f"{nombre}: la subasta sigue abierta", None)
    return (FALLIDO, f"Subasta PERDIDA: {nombre} ya no está en el mercado y no es tuyo. "
                     "El hueco que ibas a tapar sigue abierto")


#  3. CLAUSULAZO AL EXPIRAR: esperar a que caiga la protección de un rival


@vigilante("clausulazo_al_expirar")
async def clausulazo_al_expirar(seg: dict) -> tuple:
    """Espera a que un objetivo quede clausulable y avisa. No paga solo.

    Un blindaje dura 24 h y la protección de compra 14 días: los dos vencen a una hora
    concreta, así que la espera se puede registrar y delegar en vez de tener que
    acordarse de mirar.

    No ejecuta el clausulazo, aunque podría: mueve dinero y lo autoriza una persona. Lo
    que hace es avisar en el momento exacto en que se abre la ventana, con el importe ya
    releído al euro.
    """
    pid = seg["params"].get("player_id")
    nombre = seg["params"].get("nombre") or str(pid)
    tope = int(seg["params"].get("tope") or 0)
    if not pid:
        return (FALLIDO, "falta el player_id")

    from .config import settings
    cli, tok = await _api()
    async with cli as http:
        d = await http.get(f"{CMP}/player/{pid}/league/{settings.fantasy_league_id}",
                           ttl=30, auth=tok)
    pt = d.get("playerTeam") or {}
    if not pt:
        return (FALLIDO, f"{nombre} ya no pertenece a ningún equipo de la liga")

    clausula = int(pt.get("buyoutClause") or 0)
    blindado = bool(pt.get("isShielded"))
    lock = pt.get("buyoutClauseLockedEndTime")
    ahora = dt.datetime.now(dt.UTC)
    protegido = bool(lock and dt.datetime.fromisoformat(lock) > ahora)

    if blindado:
        return (SEGUIR, f"{nombre}: blindado hasta {pt.get('shieldedEndDate')}", None)
    if protegido:
        falta = (dt.datetime.fromisoformat(lock) - ahora).total_seconds()
        # Se ajusta el ritmo al tiempo que queda: no tiene sentido preguntar cada 5 min
        # por algo que vence dentro de seis días.
        return (SEGUIR, f"{nombre}: protegido hasta {lock[:16]}",
                max(300, min(int(falta / 4), 6 * 3600)))

    if tope and clausula > tope:
        return (SEGUIR, f"{nombre} ya es clausulable, pero su cláusula "
                        f"({clausula:,} €) pasa del tope ({tope:,} €)".replace(",", "."),
                3600)

    return (HECHO, f"🎯 <b>{nombre} YA ES CLAUSULABLE</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
                   f"Cláusula exacta: <b>{clausula:,} €</b>\n".replace(",", ".") +
                   "Dilo y lo ejecuto. No se paga solo: mueve dinero.")


#  4. EXPOSICIÓN: avisar antes de que TU jugador quede clausulable


@vigilante("mi_exposicion")
async def mi_exposicion(seg: dict) -> tuple:
    """Avisa cuando un jugador MÍO está a punto de quedar desprotegido.

    Es el reverso del anterior y el único momento predecible de vulnerabilidad: la
    protección de 14 días vence a una hora conocida. Avisando antes da tiempo a blindar,
    que es gratis. Enterarse después es enterarse por el tablón, cuando ya te lo llevaron.
    """
    ptid = str(seg["params"].get("player_team_id") or "")
    nombre = seg["params"].get("nombre") or ptid
    margen = int(seg["params"].get("aviso_horas") or 3)

    from .config import settings
    cli, tok = await _api()
    async with cli as http:
        plantilla = await http.get(
            f"{CMP}/leagues/{settings.fantasy_league_id}/teams/{settings.fantasy_team_id}",
            ttl=30, auth=tok)
    p = next((x for x in (plantilla.get("players") or [])
              if str(x.get("playerTeamId")) == ptid), None)
    if p is None:
        return (FALLIDO, f"{nombre} ya no está en tu plantilla")

    if p.get("isShielded"):
        return (SEGUIR, f"{nombre}: blindado hasta {p.get('shieldedEndDate')}", None)

    lock = p.get("buyoutClauseLockedEndTime")
    ahora = dt.datetime.now(dt.UTC)
    if lock and dt.datetime.fromisoformat(lock) > ahora:
        faltan = (dt.datetime.fromisoformat(lock) - ahora).total_seconds() / 3600
        if faltan > margen:
            return (SEGUIR, f"{nombre}: protegido {faltan:.1f} h más",
                    max(300, int((faltan - margen) * 3600)))

    cl = int(p.get("buyoutClause") or 0)
    return (HECHO, f"🛡️ <b>{nombre} queda clausulable</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
                   f"Su cláusula son {cl:,} €. Blindar es gratis y dura 24 h."
                   .replace(",", "."))


async def sugerir_desde_plantilla() -> list[dict]:
    """Propone qué vigilancias tendría sentido crear, mirando la plantilla de hoy.

    Sirve para no tener que acordarse: la lista sale sola de quién está sin blindar y con
    la protección a punto de caer.
    """
    async with Session() as s:
        filas = (await s.execute(text("""
            SELECT p.nickname, s.player_team_id, s.clause_lock_until, s.buyout_clause
              FROM squad_snapshots s
              JOIN players p ON p.id = s.player_id
              JOIN managers m ON m.id = s.manager_id
             WHERE s.ts = (SELECT max(ts) FROM squad_snapshots)
               AND m.is_me AND NOT COALESCE(s.shielded, false)
             ORDER BY s.buyout_clause DESC"""))).all()
    return [{"nombre": r[0], "player_team_id": r[1],
             "protegido_hasta": str(r[2]) if r[2] else None,
             "clausula": int(r[3] or 0)} for r in filas]


#  5. CUPO DE BLINDAJE: avisa cuando LaLiga vuelva a dejar blindar


@vigilante("cupo_blindaje")
async def cupo_blindaje(seg: dict) -> tuple:
    """Pregunta cada cierto tiempo si ya hay cupo para blindar, y avisa cuando lo haya.

    El cupo es por jornada (`030.02.03`), pero ningún endpoint publica cuántos quedan
    ni a qué hora vuelven. LaLiga puede rechazar con un solo blindaje activo y seguir
    rechazando con la jornada recién abierta, así que la hora no se puede calcular.

    Programar un reintento a una hora adivinada solo produce un `030.02.03` y un aviso
    más. Preguntar a `check-shield` es barato, no gasta el blindaje y responde la
    única pregunta que importa: *¿puedo ya?*

    Y cuando la respuesta cambia queda registrado cuándo se renovó, que es la única
    forma de acabar fijando la regla.
    """
    ptid = str(seg["params"].get("player_team_id") or "")
    nombre = seg["params"].get("nombre") or ptid
    if not ptid:
        return (FALLIDO, "falta el player_team_id")

    from .config import settings
    cli, tok = await _api()
    try:
        async with cli as http:
            await http.get(
                f"{CMP}/league/{settings.fantasy_league_id}"
                f"/player-team/{ptid}/check-shield", ttl=0, auth=tok)
    except Exception as e:
        cuerpo = cuerpo_de(e)
        if "030.02.03" in cuerpo:
            return (SEGUIR, "sigue sin cupo de blindajes", None)
        if "030.02.01" in cuerpo:
            return (HECHO, f"{nombre} ya está blindado")
        return (SEGUIR, f"check-shield no respondió ({type(e).__name__})", None)

    return (HECHO, f"🛡️ <b>YA HAY CUPO para blindar</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
                   f"Se puede blindar a {nombre}. Dilo y lo lanzo: es gratis y dura 24 h.")


#  6. OFERTAS DE OTROS MANAGERS: rechazarlas, que es lo que se pide al crear la vigilancia


def decidir_ofertas(ofertas: list[dict], out_of_league: bool = False) -> tuple[str, list]:
    """Qué hacer con las ofertas recibidas por un jugador propio. Función pura.

    Decide por el origen de la oferta y el estado del jugador, en este orden:

    | ofertas | jugador | devuelve |
    |---|---|---|
    | ninguna | cualquiera | `("esperar", [])` |
    | alguna del sistema (`isFromMarket`) | cualquiera | `("aceptar", [la de mayor `money`])` |
    | solo de otros managers | `out_of_league` | `("avisar", ofertas)` |
    | solo de otros managers | el resto | `("rechazar", ofertas)` |

    El `rechazar` es lo que se le pide a esta vigilancia al crearla, no una regla del
    juego: se lanza sobre un jugador que se quiere vender al sistema y no a otro manager.
    Un `out_of_league` se saca aparte porque ya no puntúa para nadie, así que ahí la
    respuesta no es evidente y la decide una persona.

    Separada de la llamada HTTP para poder ejercitarla con una tabla de casos: una oferta
    de otro manager no se puede provocar desde aquí.
    """
    if not ofertas:
        return ("esperar", [])
    del_sistema = [o for o in ofertas if o.get("isFromMarket")]
    if del_sistema:
        return ("aceptar", [max(del_sistema, key=lambda o: int(o.get("money") or 0))])
    if out_of_league:
        return ("avisar", ofertas)
    return ("rechazar", ofertas)


@vigilante("ofertas")
async def ofertas(seg: dict) -> tuple:
    """Vigila a un jugador listado y rechaza las ofertas que no vengan del sistema.

    Complementa a `venta`, que cobra la del sistema y ante la de otro manager solo avisa.
    Se crea a mano, para un jugador que se quiere vender al sistema y a nadie más. La
    decisión sale de `decidir_ofertas()`; este vigilante solo la ejecuta y nunca termina:
    devuelve `SEGUIR` en todos los casos salvo que el jugador desaparezca de la plantilla.

    Se rechaza de una en una y parando al primer fallo: son escrituras, no son
    idempotentes y no se reintentan.
    """
    from .config import settings

    ptid = str(seg["params"].get("player_team_id") or "")
    nombre = seg["params"].get("nombre") or ptid
    if not ptid:
        return (FALLIDO, "falta el player_team_id")

    cli, tok = await _api()
    async with cli as http:
        plantilla = await http.get(
            f"{CMP}/leagues/{settings.fantasy_league_id}/teams/{settings.fantasy_team_id}",
            ttl=30, auth=tok)
        ficha = next((p for p in (plantilla.get("players") or [])
                      if str(p.get("playerTeamId")) == ptid), None)
        if ficha is None:
            return (HECHO, f"{nombre} ya no está en la plantilla")
        fuera = ((ficha.get("playerMaster") or {}).get("playerStatus") == "out_of_league")
        try:
            recibidas = await http.get(
                f"{CMP}/league/{settings.fantasy_league_id}/playerTeam/{ptid}/offer",
                ttl=15, auth=tok)
        except Exception as e:
            # Un 403 aquí es "esas ofertas no son tuyas", que sobre un jugador propio no
            # debería pasar: se reintenta en vez de dar el seguimiento por fallido.
            return (SEGUIR, f"no se pudieron leer las ofertas ({type(e).__name__})", None)

    que, cuales = decidir_ofertas(recibidas if isinstance(recibidas, list) else [], fuera)

    if que == "esperar":
        # Mismo criterio que en `venta`: una oferta tarda días, así que el ritmo se relaja
        # solo en vez de gastar una petición cada 30 s durante dos días.
        espera = min(1800, 60 * (1 + int(seg.get("intentos") or 0) // 10))
        return (SEGUIR, f"{nombre}: sin ofertas", espera)

    if que == "aceptar":
        return (SEGUIR, f"{nombre}: la oferta es del SISTEMA, no se rechaza. "
                        "Para cobrarla, el seguimiento que toca es «venta»", None)

    if que == "avisar":
        return (SEGUIR, f"{nombre} está fuera de LaLiga y hay {len(cuales)} oferta(s) de "
                        "rival: aquí SÍ interesa vender (le das lastre y te llevas su "
                        "dinero), pero eso lo decide una persona", None)

    # Los dos ids van en el CUERPO, que es de donde los lee `rechazar_oferta`. Mandarlos
    # por query da un 422 que el `except` de abajo convierte en "no se pudo rechazar", o
    # sea con la misma cara que un fallo de red, y así no se rechaza ninguna.
    rechazadas, fallo = [], None
    for o in cuales:
        try:
            await _accion("rechazar-oferta", {
                "market_id": str(o.get("marketId") or (o.get("market") or {}).get("id")
                                 or o.get("market_id") or ""),
                "offer_id": str(o.get("id") or ""),
                "confirmar": True})
            rechazadas.append(str(o.get("id")))
        except Exception as e:
            fallo = cuerpo_de(e) or str(e)
            break

    if fallo and not rechazadas:
        return (SEGUIR, f"{nombre}: no se pudo rechazar ({fallo[:120]})", None)
    return (SEGUIR, f"{nombre}: rechazada(s) {len(rechazadas)} oferta(s) de rival"
                    + (f"; queda(n) por rechazar: {fallo[:80]}" if fallo else ""), None)
