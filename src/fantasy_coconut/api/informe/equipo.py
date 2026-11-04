"""Tu equipo y el de los demás: plantilla, once, puntos y calendario.

Parte de `/informe/*`. Ver `comun.py` para lo compartido."""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from ...config import settings
from ...db import Session
from ...models.saldos import estimar
from ...sqlite_compat import fecha
from ...tiempo import ahora
from ..deps import api, bearer, cliente, protegido
from .comun import (
    CMP,
    CONCEPTOS,
    EST_PARTIDO,
    POS,
    _equipos_de_jugadores,
    _fuerza_equipos,
    _puntos_jornada,
    _saldo_real,
    cobertura,
)

log = logging.getLogger("coconut.api.informe.equipo")
router = APIRouter()


# Las piezas de `equipo()`. Todas sin entrada/salida: reciben lo que LaLiga devolvió y
# devuelven la forma que usa la interfaz.
#
# Están separadas porque cada una guarda una trampa de la API, y dentro de una función de
# 160 líneas esas trampas no se ven. Sueltas se leen de una vez y se pueden probar con una
# respuesta guardada, sin tocar la API.


def _jornada_a_mirar(semana: dict, pedida: int | None, ahora: dt.datetime) -> int:
    """Qué jornada enseñar por defecto: la última que YA SE jugó.

    `weekNumber` apunta a la próxima en cuanto se cierra la anterior. Sin este ajuste
    se pide el once de una jornada que no ha empezado y salen once ceros, como si
    nadie hubiera puntuado.
    """
    actual = int(semana.get("weekNumber") or 1)
    if pedida:
        return pedida
    abre = semana.get("openingWeekDate")
    ya_empezo = bool(semana.get("isLive")) or (
        abre and dt.datetime.fromisoformat(abre) <= ahora)
    return actual if ya_empezo else max(1, actual - 1)


def _normalizar_plantilla(plantilla: dict, ahora: dt.datetime) -> list[dict]:
    """Las fichas de un equipo, con lo que hace falta para decidir si se puede atacar.

    Las dos protecciones son distintas y hay que mirar las dos: `isShielded` es el
    blindaje que se activa a propósito, y `buyoutClauseLockedEndTime` son los 14 días
    automáticos del recién comprado. Un jugador es atacable solo si no tiene ninguna.
    """
    jugadores = []
    for p in (plantilla.get("players") or []):
        pm = p.get("playerMaster") or {}
        clausula = int(p.get("buyoutClause") or 0)
        valor = int(pm.get("marketValue") or 0)
        lock = p.get("buyoutClauseLockedEndTime")
        protegido_aun = bool(lock and dt.datetime.fromisoformat(lock) > ahora)
        jugadores.append({
            "player_id": int(pm.get("id") or 0),
            "player_team_id": p.get("playerTeamId"),
            "nombre": pm.get("nickname"), "posicion": POS.get(pm.get("positionId"), "?"),
            "posicion_id": pm.get("positionId"),
            "valor": valor, "clausula": clausula,
            "ratio": round(clausula / valor, 3) if valor else None,
            "puntos": pm.get("points") or 0, "media": pm.get("averagePoints"),
            "estado": pm.get("playerStatus"),
            "foto": ((pm.get("images") or {}).get("transparent") or {}).get("256x256"),
            "blindado": bool(p.get("isShielded")),
            "blindado_hasta": p.get("shieldedEndDate"),
            "protegido_hasta": lock,
            "atacable": not p.get("isShielded") and not protegido_aun,
        })
    jugadores.sort(key=lambda j: (j["posicion_id"] or 9, -j["puntos"]))
    return jugadores


def _estado_por_equipo(calendario: list | None) -> dict[int, int]:
    """`teamId` `matchState` de esa jornada.  1 = pendiente · 2/3 = en juego · 7 = final.

    Sin esto, un jugador cuyo partido aún no se ha disputado sale con 0 puntos y es
    indistinguible de uno que jugó y no puntuó. Al leer el once de un rival son dos cosas
    muy distintas: una dice "le ha salido mal", la otra "todavía no se sabe".

    Y hay que mirarlo partido a partido: una jornada puede estar repartida por
    semanas y su calendario arrastra aplazados, así que "la jornada empezó" no implica
    que hayan jugado todos.
    """
    estado: dict[int, int] = {}
    for m in (calendario or []):
        st = int(m.get("matchState") or 0)
        for lado in ("local", "visitor"):
            tid = m.get(f"{lado}Id") or ((m.get(lado) or {}).get("id"))
            if tid:
                estado[int(tid)] = st
    return estado


def _normalizar_once(lineup: dict, estado_equipo: dict[int, int],
                     n: int) -> tuple[list[dict], int, list[int]]:
    """El once congelado de la jornada `n`, sus puntos y la formación `[DEF, MED, DEL]`.

    `None` no es lo mismo que 0. Los puntos solo se convierten en cero cuando
    consta que el partido terminó; si no, se dejan sin valor y la interfaz pinta un
    guion. Un hueco dice la verdad y un cero falso no.
    """
    once: list[dict] = []
    puntos_once = 0
    formacion: list[int] = []
    f = lineup.get("formation") or {}
    for k in ("goalkeeper", "defender", "midfield", "striker"):
        v = f.get(k) or []
        v = v if isinstance(v, list) else [v]
        if k != "goalkeeper":
            formacion.append(len(v))
        for p in v:
            pm = p.get("playerMaster") or {}
            pts = None
            for st in (pm.get("lastStats") or []):
                if int(st.get("weekNumber", 0)) == n:
                    pts = int(st.get("totalPoints") or 0)
            est = estado_equipo.get(int(pm.get("teamId") or 0))
            if pts is None and est == 7:
                pts = 0
            puntos_once += pts or 0
            once.append({"player_id": int(pm.get("id") or 0),
                         "player_team_id": p.get("playerTeamId"),
                         "nombre": pm.get("nickname"),
                         "posicion": POS.get(pm.get("positionId"), "?"),
                         "linea": k,
                         "team_id": pm.get("teamId"),
                         "estado": pm.get("playerStatus"),
                         "foto": ((pm.get("images") or {})
                                  .get("transparent") or {}).get("256x256"),
                         # None = no se pudo saber (sin calendario), y se dice.
                         "jugado": None if est is None else est == 7,
                         "en_juego": None if est is None else est in (2, 3),
                         "puntos": pts})
    return once, puntos_once, formacion


async def _caja_estimada_de(nombre_mgr: str | None) -> int | None:
    """Lo que le queda en caja a ese manager, deducido del tablón.

    Es una cota superior: no se restan las subidas de cláusula ajenas, que el
    tablón no publica. El saldo real es igual o menor, nunca mayor.
    """
    try:
        saldos, _ = await estimar()
        return next((int(x.estimado or 0) for x in saldos
                     if str(x.nombre) == str(nombre_mgr)), None)
    except Exception as e:
        log.warning("sin caja estimada para %s: %s", nombre_mgr, e)
        return None


async def _ultima_cerrada(manager_id: str | None) -> tuple[int | None, int | None]:
    """Jornada y puntos de la última jornada cerrada de ese manager.

    Salen de la recompensa de jornada del tablón, que paga 100.000 € por punto exactos:
    no gasta una petición y no depende de `fixturePoints`, que vuelve a 0 al cerrar.
    """
    if not manager_id:
        return None, None
    try:
        async with Session() as s:
            fila = (await s.execute(text("""
                SELECT raw->>'weekNumber', amount FROM league_events
                WHERE kind = 'recompensa' AND manager_id = :m
                ORDER BY ts DESC, id DESC LIMIT 1"""), {"m": int(manager_id)})).first()
    except Exception as e:
        log.warning("sin última jornada cerrada de %s: %s", manager_id, e)
        return None, None
    if not fila or fila[1] is None:
        return None, None
    return (int(fila[0]) if fila[0] else None), int(fila[1]) // 100_000


@router.get("/equipo/{team_id}", summary="La plantilla y el once de cualquier manager")
async def equipo(team_id: str, jornada: int | None = None):
    """Ver a un rival por dentro: plantilla, once y puntos por jornada.

    Son los mismos endpoints que el equipo propio, solo cambiando el `teamId`. En una liga
    donde todos leen las mismas fuentes, saber qué tiene el rival y a quién alinea es la
    mitad del juego, y la plantilla ajena es pública.

    El once que se devuelve es el congelado de esa jornada (`lineup/week/{N}`), no
    el actual: es el que puntuó de verdad. Pedir `lineup` a secas daría el que tiene
    preparado para la siguiente, que es otra cosa.
    """
    tok = await bearer()
    liga = settings.fantasy_league_id
    ahora = dt.datetime.now(dt.UTC)

    semana = await protegido(api().jornada_actual())
    actual = int(semana.get("weekNumber") or 1)
    n = _jornada_a_mirar(semana, jornada, ahora)

    equipos = await protegido(cliente().get(f"{CMP}/leagues/{liga}/teams", ttl=300, auth=tok))
    eq = next((t for t in equipos if str(t.get("id")) == str(team_id)), None)
    if eq is None:
        raise HTTPException(404, f"no hay ningún equipo {team_id} en la liga")

    plantilla = await protegido(cliente().get(
        f"{CMP}/leagues/{liga}/teams/{team_id}", ttl=180, auth=tok))
    jugadores = _normalizar_plantilla(plantilla, ahora)

    # El calendario y el once son opcionales: sin ellos la ficha sigue valiendo. Cada
    # uno se pide por separado a propósito, para que un fallo no se lleve al otro.
    estado_equipo: dict[int, int] = {}
    try:
        cal = await protegido(cliente().get(f"{CMP}/calendar?weekNumber={n}", ttl=300, auth=tok))
        estado_equipo = _estado_por_equipo(cal)
    except Exception as e:
        # Sin calendario no se sabe quién ha jugado. Se sigue: es peor quedarse sin once
        # que enseñarlo sin poder marcar los pendientes.
        log.warning("sin calendario de la J%s: %s", n, e)

    once, puntos_once, formacion = [], 0, []
    try:
        lineup = await protegido(cliente().get(
            f"{CMP}/teams/{team_id}/lineup/week/{n}", ttl=300, auth=tok))
        once, puntos_once, formacion = _normalizar_once(lineup, estado_equipo, n)
    except Exception as e:
        log.warning("sin once congelado de la J%s para %s: %s", n, team_id, e)

    cuenta, faltan = cobertura([(j["posicion"], j["estado"]) for j in jugadores])
    mi_caja = await _saldo_real() or 0
    es_mio = str(team_id) == str(settings.fantasy_team_id)
    nombre_mgr = ((eq.get("manager") or {}).get("managerName") or eq.get("name"))
    # La caja propia se lee de la API al euro. La cota del tablón sale 68M por encima de
    # la real en la ficha propia (94,66M frente a 26,34M).
    _caja_estimada = mi_caja if es_mio else await _caja_estimada_de(nombre_mgr)
    ultima_n, ultima_pts = await _ultima_cerrada((eq.get("manager") or {}).get("id"))

    return {
        "por_posicion": dict(cuenta),
        "puede_alinear": not faltan,
        "le_falta": faltan,
        "team_id": team_id, "es_mio": es_mio,
        "manager": nombre_mgr,
        # `fixturePoints` es el marcador de la jornada EN CURSO: vuelve a 0 al cerrarla,
        # así que la última cerrada sale aparte.
        "puntos": eq.get("teamPoints"), "puntos_jornada_en_curso": eq.get("fixturePoints"),
        "puntos_ultima_jornada": ultima_pts, "ultima_jornada_cerrada": ultima_n,
        "valor_plantilla": eq.get("teamValue"),
        "posicion_liga": eq.get("position"),
        # En un rival es cota superior: no se restan sus subidas de cláusula. La web pinta
        # la propia sin "(est.)".
        "caja_estimada": _caja_estimada,
        "jornada": n, "jornada_actual": actual,
        "once": once, "puntos_once": puntos_once,
        # [DEF, MED, DEL]: para poder pintarlo sobre el campo en vez de en lista.
        "formacion": formacion,
        "pendientes": sum(1 for j in once if j["jugado"] is False),
        "jugadores": jugadores,
        "mi_caja": mi_caja,
        "atacables_que_me_llegan": sorted(
            [j for j in jugadores if not es_mio and j["atacable"] and j["clausula"] <= mi_caja],
            key=lambda j: -j["puntos"]),
    }


@router.get("/jugador/{player_id}", summary="Ficha completa: puntos por concepto y precio")
async def jugador(player_id: int):
    """Todo lo que se sabe de un jugador, en una llamada.

    Junta cuatro fuentes que hay que mirar por separado:

    - `playerStats`: el desglose real de sus puntos por jornada y concepto
      (`[cantidad, puntos]`). Es lo que explica *por qué* hizo esos puntos.
    - `playerTeam`: de quién es, su cláusula, y si está blindado o protegido.
    - La serie de precios: la propia, más fina, y la oficial de LaLiga, más larga.
    - El momentum, que es la señal que de verdad predice la subida de mañana.
    """
    tok = await bearer()
    d = await protegido(cliente().get(
        f"{CMP}/player/{player_id}/league/{settings.fantasy_league_id}", ttl=120, auth=tok))
    pm = d.get("playerMaster") or {}
    pt = d.get("playerTeam") or {}

    jornadas = []
    for st in (pm.get("playerStats") or []):
        detalle = []
        for clave, par in (st.get("stats") or {}).items():
            if not isinstance(par, list) or len(par) < 2:
                continue
            cantidad, puntos = par[0], par[1]
            # Se enseña lo que puntuó o lo que ocurrió; un 0/0 no dice nada.
            if not cantidad and not puntos:
                continue
            detalle.append({"concepto": CONCEPTOS.get(clave, clave),
                            "cantidad": cantidad, "puntos": puntos})
        detalle.sort(key=lambda x: -abs(x["puntos"]))
        jornadas.append({"jornada": st.get("weekNumber"),
                         "puntos": st.get("totalPoints"),
                         "en_once_ideal": st.get("isInIdealFormation"),
                         "detalle": detalle})
    jornadas.sort(key=lambda j: j["jornada"] or 0)

    # Precio: la serie propia (4 tomas al día) y la oficial de LaLiga (un valor diario).
    async with Session() as s:
        propia = [{"fecha": str(r[0]), "valor": int(r[1])} for r in (await s.execute(text("""
            SELECT date(ts), CAST(round(AVG(value)) AS INTEGER) FROM market_values
             WHERE player_id = :p AND value > 0 GROUP BY 1 ORDER BY 1"""),
            {"p": player_id})).all()]
        try:
            oficial = [{"fecha": str(r[0]), "valor": int(r[1])} for r in (await s.execute(text("""
                SELECT fecha, valor FROM market_values_oficial
                 WHERE player_id = :p ORDER BY fecha"""), {"p": player_id})).all()]
        except Exception as e:
            # La tabla la llena un job por lotes, así que en una instalación nueva puede
            # no existir todavía. Se sigue sin ella, pero queda dicho: si el fallo es
            # otro (un SQL mal escrito), el log es lo único que lo delata.
            log.warning("sin histórico oficial de precios de %s (%s)",
                        player_id, type(e).__name__)
            oficial = []

    from ...models import precio as _precio
    mom = next((m for m in await _precio.momentum(minimo_pct=-1e9)
                if m["player_id"] == player_id), None)

    ahora = dt.datetime.now(dt.UTC)
    lock = pt.get("buyoutClauseLockedEndTime")
    valor = int(pm.get("marketValue") or 0)
    clausula = int(pt.get("buyoutClause") or 0)
    return {
        "id": player_id,
        "nombre": pm.get("nickname"), "nombre_largo": pm.get("name"),
        "posicion": POS.get(pm.get("positionId"), "?"),
        "equipo_id": pm.get("teamId"), "estado": pm.get("playerStatus"),
        "foto": ((pm.get("images") or {}).get("transparent") or {}).get("256x256"),
        "valor": valor,
        "puntos": pm.get("points"), "media": pm.get("averagePoints"),
        "puntos_temporada_anterior": pm.get("lastSeasonPoints"),
        "dueño": ((pt.get("manager") or {}).get("managerName")
                  if pt else None) or (d.get("manager") or {}).get("managerName"),
        "clausula": clausula or None,
        "ratio": round(clausula / valor, 3) if clausula and valor else None,
        "blindado": bool(pt.get("isShielded")),
        "blindado_hasta": pt.get("shieldedEndDate"),
        "protegido_hasta": lock,
        "atacable": bool(pt) and not pt.get("isShielded") and not (
            lock and dt.datetime.fromisoformat(lock) > ahora),
        "jornadas": jornadas,
        "precio_propio": propia, "precio_oficial": oficial,
        "momentum": mom,
    }


@router.get("/optimo/{jornada}", summary="Tu once contra el más rentable de la jornada")
async def optimo(jornada: int):
    """Compara lo que hiciste con lo que se podía hacer.

    `GET {CMP}/profitable/{jornada}` devuelve el once más rentable de esa jornada en
    toda la competición: los que más puntuaron por millón. Es backtesting sin construir
    nada, y es lo que contesta el post-jornada: si el fallo fue del modelo o fue varianza.
    """
    tok = await bearer()
    mio = await protegido(cliente().get(
        api()._p(f"/teams/{settings.fantasy_team_id}/lineup/week/{jornada}"), ttl=3600, auth=tok))
    ideal = await protegido(cliente().get(
        api()._p(f"/profitable/{jornada}"), ttl=6 * 3600, auth=tok))

    def desglosar(lineup: dict) -> list[dict]:
        f = lineup.get("formation") or {}
        out = []
        for clave in ("goalkeeper", "defender", "midfield", "striker"):
            bloque = f.get(clave) or []
            for p in (bloque if isinstance(bloque, list) else [bloque]):
                pm = p.get("playerMaster") or {}
                pts = pm.get("weekPoints")
                if isinstance(pts, list):      # el once propio trae la lista entera
                    pts = next((w.get("points") for w in pts
                                if w.get("weekNumber") == jornada), 0)
                out.append({"nombre": pm.get("nickname"),
                            # Son dos campos y hacen falta los dos. `linea` es el bloque
                            # donde lo alineó su dueño y `posicion` la demarcación del
                            # jugador; no siempre coinciden. Mandar el nombre inglés del
                            # bloque como `posicion` deja a la interfaz pidiendo el chip
                            # de "goalkeeper" y pintando "?" en los once.
                            "linea": clave,
                            "posicion": POS.get(pm.get("positionId"), "?"),
                            "player_id": int(pm.get("id") or 0),
                            "foto": ((pm.get("images") or {})
                                     .get("transparent") or {}).get("256x256"),
                            "puntos": int(pts or 0),
                            "valor": int(pm.get("marketValue") or 0)})
        return out

    tuyo, mejor = desglosar(mio), desglosar(ideal)
    pts_tuyo, pts_mejor = sum(j["puntos"] for j in tuyo), sum(j["puntos"] for j in mejor)
    val_tuyo = sum(j["valor"] for j in tuyo) or 1

    return {
        "jornada": jornada,
        "tuyo": {"puntos": pts_tuyo, "jugadores": len(tuyo),
                 "valor": val_tuyo, "pts_por_millon": round(pts_tuyo / (val_tuyo / 1e6), 2)},
        "optimo": {"puntos": pts_mejor, "formacion": (ideal.get("formation") or {}).get("tacticalFormation"),
                   "valor": ideal.get("teamValue"),
                   "pts_por_millon": round(pts_mejor / ((ideal.get("teamValue") or 1) / 1e6), 2),
                   # El once ÓPTIMO entero, para poder pintarlo sobre el campo. No basta
                   # con `te_faltaron`, que es un subconjunto: sin los que sí tenías, el
                   # dibujo queda incompleto y sin decir por qué.
                   "once": mejor},
        "diferencia": pts_mejor - pts_tuyo,
        # Quién entró en el óptimo y no en el tuyo: los fichajes que se dejaron pasar.
        "te_faltaron": [j for j in mejor if j["nombre"] not in {x["nombre"] for x in tuyo}],
        "tu_once": sorted(tuyo, key=lambda x: -x["puntos"]),
    }


@router.get("/jornada", summary="Puntos, once congelado y si la jornada está contando")
async def jornada(numero: int | None = None):
    tok = await bearer()
    liga, yo = settings.fantasy_league_id, settings.fantasy_team_id
    semana = await protegido(api().jornada_actual())
    n = numero or int(semana.get("weekNumber") or 1)

    partidos = {}
    for c in await protegido(api().calendario(n)):
        for a in (c["localId"], c["visitorId"]):
            partidos[int(a)] = {
                "fecha": c["matchDate"],
                "estado": EST_PARTIDO.get(int(c["matchState"]), "?"),
                "rival": int(c["visitorId"] if a == c["localId"] else c["localId"]),
            }

    once = await protegido(
        cliente().get(f"{CMP}/teams/{yo}/lineup/week/{n}", ttl=60, auth=tok))
    f = once.get("formation", {})

    titulares, total, faltan = [], 0, []
    for k in ("goalkeeper", "defender", "midfield", "striker"):
        v = f.get(k) or []
        for p in (v if isinstance(v, list) else [v]):
            pm = p["playerMaster"]
            # Puntos de esa jornada. `weekPoints` es dinámico y cambia con la jornada
            # en curso: sumarlo para auditar una jornada pasada da un número falso.
            pts = 0
            for st in (pm.get("lastStats") or []):
                if int(st.get("weekNumber", 0)) == n:
                    pts = int(st.get("totalPoints") or 0)
            total += pts
            info = partidos.get(int(pm.get("teamId") or 0), {})
            if info.get("estado") != "final":
                faltan.append(pm["nickname"])
            titulares.append({
                "nombre": pm["nickname"], "posicion": POS.get(pm["positionId"], "?"),
                "puntos": pts, "partido": info.get("fecha"),
                "estado_partido": info.get("estado"),
            })

    equipos = await protegido(
        cliente().get(f"{CMP}/leagues/{liga}/teams", ttl=300, auth=tok))
    mio = next((t for t in equipos if str(t.get("id")) == str(yo)), {})
    oficiales = mio.get("fixturePoints")

    alertas = []
    if len(titulares) < 11:
        alertas.append(
            f"🚨 ONCE INCOMPLETO: {len(titulares)} jugadores. Con la plantilla incompleta "
            f"la jornada NO puntúa: el marcador oficial se queda en 0.")
    # Esta comprobación solo vale con la jornada abierta. `fixturePoints` es el
    # marcador de la jornada EN curso y se reinicia a cero en cuanto una cierra, así que
    # contra un once ya terminado daría siempre "no está puntuando", que es falso.
    cierre = fecha(semana.get("closingWeekDate"))
    cerrada = bool(cierre and ahora() >= cierre)
    if total > 0 and oficiales == 0 and not cerrada:
        alertas.append(
            f"🚨 Tienes {total} puntos en el once pero el marcador oficial de la jornada "
            f"es 0. Señal de que la jornada no está contando.")

    return {"jornada": n, "en_juego": semana.get("isLive"),
            "abre": semana.get("openingWeekDate"), "cierra": semana.get("closingWeekDate"),
            "puntos_sumados_del_once": total, "puntos_oficiales_jornada": oficiales,
            "num_titulares": len(titulares), "once_completo": len(titulares) == 11,
            "por_jugar": faltan, "titulares": titulares, "alertas": alertas}


@router.get("/en-vivo", summary="Puntos en vivo de la jornada")
async def en_vivo():
    """`GET {CMP}/players/live-points`, más el acumulado real de la jornada.

    "Jornada abierta" y "partido rodando" no son lo mismo. `live-points` solo devuelve
    algo mientras la pelota rueda: entre partidos contesta `[]`, y eso no significa
    cero. Confundirlos hace que la pantalla anuncie *"no hay ninguna jornada en juego"* y
    0 puntos con la jornada abierta y puntos ya sumados.

    Se contestan las dos cosas por separado:

    - `en_juego`: hay partidos rodando ahora (lo que da `live-points`).
    - `jornada_abierta`: la jornada está viva, aunque ahora no juegue nadie.
    - `mis_puntos_jornada`: lo que de verdad llevas: el once congelado
      (`lineup/week/{N}`, los *quiénes*) cruzado con `players.weekPoints.points` de la BD
      (los *cuántos*). Es lo único que cuenta para la clasificación.
    """
    tok = await bearer()
    lista: list = []
    try:
        datos = await protegido(cliente().get(
            f"{CMP}/players/live-points", ttl=60, auth=tok))
        lista = datos if isinstance(datos, list) else []
    except Exception as e:
        log.warning("live-points no disponible: %s", type(e).__name__)

    # `None`, no un conjunto vacío. Si esto falla no se puede decir "llevas 0 puntos",
    # solo "no sé cuáles son tuyos": con `mios` vacío ningún jugador sale como propio y la
    # pantalla enseña 0 puntos con la jornada rodando, que es un número plausible y
    # falso. Quien lo lea abajo distingue los dos casos comprobando `is None`.
    mios: set[str] | None = None
    try:
        propia = await protegido(api().plantilla(
            tok, settings.fantasy_league_id, settings.fantasy_team_id))
        mios = {str((p.get("playerMaster") or {}).get("id"))
                for p in (propia.get("players") or [])}
    except Exception as e:
        log.warning("en vivo: no se pudo leer la plantilla propia (%s)", type(e).__name__)

    jornada: dict = {}
    try:
        jornada = await _puntos_jornada(tok)
    except Exception as e:
        log.warning("no se pudo calcular el acumulado de la jornada: %s", e)

    abierta = bool(jornada.get("abierta"))
    return {
        "en_juego": bool(lista),
        "jornada_abierta": abierta,
        "motivo": (None if lista else
                   ("la jornada está abierta, pero ahora mismo no hay ningún partido "
                    "rodando" if abierta else "no hay ninguna jornada en juego")),
        "jugadores": [{**j, "es_mio": (None if mios is None
                                       else str(j.get("playerMasterId")) in mios)}
                      for j in lista],
        # `playerMasterId`, no `id`: es el nombre que usa live-points, y cruzar por `id`
        # no casa con nadie, así que da 0 puntos sin error ninguno.
        "mis_puntos_en_vivo": (None if mios is None else
                               sum(int(j.get("points") or 0) for j in lista
                                   if str(j.get("playerMasterId")) in mios)),
        "aviso": (None if mios is not None else
                     "no se pudo leer tu plantilla: no se sabe qué jugadores son tuyos"),
        **jornada,
    }


@router.get("/clasificacion/{jornada}", summary="Clasificación de una jornada concreta")
async def clasificacion_jornada(jornada: int):
    """Clasificación de la jornada `N`, vía `GET {CMP}/leagues/{lg}/standing/{N}`.

    OJO a la ruta: va sin la palabra `week`. `standing/week/{N}` responde 404, lo
    que invita a concluir que no existe clasificación por jornada. Sí existe.
    """
    tok = await bearer()
    try:
        datos = await protegido(cliente().get(
            f"{CMP}/leagues/{settings.fantasy_league_id}/standing/{jornada}",
            ttl=600, auth=tok))
    except Exception as e:
        raise HTTPException(502, f"no se pudo leer la jornada {jornada}: {type(e).__name__}") from e

    filas = []
    for t in (datos if isinstance(datos, list) else datos.get("data") or []):
        eq = t.get("team") or t
        filas.append({
            "team_id": str(eq.get("id") or t.get("teamId") or ""),
            "manager": ((eq.get("manager") or {}).get("managerName")
                        or eq.get("name") or t.get("managerName")),
            "puntos_jornada": t.get("points") if t.get("points") is not None
                              else eq.get("fixturePoints"),
            "puntos_totales": t.get("totalPoints") or eq.get("teamPoints"),
            "posicion": t.get("position") or eq.get("position"),
            "es_mio": str(eq.get("id") or "") == str(settings.fantasy_team_id),
        })
    filas.sort(key=lambda f: -(f["puntos_jornada"] or 0))
    return {"jornada": jornada, "clasificacion": filas}


@router.get("/validar-once", summary="¿El once actual es legal y completo?")
async def validar_once():
    """Un once de 10 anula la jornada entera, así que conviene comprobarlo antes."""
    tok = await bearer()
    yo = settings.fantasy_team_id
    once = await protegido(cliente().get(f"{CMP}/teams/{yo}/lineup", ttl=30, auth=tok))
    f = once.get("formation", {})
    plantilla = await protegido(
        api().plantilla(tok, settings.fantasy_league_id, yo))
    mios = {str(p.get("playerTeamId")) for p in plantilla.get("players", [])}

    puestos, ausentes, n = {}, [], 0
    for k in ("goalkeeper", "defender", "midfield", "striker"):
        v = f.get(k) or []
        lista = v if isinstance(v, list) else [v]
        puestos[k] = len(lista)
        for p in lista:
            n += 1
            ptid = str(p.get("playerTeamId"))
            if ptid not in mios:
                ausentes.append(p["playerMaster"]["nickname"])

    problemas = []
    if n != 11:
        problemas.append(f"🚨 el once tiene {n} jugadores, no 11: la jornada NO puntuaría")
    if ausentes:
        problemas.append(f"🚨 ya no son tuyos: {', '.join(ausentes)}")
    if puestos.get("goalkeeper", 0) != 1:
        problemas.append(" debe haber exactamente 1 portero")

    return {"num_jugadores": n, "por_puesto": puestos,
            "formacion": f.get("tacticalFormation"),
            "jugadores_ausentes": ausentes,
            "valido": not problemas, "problemas": problemas,
            "actualizado": once.get("updatedAt")}


@router.get("/enfrentamientos", summary="Rival de cada jugador, duelos internos y avisos")
async def enfrentamientos(jornada: int | None = None):
    """Lo que el modelo nunca mira: contra quién juega cada uno.

    Un portero o un defensa frente a uno de los equipos más caros de la liga es una plaza
    quemada de antemano: sin portería a cero se va la mitad de sus puntos posibles. Y si
    dos jugadores tuyos se enfrentan, se anulan: cuando uno marca, el otro encaja.
    """
    tok = await bearer()
    yo = settings.fantasy_team_id
    semana = await protegido(api().jornada_actual())
    n = jornada or int(semana.get("nextWeek") if semana.get("isLive")
                       else semana.get("weekNumber") or 1)

    equipos_jug = await _equipos_de_jugadores()
    fuerza = await _fuerza_equipos()
    top = sorted(fuerza.items(), key=lambda x: -x[1])[:3]
    ids_top = {t for t, _ in top}

    plantilla = await protegido(
        api().plantilla(tok, settings.fantasy_league_id, yo))

    calendario = await protegido(api().calendario(n))
    partido_de = {}
    for c in calendario:
        local, visitante = int(c["localId"]), int(c["visitorId"])
        partido_de[local] = {"rival": visitante, "casa": True,
                             "fecha": c["matchDate"], "id": c["id"]}
        partido_de[visitante] = {"rival": local, "casa": False,
                                 "fecha": c["matchDate"], "id": c["id"]}

    jugadores, por_partido = [], {}
    for p in plantilla.get("players", []):
        pm = p["playerMaster"]
        tid = equipos_jug.get(int(pm["id"]))
        info = partido_de.get(tid or -1)
        avisos = []
        if info and info["rival"] in ids_top and pm["positionId"] in (1, 2):
            avisos.append("⚠️ rival de los más fuertes de la liga (portero/defensa)")
        if info is None:
            avisos.append("sin partido en esta jornada")
        j = {"nombre": pm["nickname"], "posicion": POS.get(pm["positionId"], "?"),
             "equipo": tid, "rival": info["rival"] if info else None,
             "en_casa": info["casa"] if info else None,
             "fecha": info["fecha"] if info else None,
             "puntos_temporada": pm.get("points"), "avisos": avisos}
        jugadores.append(j)
        if info:
            # Se guarda con su equipo: un "duelo" solo lo es si mis jugadores están en
            # lados opuestos. Agrupar solo por partido marca como duelo a dos compañeros
            # de equipo, que es ruido puro.
            por_partido.setdefault(info["id"], []).append((tid, pm["nickname"]))

    duelos = []
    for pid, lista in por_partido.items():
        lados = {t for t, _ in lista}
        if len(lados) > 1:
            duelos.append({
                "partido": pid,
                "lados": {str(t): [n for tt, n in lista if tt == t] for t in lados},
                "jugadores": [n for _, n in lista],
            })

    return {
        "jornada": n,
        "equipos_mas_fuertes": [{"team_id": t, "valor_medio": round(v)} for t, v in top],
        "duelos_internos": duelos,
        "avisos": ([" DUELO INTERNO: " +
                    " CONTRA ".join(", ".join(v) for v in d["lados"].values())
                    for d in duelos] +
                   [f"⚠️ {j['nombre']} ({j['posicion']}) contra un rival top"
                    for j in jugadores if any("más fuertes" in a for a in j["avisos"])]),
        "jugadores": sorted(jugadores, key=lambda x: (x["fecha"] or "", x["posicion"])),
    }
