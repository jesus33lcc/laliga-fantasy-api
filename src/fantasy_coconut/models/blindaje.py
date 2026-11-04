"""Cuándo se puede blindar, y cuándo conviene hacerlo.

Dos blindajes por jornada, gratis viendo un anuncio, que duran 24 h (48 h en premium) y no
se acumulan. El cupo se renueva al cerrar la jornada, no al abrirla, y solo se pone sobre
jugadores con la cláusula ya activa: a uno dentro de sus 14 días ni le hace falta ni le
deja.

Lo que cierra la ventana es haber gastado el cupo, no que la jornada esté rodando (el
tablón registra blindajes puestos con la jornada viva). `Ventana.abierta` no lleva tope
por arriba: solo comprueba que el cupo se haya renovado, así que sigue en `True` con la
jornada en marcha. Lo que no sabe es si el cupo está gastado, y eso lo responde
`check-shield`, que es gratis y no gasta cupo.

El óptimo depende de dónde caiga el cierre de clausulazos (`030.01.17`, 24 h antes de la
jornada): si cae dentro de la ventana, las 24 h de blindaje tienen que acabar ahí; si cae
antes, se pone lo más tarde que se pueda, porque el peligro vuelve al arrancar la jornada.
En un parón no hay cierre que renueve el cupo, y ahí la defensa es subir la cláusula.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

log = logging.getLogger("coconut.models.blindaje")

CMP = "/v1/competition/1"

# Los clausulazos se desactivan 24 h antes de que empiece la jornada (regla `030.01.17`).
HORAS_CIERRE_CLAUSULAZOS = 24
# Lo que dura un blindaje en liga no premium. La respuesta trae `shieldedEndDate`, que es
# la verdad; esto solo sirve para PLANIFICAR antes de tener esa respuesta.
HORAS_BLINDAJE = 24
# Cuántos caben por jornada. La respuesta de LaLiga trae `shieldsLimitReached`, que es la
# verdad; esto solo sirve para planificar antes de tener esa respuesta.
POR_JORNADA = 2
# Cuánto antes del cierre de la ventana se blinda cuando interesa apurarla. No es 0 porque
# dos tareas se separan 5 min entre sí y la llamada puede fallar y necesitar reintento.
MARGEN_FIN_VENTANA = 15


def _local(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone()


@dataclass
class Ventana:
    jornada: int                    # la jornada que se está protegiendo (la próxima)
    abre: dt.datetime               # cierre de la jornada en curso
    cierra: dt.datetime             # apertura de la siguiente
    cierran_clausulazos: dt.datetime
    optimo: dt.datetime
    cupo: int = POR_JORNADA

    @property
    def abierta(self) -> bool:
        """Si el cupo de esta jornada ya se renovó.

        OJO: no lleva tope por arriba, a propósito. Exigir además que la siguiente
        jornada no haya empezado devuelve "cerrada" con la jornada rodando, que es un
        momento en el que sí se puede blindar. Lo único que cierra de verdad es gastar
        los dos, y eso no se sabe desde aquí: lo responde `check-shield`, que es gratis
        y no gasta cupo.
        """
        return dt.datetime.now().astimezone() >= self.abre

    @property
    def es_el_momento(self) -> bool:
        """¿Es ya el punto óptimo, o pasado, con el cupo renovado?"""
        return self.abierta and dt.datetime.now().astimezone() >= self.optimo

    def como_texto(self) -> str:
        ahora = dt.datetime.now().astimezone()
        if ahora < self.abre:
            faltan = (self.abre - ahora).total_seconds() / 3600
            return (f"sin cupo · se renueva al terminar la jornada, el "
                    f"{self.abre:%d-%b a las %H:%M} (en {faltan:.0f} h)")
        if ahora < self.optimo:
            faltan = (self.optimo - ahora).total_seconds() / 3600
            return (f"hay cupo, pero aún es PRONTO · el momento bueno es el "
                    f"{self.optimo:%d-%b a las %H:%M} (en {faltan:.0f} h)")
        if ahora >= self.cierra:
            # Pasada la apertura de la siguiente jornada el óptimo ya no aplica, pero el
            # cupo que quede sigue sirviendo: es mejor gastarlo tarde que perderlo.
            return ("⭐ HAY CUPO y la jornada ya empezó · úsalo, se pierde al cerrar "  # sin-rastro-ok: texto que se pinta en el panel
                    f"la J{self.jornada}")
        return (f"⭐ HAY CUPO y es el momento · el óptimo dura "  # sin-rastro-ok: panel
                f"hasta el {self.cierra:%d-%b %H:%M}")


async def ventana(http, tok) -> Ventana | None:
    """Calcula la ventana de blindaje de la próxima jornada leyendo el calendario real.

    No se deducen las fechas de un cron ni de una constante: la apertura de una jornada
    es la hora de su primer partido, y LaLiga la mueve. Por eso se lee `calendar` de la
    jornada siguiente en vez de suponer "los viernes a las 21:00".
    """
    try:
        w = await http.get(f"{CMP}/week/current", ttl=300, auth=tok)
        n = int(w["weekNumber"])

        # `active_weeks` de `/config` es la señal fiable, mejor que `isLive`: es la lista
        # de jornadas que ya han arrancado, declarada por LaLiga. `[1,2,3,4]` con
        # `weekNumber=5` significa que la 5 no ha empezado. Es un hecho enumerado y no un
        # flag que puede ir con desfase.
        arrancada = bool(w.get("isLive"))          # respaldo si `/config` no contesta
        try:
            cfg = await http.get(f"{CMP}/config", ttl=1800, auth=tok)
            activas = next((x.get("value") for x in (cfg or [])
                            if x.get("name") == "active_weeks"), None)
            if isinstance(activas, list) and activas:
                arrancada = n <= max(int(v) for v in activas)
        except Exception as e:
            log.warning("sin active_weeks (%s): se usa isLive", e)

        if arrancada:
            # La jornada `n` ya arrancó: la ventana abrirá cuando esta cierre.
            protege = n + 1
            abre = _local(w["closingWeekDate"])
            cal = await http.get(f"{CMP}/calendar", params={"weekNumber": n + 1},
                                 ttl=3600, auth=tok)
            # El primer partido FUTURO, no el primero a secas: el calendario puede traer
            # aplazados de jornadas anteriores, y entonces el cierre de la ventana sale
            # ya vencido sin dar ningún error.
            from ..jornada import apertura as _apertura
            _ap = _apertura(cal or [], etiqueta=f"ventana-blindaje J{n + 1}")
            if _ap is None:
                log.warning("la jornada %d aún no tiene calendario futuro: sin ventana", n + 1)
                return None
            cierra = _local(_ap.isoformat())
        else:
            # `n` todavía no ha empezado: el intervalo llega hasta su apertura. Es el
            # horizonte para PLANIFICAR cuándo poner el blindaje, no la disponibilidad
            # real: quién decide si se puede ahora mismo es `se_puede_ya()` contra la API.
            # La jornada anterior la da `previousWeek`, y si falta se cae a `n - 1`.
            protege = n
            cierra = _local(w["openingWeekDate"])
            previa = int(w.get("previousWeek") or (n - 1))
            cal = await http.get(f"{CMP}/calendar", params={"weekNumber": previa},
                                 ttl=3600, auth=tok)
            fin = [_local(p["matchDate"]) for p in (cal or []) if p.get("matchDate")]
            # El cierre exacto de la anterior no lo publica ningún endpoint; se aproxima
            # con su último partido + 6 h. Solo afecta al borde inicial de la ventana, y
            # ese borde lo verifica `se_puede_ya()` contra la API.
            abre = (max(fin) + dt.timedelta(hours=6)) if fin else dt.datetime.now().astimezone()
        abre_siguiente = cierra
        cierre_actual = abre
        n = protege - 1
    except Exception as e:
        log.warning("no se pudo calcular la ventana de blindaje: %s", e)
        return None

    cierran_cl = abre_siguiente - dt.timedelta(hours=HORAS_CIERRE_CLAUSULAZOS)
    # El blindaje debe morir cuando ya nadie pueda clausular, no antes.
    if cierran_cl > cierre_actual:
        # Los clausulazos siguen abiertos parte de la ventana: se cubre ese tramo.
        optimo = max(cierre_actual, cierran_cl - dt.timedelta(hours=HORAS_BLINDAJE))
    else:
        # La ventana entera cae DESPUÉS del cierre de clausulazos, así que dentro de ella
        # nadie puede robar y blindar pronto es tirar las horas. El peligro vuelve al
        # arrancar la jornada, porque con la jornada EN curso sí se puede clausular. Se
        # pone lo más tarde que la ventana permite y las 24 h cubren ese arranque.
        optimo = abre_siguiente - dt.timedelta(minutes=MARGEN_FIN_VENTANA)

    return Ventana(jornada=n + 1, abre=cierre_actual, cierra=abre_siguiente,
                   cierran_clausulazos=cierran_cl, optimo=optimo)


async def anotar_perdida(candidatos: list[dict]) -> None:
    """Añade a cada candidato lo que costaría perderlo, en puntos esperados del once.

    Se recalcula el mejor once SIN él y se resta del actual. Mide la escasez posicional,
    que los puntos no ven: un portero único cuesta su esperado entero porque detrás no hay
    nadie, mientras que el cuarto medio de seis se sustituye casi sin coste.

    No vale `esperado(él) - esperado(su recambio de banquillo)`, que es lo que invita la
    intuición: da la formación por fija. Al perder al único delantero el once no deja el
    hueco, cambia de formación y mete un defensa o un medio de más, así que parte de la
    pérdida se recupera sola. La diferencia entre las dos cuentas basta para dar la vuelta
    al orden de los dos primeros candidatos, que es lo que decide el cupo.

    Cuánto difieren depende de la plantilla del momento: con un recambio sano en esa
    posición la formación absorbe parte de la baja, y sin él las dos cuentas convergen.

    Cuesta un `mejor_once` por candidato sobre la plantilla ya leída: 7 formaciones sobre
    15 jugadores en memoria, sin una petición más.

    Si no se puede calcular (la base va por detrás de la API, o falta el modelo), se deja
    a 0 y el orden cae en los puntos, que sigue dando una lista utilizable.
    """
    for c in candidatos:
        c["perdida"] = 0.0
    try:
        from .alineacion import mejor_once, mi_plantilla
        plantilla = await mi_plantilla()
        base = (await mejor_once(plantilla)).total
    except Exception as e:
        log.warning("sin pérdida marginal (%s): se ordena por puntos", e)
        return

    for c in candidatos:
        pid = str(c.get("player_id"))
        resto = [j for j in plantilla if str(j.player_id) != pid]
        if len(resto) == len(plantilla):
            continue                    # no está en la plantilla que ve el modelo
        c["perdida"] = round(max(base - (await mejor_once(resto)).total, 0.0), 2)


async def a_quien(http, tok, team_id: str | int, liga: str | int,
                  cuantos: int = POR_JORNADA) -> list[dict]:
    """Los `cuantos` jugadores propios que más urge blindar.

    Ordena por lo que cuesta perderlos (la caída del once al sustituirlos); el ratio
    cláusula/valor solo desempata.

    Se descartan los ya blindados, los que están dentro de sus 14 días (ahí LaLiga no
    deja poner blindaje), los `out_of_league` y los que ningún rival puede pagar.

    Devuelve como mucho `cuantos`, y MENOS si no hay tantos al alcance: un blindaje
    puesto sobre quien nadie puede robar gasta medio cupo protegiendo lo único que ya
    estaba a salvo. Con la lista corta, el llamante ve que sobra cupo.
    """
    plantilla = await http.get(f"{CMP}/leagues/{liga}/teams/{team_id}", ttl=60, auth=tok)
    ahora = dt.datetime.now().astimezone()

    # Caja del rival más rico. Para clausular no se puede uno endeudar (lo dice el
    # reglamento; `030.01.32` es otra cosa, el margen de deuda del 20 % en las pujas),
    # así que cuenta la caja limpia.
    #
    # `estimar()` devuelve una TUPLA `(list[Saldo], inicial)` de dataclasses, no una lista
    # de dicts. Tratarla como dicts salta al `except`, el tope queda en infinito y todo el
    # mundo sale "alcanzable".
    try:
        from .saldos import estimar
        saldos, _inicial = await estimar()
        tope = max((float(s.estimado or 0) for s in saldos if not s.es_mio), default=0.0)
        if tope <= 0:
            raise ValueError("la estimación de caja rival salió a cero")
    except Exception as e:
        log.warning("sin estimación de caja rival (%s): no se descarta a nadie", e)
        tope = float("inf")

    out = []
    for p in (plantilla.get("players") or []):
        pm = p.get("playerMaster") or {}
        if p.get("isShielded"):
            continue
        bloq = p.get("buyoutClauseLockedEndTime")
        if bloq and _local(bloq) > ahora:
            continue                       # protegido por los 14 días: ni hace falta ni deja
        # Un `out_of_league` no se blinda: no puede puntuar nunca más, así que si un
        # rival paga su cláusula te está haciendo un favor. Gastar ahí medio cupo de la
        # jornada es protegerse de algo que quieres que pase.
        if str(pm.get("playerStatus") or "").lower() == "out_of_league":
            continue
        valor = int(pm.get("marketValue") or 0)
        clausula = int(p.get("buyoutClause") or 0)
        if not valor or not clausula:
            continue
        out.append({
            "player_id": pm.get("id"),
            "player_team_id": str(p.get("playerTeamId")),
            "nombre": pm.get("nickname") or pm.get("name"),
            "posicion": int(pm.get("positionId") or 0),
            "valor": valor, "clausula": clausula,
            "ratio": round(clausula / valor, 3),
            "puntos": int(pm.get("points") or 0),
            "alcanzable": clausula <= tope,
        })

    await anotar_perdida(out)

    # Orden: primero los que alguien puede pagar de verdad; dentro de esos, el que más
    # duele perder, y a igualdad el más barato de robar.
    #
    # Ordenar solo por ratio pone por delante a quien tiene la cláusula más pegada al
    # valor aunque nadie pueda pagarla. Una vez descartado lo inalcanzable, todos los que
    # quedan son igual de robables, así que lo que decide es cuánto cuesta perderlos.
    #
    # Y "cuánto cuesta perderlos" no son sus puntos: es lo que baja el once al sustituirlos
    # por el mejor recambio de su posición. Los puntos dejan fuera al portero único, cuyo
    # relevo es un suplente que no juega, y priorizan a un medio de los seis que tienes.
    #
    # El inalcanzable NO entra en el resultado. Marcarlo y mandarlo al final no basta:
    # con `out[:cuantos]` y pocos alcanzables, el corte lo incluye igual.
    alcanzables = [d for d in out if d["alcanzable"]]
    alcanzables.sort(key=lambda d: (-d["perdida"], -d["puntos"], d["ratio"]))
    if len(alcanzables) < cuantos:
        log.info("solo %d de la plantilla están al alcance de un rival (tope %.2fM): "
                 "el cupo de blindajes sobra", len(alcanzables), tope / 1e6)
    return alcanzables[:cuantos]


async def se_puede_ya(http, tok, liga, player_team_id: str) -> tuple[bool | None, str]:
    """Pregunta a LaLiga si ahora MISMO se puede blindar. Gratis y sin gastar cupo.

    Esto es la verdad; `ventana()` es solo una predicción. El cálculo se apoya en
    fechas (`week/current`, `calendar`) y siempre tiene bordes: una jornada aplazada, el
    flag `isLive` con desfase, un partido movido de semana. Antes de fiarse del panel se
    contrasta con la API, que no cuesta nada.

    Devuelve `(se_puede, motivo)`. `None` si no se pudo determinar: ahí se conserva lo que
    diga el cálculo, porque quedarse sin respuesta no es lo mismo que un "no".
    """
    import json as _json

    try:
        await http.get(f"{CMP}/league/{liga}/player-team/{player_team_id}/check-shield",
                       ttl=0, auth=tok)
        return True, "LaLiga acepta blindar ahora mismo"
    except _json.JSONDecodeError:
        # `check-shield` responde 200 con el CUERPO vacío cuando la respuesta es que
        # sí. El cliente hace `resp.json()` y revienta al parsear. Pero el
        # `raise_for_status()` va antes, así que si se llega al parseo el HTTP fue 2xx:
        # un cuerpo vacío aquí es un sí, no un fallo. Sin este `except`, la
        # comprobación devuelve siempre "no se pudo comprobar".
        return True, "LaLiga acepta blindar ahora mismo (200 sin cuerpo)"
    except Exception as e:
        cuerpo = ""
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                cuerpo = resp.text or ""
            except Exception:
                pass
        cuerpo = cuerpo or str(e)
        if "030.02.03" in cuerpo:
            # Ojo: el límite puede ser por cupo agotado (ya se usaron los dos de esta
            # jornada) o por ventana cerrada. El código es el mismo, así que no se puede
            # distinguir desde aquí: se dice lo que hay sin inventar la causa.
            return False, ("LaLiga lo rechaza (030.02.03): o el cupo de esta jornada ya "
                           "está gastado, o la ventana no está abierta")
        if "030.02.01" in cuerpo:
            return None, "ese jugador ya está blindado: prueba con otro para saberlo"
        return None, f"no se pudo comprobar ({cuerpo[:80]})"
