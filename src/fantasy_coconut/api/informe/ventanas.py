"""Cuándo se puede atacar y cuándo te pueden atacar. Las dos protecciones del juego vencen
a una hora concreta y conocida, así que la agenda es predecible.

Parte de `/informe/*`. Ver `comun.py` para lo compartido."""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter

from ...config import settings
from ...models.saldos import estimar
from ..deps import api, bearer, cliente, protegido
from .comun import CMP, POS, _saldo_real

log = logging.getLogger("coconut.api.informe.ventanas")
router = APIRouter()


@router.get("/ventanas-liga", summary="A qué hora se puede atacar a quién, en toda la liga")
async def ventanas_liga():
    """Cuándo queda clausulable cada jugador de la liga, propio o rival, ordenado por hora.

    Las dos protecciones vienen en cada ficha y vencen a una hora concreta:
    `buyoutClauseLockedEndTime` (14 días desde la compra) e `isShielded` (24 h desde que
    se activó), así que las ventanas de toda la liga se pueden calcular de una vez.

    Se cruza con la ventana global: los clausulazos se desactivan 24 h antes de cada
    jornada (`030.01.17`), así que un objetivo que se libera después de ese corte no se
    puede tocar hasta que la jornada arranque. Con la jornada ya en curso sí se puede
    clausular: lo que cierra la ventana son las 24 h previas a la siguiente, no la jornada
    viva.
    """
    tok = await bearer()
    liga = settings.fantasy_league_id
    ahora = dt.datetime.now(dt.UTC)

    try:
        vc = await ventana_clausulazo()
        cierre = dt.datetime.fromisoformat(vc["clausulazos_hasta"]) if vc.get(
            "clausulazos_hasta") else None
    except Exception:
        vc, cierre = {}, None

    equipos = await protegido(cliente().get(
        f"{CMP}/leagues/{liga}/teams", ttl=300, auth=tok))

    mi_caja = await _saldo_real() or 0
    filas = []
    for eq in equipos:
        tid = str(eq.get("id"))
        es_mio = tid == str(settings.fantasy_team_id)
        # La plantilla individual es la única que trae `isShielded`: el agregado no.
        try:
            plantilla = await protegido(cliente().get(
                f"{CMP}/leagues/{liga}/teams/{tid}", ttl=180, auth=tok))
        except Exception:
            continue
        dueño = ((eq.get("manager") or {}).get("managerName")
                 or eq.get("name") or f"equipo {tid}")

        for p in (plantilla.get("players") or []):
            pm = p.get("playerMaster") or {}
            clausula = int(p.get("buyoutClause") or 0)
            valor = int(pm.get("marketValue") or 0)
            blindado = bool(p.get("isShielded"))
            fin_blindaje = p.get("shieldedEndDate")
            lock = p.get("buyoutClauseLockedEndTime")

            libre_desde, motivo = None, "libre"
            if lock:
                f = dt.datetime.fromisoformat(lock)
                if f > ahora:
                    libre_desde, motivo = f, "protegido (14 días)"
            if blindado and fin_blindaje:
                f = dt.datetime.fromisoformat(fin_blindaje)
                # Manda la que caduque más tarde: hasta entonces no es atacable.
                if libre_desde is None or f > libre_desde:
                    libre_desde, motivo = f, "blindado (24 h)"

            atacable_ya = libre_desde is None
            # Un objetivo que se libera DESPUÉS del cierre de la ventana no sirve para
            # esta jornada, por muy jugoso que sea.
            en_ventana = (cierre is None or (libre_desde or ahora) < cierre)

            filas.append({
                # Hace falta para enlazar con la ficha (`#jugador/{id}`) desde la lista.
                "player_id": int(pm.get("id") or 0),
                "jugador": pm.get("nickname"), "posicion": POS.get(pm.get("positionId"), "?"),
                "equipo": dueño, "es_mio": es_mio,
                "clausula": clausula, "valor": valor,
                "ratio": round(clausula / valor, 3) if valor else None,
                "puntos": pm.get("points") or 0,
                "estado": pm.get("playerStatus"),
                "atacable_ya": atacable_ya,
                "libre_desde": libre_desde.isoformat() if libre_desde else None,
                "horas_para_liberarse": (round((libre_desde - ahora).total_seconds() / 3600, 1)
                                         if libre_desde else 0),
                "motivo": motivo if not atacable_ya else "sin protección",
                "en_ventana_de_jornada": en_ventana,
                "me_llega": (not es_mio) and clausula <= mi_caja,
            })

    rivales = [f for f in filas if not f["es_mio"]]
    return {
        "ventana_global": vc,
        "mi_caja": mi_caja,
        # Lo que puedo atacar ahora y me cabe en caja, lo mejor primero.
        "puedo_atacar_ya": sorted(
            [f for f in rivales if f["atacable_ya"] and f["me_llega"]],
            key=lambda f: -f["puntos"])[:15],
        # Lo que se libera pronto: es la agenda de las próximas horas.
        "se_liberan_pronto": sorted(
            [f for f in rivales if not f["atacable_ya"] and f["horas_para_liberarse"] <= 48],
            key=lambda f: f["horas_para_liberarse"])[:20],
        # Los míos ya expuestos o que lo estarán en 48 h: `horas_para_liberarse` vale 0
        # para quien ya está libre, así que aquí entran los dos casos.
        "me_quedo_expuesto": sorted(
            [f for f in filas if f["es_mio"] and f["horas_para_liberarse"] <= 48],
            key=lambda f: f["horas_para_liberarse"])[:20],
        "total_jugadores": len(filas),
    }


@router.get("/ventana-blindaje", summary="¿Se puede blindar, cuándo conviene y a quién?")
async def ventana_blindaje():
    """Dice si se puede blindar ahora, a quién conviene y cuándo ponerlo.

    Devuelve `abierta_segun_calculo` (estimación por fechas de `models/blindaje.ventana`),
    `abierta_segun_laliga` (respuesta de `check-shield`) y `abierta`, que es la que vale:
    manda LaLiga si contestó, y el cálculo solo cuando no. Si discrepan, lo señala.
    Consultar `check-shield` es gratis y no gasta el blindaje.

    El cupo es de dos por jornada. Lo que cierra la ventana es haberlos gastado, no
    que la jornada esté en juego: el tablón registra blindajes puestos con una jornada
    rodando (ver `models/blindaje.py`).

    El *cuándo conviene* sale de que el blindaje dura 24 h mientras la ventana dura días:
    se recomienda ponerlo 24 h antes de que se cierren los clausulazos, para que la
    protección cubra el último tramo en que se puede robar.

    El blindaje no aparece en el centro de ayuda oficial (ver `docs/REGLAS.md`):
    estas reglas salen de la web de LaLiga y de la prensa especializada.
    """
    from ...models.blindaje import a_quien, se_puede_ya, ventana

    tok = await bearer()
    v = await ventana(cliente(), tok)
    if v is None:
        return {"ok": False, "motivo": "no hay calendario de la próxima jornada todavía"}

    try:
        objetivos = await a_quien(cliente(), tok, settings.fantasy_team_id,
                                  settings.fantasy_league_id, v.cupo)
    except Exception as e:
        objetivos, err = [], str(e)[:120]
        log.warning("no se pudo calcular a quién blindar: %s", err)

    # Contraste contra LaLiga. El cálculo de arriba estima la ventana por fechas y un
    # borde mal estimado la desplaza jornadas enteras. `check-shield` es gratis y no gasta
    # blindaje. Si discrepan, gana LaLiga (ver `abierta`, más abajo).
    # Solo se puede preguntar si hay algún objetivo: la ruta lleva un `playerTeamId`.
    real, motivo_real = (None, "sin comprobar")
    if objetivos:
        real, motivo_real = await se_puede_ya(
            cliente(), tok, settings.fantasy_league_id,
            str(objetivos[0]["player_team_id"]))

    abierta = v.abierta if real is None else real
    discrepa = real is not None and real != v.abierta

    return {
        "jornada_protegida": v.jornada,
        "abierta": abierta,
        "abierta_segun_calculo": v.abierta,
        "abierta_segun_laliga": real,
        "verificado_con_laliga": motivo_real,
        "discrepancia": (
            f"El cálculo decía {'abierta' if v.abierta else 'cerrada'} y LaLiga dice "
            f"{'abierta' if real else 'cerrada'}. Manda LaLiga. Revisa "
            f"models/blindaje.ventana()." if discrepa else None),
        "es_el_momento": v.es_el_momento and abierta,
        # `como_texto()` solo sabe de fechas, así que dice "hay cupo" mientras LaLiga
        # responde `030.02.03`. Quien lee el panel ve esa frase, no los campos de al
        # lado: si el árbitro dice que no, el texto tiene que decir que no.
        "estado": (v.como_texto() if abierta else
                   f"SIN CUPO ahora mismo · {motivo_real}. El cálculo por fechas decía: "
                   f"{v.como_texto()}"),
        "cupo_por_jornada": v.cupo,
        "ventana": {"abre": v.abre.isoformat(), "cierra": v.cierra.isoformat()},
        "cierran_clausulazos": v.cierran_clausulazos.isoformat(),
        "momento_optimo": v.optimo.isoformat(),
        # `a_quien()` descarta a quien ningún rival puede pagar, así que esta lista puede
        # venir más corta que el cupo, o vacía. Corta significa que sobra cupo: no hay a
        # quién gastarlo.
        "a_quien": objetivos,
        "nota": ("Dos por jornada, gratis viendo un anuncio, 24 h de protección. Solo se "
                 "pueden poner sobre jugadores con la cláusula ya activa: a uno dentro de "
                 "sus 14 días no le hace falta y LaLiga no deja."),
    }


@router.get("/ventana-clausulazo", summary="¿Se puede clausular ahora, y hasta cuándo?")
async def ventana_clausulazo():
    """Los clausulazos se desactivan 24 h antes de cada jornada (error `030.01.17`).

    La ventana no está siempre abierta, y eso no aparece en la documentación oficial.
    Saberlo antes de planificar evita decidir un clausulazo para una hora en la que la API
    ya no lo acepta, y descubrirlo con un 400 cuando no da tiempo a otra cosa.
    """
    import datetime as dt

    tok = await bearer()
    w = await protegido(api().jornada_actual()) if hasattr(api(), "jornada_actual") else None
    if w is None:
        w = await protegido(cliente().get(api()._p("/week/current"), ttl=1800, auth=tok))

    n = w.get("nextWeek") if w.get("isLive") else w.get("weekNumber")
    cal = await protegido(cliente().get(
        api()._p(f"/calendar?weekNumber={n}"), ttl=3600, auth=tok))
    partidos = cal if isinstance(cal, list) else cal.get("matches", [])

    # No vale `min(matchDate)`: el calendario puede traer partidos APLAZADOS de jornadas
    # anteriores, y entonces la ventana sale ya vencida sin dar ningún error.
    # `jornada.apertura_de` filtra los ya jugados y prefiere `openingWeekDate` si lo hay.
    from ...jornada import apertura_de
    apertura = apertura_de(w, partidos, etiqueta=f"ventana-clausulazo J{n}")
    if apertura is None:
        return {"jornada": n, "abierta": None,
                "motivo": "no hay calendario futuro para esa jornada"}

    cierre = apertura - dt.timedelta(hours=24)
    ahora = dt.datetime.now(dt.UTC)
    quedan = (cierre - ahora).total_seconds() / 3600

    return {
        "jornada": n,
        "abre_la_jornada": apertura.isoformat(),
        "clausulazos_hasta": cierre.isoformat(),
        "abierta": quedan > 0,
        "horas_restantes": round(quedan, 1) if quedan > 0 else 0,
        # Con una jornada en curso sí se puede clausular: lo que cierra la ventana son
        # las 24 h previas a la SIGUIENTE jornada (`030.01.17`), no la jornada en juego.
        "aviso": ("quedan menos de 6 h para que se cierren los clausulazos"
                  if 0 < quedan < 6 else
                  "cerrado: estamos dentro de las 24 h previas a la jornada"
                  if quedan <= 0 else None),
    }


@router.get("/exposicion", summary="Quién me pueden quitar, con caja rival")
async def exposicion():
    tok = await bearer()
    liga, yo = settings.fantasy_league_id, settings.fantasy_team_id
    # Plantilla individual: el agregado no trae `isShielded`.
    plantilla = await protegido(api().plantilla(tok, liga, yo))
    saldos, inicial = await estimar()
    rivales = sorted([(getattr(s, "nombre", "?"), s.estimado or 0)
                      for s in saldos if not s.es_mio], key=lambda x: -x[1])
    tope = rivales[0][1] if rivales else 0
    ahora = dt.datetime.now(dt.UTC)

    filas = []
    for p in plantilla.get("players", []):
        pm = p["playerMaster"]
        valor = int(pm.get("marketValue") or 1)
        clausula = int(p.get("buyoutClause") or 0)
        lock = p.get("buyoutClauseLockedEndTime") or ""
        protegido_ = False
        if lock:
            try:
                protegido_ = dt.datetime.fromisoformat(lock) > ahora
            except ValueError:
                pass
        blindado = bool(p.get("isShielded"))
        atacable = (not protegido_) and (not blindado) and clausula <= tope
        filas.append({
            "nombre": pm["nickname"], "posicion": POS.get(pm["positionId"], "?"),
            "clausula": clausula, "valor": valor,
            "ratio": round(clausula / valor, 3) if valor else None,
            "puntos_temporada": pm.get("points"),
            "blindado": blindado, "blindado_hasta": p.get("shieldedEndDate"),
            "protegido_hasta": lock if protegido_ else None,
            "atacable": atacable,
        })

    atacables = [f for f in filas if f["atacable"]]
    # Aquí NO se sugiere a quién blindar, y es deliberado. El ratio ordena la lista de
    # expuestos y nada más: no mira los puntos ni el `out_of_league`, así que pone en
    # cabeza al más barato, que suele ser el que menos duele perder.
    # A quién blindar lo contesta `GET /informe/ventana-blindaje`, que descarta a quien
    # ningún rival puede pagar y ordena por lo que cuesta perderlos. Dos respuestas a la
    # misma pregunta se acaban confundiendo, así que aquí solo hay una lista.
    return {
        "caja_rival_estimada": [{"manager": n, "estimado": v} for n, v in rivales],
        # `estimar()` devuelve `(saldos, presupuesto_inicial_deducido)`. El segundo NO son
        # notas: es el inicial del reglamento con el que se despeja la contabilidad.
        "presupuesto_inicial": inicial,
        "puede_pagar_hasta": tope,
        "atacables": sorted(atacables, key=lambda f: f["ratio"] or 9),
        "a_salvo": [f for f in filas if not f["atacable"]],
        # Quien lee este JSON no ve el código, así que el puntero al endpoint bueno va
        # dentro de la respuesta.
        "a_quien_blindar": "GET /informe/ventana-blindaje",
    }
