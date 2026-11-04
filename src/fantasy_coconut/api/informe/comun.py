"""Lo que comparten los informes: `/informe/*`, la rutina diaria hecha endpoints.

Cada pregunta que hay que hacerse a diario tiene aquí su endpoint, y `GET /informe` las
une en una sola llamada. La diferencia con `/fantasy/*` es que esto decide: cruza
datos, avisa y ordena por lo que importa.

Tres de ellos cubren errores que cuestan una jornada entera:

  · `/informe/validar-once`: un once que se congela con 10 jugadores anula la jornada
    (`fixturePoints` a 0 por muchos puntos que hayan hecho).
  · `/informe/enfrentamientos`: contra quién juega cada uno, y los duelos internos,
    donde dos jugadores propios se anulan entre ellos.
  · `/informe/exposicion`: quién puede quitarte a quién, leyendo el blindaje del
    endpoint correcto (el agregado no lo trae).
"""

from __future__ import annotations

import contextlib
import logging

from sqlalchemy import bindparam, text

from ...config import settings
from ...db import Session
from ..deps import api, bearer, cliente, protegido

log = logging.getLogger("coconut.api.informe")

# Un fallo de PROGRAMACIÓN, no del mundo. Si una de estas sale de un bloque del panel,
# el bloque no es que no se pueda calcular hoy: es que está roto y no funcionará mañana
# tampoco. Distinguirlas es lo que separa "LaLiga no contesta" de "me falta un import".
BUGS = (NameError, AttributeError, TypeError, KeyError, IndexError,
        ValueError, ZeroDivisionError, UnboundLocalError)


@contextlib.asynccontextmanager
async def bloque(out: list[dict], que: str, urgencia_bug: int = 55):
    """Aísla un bloque del panel: si falla, lo dice; no se lleva el panel por delante.

    Un `except Exception: pass` deja el bloque fuera sin rastro, y un panel al que le falta
    "te pueden clausular a X" se lee igual que uno donde nadie puede robarte. Aquí siempre
    se registra con traza y siempre sale en pantalla que está incompleto.

    Y separa el bug del mundo: que LaLiga no conteste sale con urgencia baja, y un
    `NameError` por encima del umbral del resumen diario, que es la única forma de
    enterarse sin mirar el log.
    """
    try:
        yield
    except Exception as e:
        es_bug = isinstance(e, BUGS)
        log.exception("panel: el bloque %r falló (%s)", que, type(e).__name__)
        out.append({
            "urgencia": urgencia_bug if es_bug else 15,
            "clave": f"bloque_roto:{que}",
            "titulo": f"⚠️ El panel está incompleto: falta «{que}»",
            "detalle": f"{type(e).__name__}: {e}"[:160],
            "accion": ("Es un fallo del código, no del juego: mirar el log"
                       if es_bug else "Reintentar; LaLiga o la base no respondieron"),
            "porque": "Un bloque que desaparece sin avisar se lee como «aquí no hay nada»",
        })

# Aquí no hay router: `comun.py` es solo lo compartido. Un router declarado en un módulo
# que nadie incluye registra sus rutas en el vacío, y desaparecen sin dar ningún error.

CMP = "/v1/competition/1"
POS = {1: "POR", 2: "DEF", 3: "MED", 4: "DEL", 5: "ENTRENADOR"}
EST_PARTIDO = {1: "pendiente", 2: "en juego", 3: "en juego", 7: "final"}

# Un jugador en estos estados ocupa plaza pero no cubre su posición: puede alinearse y
# puntúa 0. Produce avisos que parecen un fallo de datos ("tengo 2 defensas" con 3 en
# pantalla) y no lo son.
NO_CUBREN = ("injured", "suspended", "out_of_league")

# Lo que exigen las 7 formaciones. Ninguna prescinde del portero ni del delantero, así que
# con 0 de cualquiera de los dos la jornada entera se anula (evento tipo 7).
MINIMOS = (("POR", 1), ("DEF", 3), ("MED", 3), ("DEL", 1))


def cobertura(fichas: list[tuple[str, str | None]]) -> tuple[dict[str, int], list[str]]:
    """¿Da la plantilla para un once legal? Devuelve `(cuenta por posición, qué falta)`.

    Existe para que haya una sola regla. La necesitan la ficha de equipo y los asuntos
    de rivales, y dos copias de una regla del juego acaban divergiendo: la primera que se
    corrige deja a la otra dando una respuesta distinta sobre los mismos datos, sin que
    nada falle.

    `fichas` son pares `(posición, estado)`. Se piden así, y no la plantilla entera, para
    que sirva tanto con las fichas normalizadas de la API propia como con las crudas de
    LaLiga, sin obligar a construir un diccionario intermedio solo para contar.
    """
    cuenta: dict[str, int] = {}
    for pos, estado in fichas:
        if (estado or "ok") not in NO_CUBREN:
            cuenta[pos] = cuenta.get(pos, 0) + 1
    faltan = []
    if len(fichas) < 11:
        faltan.append(f"solo tiene {len(fichas)} jugadores, hacen falta 11")
    for pos, minimo in MINIMOS:
        if cuenta.get(pos, 0) < minimo:
            faltan.append(f"{cuenta.get(pos, 0)} {pos}, mínimo {minimo}")
    return cuenta, faltan


async def _saldo_real() -> int | None:
    """Saldo disponible leído de la API, que es `teamMoney - teamInvestment`.

    Se pregunta a la API en vez de usar `vigilancia.mi_saldo()`, que lee el snapshot de
    la BD y puede ir hasta 30 min por detrás. Para proponer compras hace falta el saldo
    de ahora: entre la ingesta y la decisión pueden haber entrado una venta o una puja.

    Las dos fuentes miden lo mismo; lo que cambia es la frescura, no que una esté mal.
    """
    try:
        tok = await bearer()
        m = await protegido(cliente().get(
            api()._p(f"/teams/{settings.fantasy_team_id}/money"), ttl=120, auth=tok))
        return int(m.get("teamMoney") or 0) - int(m.get("teamInvestment") or 0)
    except Exception:
        return None


async def _equipos_de_jugadores() -> dict[int, int]:
    """player_id -> team_id. La plantilla no trae `teamId`; la BD sí."""
    async with Session() as s:
        filas = await s.execute(text("SELECT id, team_id FROM players WHERE team_id IS NOT NULL"))
        return {int(r[0]): int(r[1]) for r in filas}


async def _puntos_jornada(tok: str) -> dict:
    """Lo que llevas de verdad en la jornada en curso, jugador a jugador.

    Los *quiénes* salen del once congelado (el que puntúa) y los *cuántos* de la BD,
    porque el `weekPoints` que trae el propio `lineup` viene `null` hasta que la
    jornada cierra: leerlo como 0 pinta un once entero a cero con partidos ya jugados.
    """
    w = await protegido(cliente().get(f"{CMP}/week/current", ttl=300, auth=tok))
    n = int(w["weekNumber"])
    if not w.get("isLive"):
        return {"abierta": False, "jornada": n}

    cal = await protegido(cliente().get(f"{CMP}/calendar", params={"weekNumber": n},
                                        ttl=300, auth=tok))
    jugados = {int(p[k + "Id"]) for p in (cal or []) if p.get("matchState") == 7
               for k in ("local", "visitor")}

    # El total lo dice LaLiga, no hay que sumarlo. `GET /leagues/{lg}/teams`
    # trae `fixturePoints`: los puntos de la jornada EN curso, de todos los managers, en
    # UNA petición. Es la única cifra autoritativa mientras la jornada está viva.
    total_oficial = None
    try:
        equipos = await protegido(cliente().get(
            f"{CMP}/leagues/{settings.fantasy_league_id}/teams", ttl=120, auth=tok))
        for t in (equipos or []):
            if str(t.get("id")) == str(settings.fantasy_team_id):
                total_oficial = int(t.get("fixturePoints") or 0)
    except Exception as e:
        log.warning("sin fixturePoints: %s", e)

    # Desglose por jugador. La base no sirve durante la jornada: el maestro público
    # `/players` no publica la jornada en curso, así que leer de ahí pinta el once entero
    # a cero. `players/live-points` sí la trae, y cruza por `playerMasterId`, no por
    # `id`: con `id` no casa con nadie y el desglose sale vacío sin dar error.
    vivo: dict[str, int] = {}
    try:
        lp = await protegido(cliente().get(f"{CMP}/players/live-points", ttl=60, auth=tok))
        for x in (lp if isinstance(lp, list) else []):
            for w in (x.get("weekPoints") or []):
                if str(w.get("weekNumber")) == str(n) and w.get("points") is not None:
                    vivo[str(x.get("playerMasterId"))] = int(w["points"])
    except Exception as e:
        log.warning("live-points no disponible: %s", e)

    ln = await protegido(cliente().get(
        f"{CMP}/teams/{settings.fantasy_team_id}/lineup/week/{n}", ttl=120, auth=tok))
    once = []
    for linea in ("goalkeeper", "defender", "midfield", "striker"):
        v = (ln.get("formation") or {}).get(linea) or []
        for p in (v if isinstance(v, list) else [v]):
            pm = p.get("playerMaster") or {}
            once.append((int(pm.get("id") or 0), pm.get("nickname"),
                         int(pm.get("positionId") or 0), int(pm.get("teamId") or 0)))

    # `live-points` solo trae datos con partidos rodando: entre partidos devuelve `[]` y
    # un desglose que dependa solo de él se queda en blanco a media jornada. El respaldo es
    # `player_points`, que se rellena una vez por jugador desde `/player/{id}` y solo para
    # los que ya cerraron su partido: ~11 peticiones por jornada, no por consulta.
    pts = await _cachear_puntos(n, once, jugados, vivo, tok)

    detalle, suma, hechos = [], 0, 0
    for pid, nombre, pos, team in once:
        acabado = team in jugados
        # `pts` ya viene resuelto por `_cachear_puntos()`: caché > live-points >
        # `/player/{id}`. Nunca `players.raw`, que llega a guardar valores PARCIALES
        # (una foto tomada a mitad de partido). Si no hay dato se muestra pendiente: un
        # hueco dice la verdad y un número parcial no.
        p = pts.get(pid)
        if acabado:
            hechos += 1
            suma += int(p or 0)
        detalle.append({"nombre": nombre, "posicion": pos,
                        "puntos": int(p) if p is not None else None,
                        "ha_jugado": acabado})

    return {"abierta": True, "jornada": n,
            # El oficial manda; la suma propia solo se usa si LaLiga no contestó.
            "mis_puntos_jornada": total_oficial if total_oficial is not None else suma,
            "suma_del_desglose": suma,
            "jugadores_completados": hechos, "de": len(once), "once": detalle}


async def _cachear_puntos(n: int, once: list, jugados: set,
                          vivo: dict, tok: str) -> dict[int, int]:
    """Puntos por jugador de la jornada `n`, con caché en `player_points`.

    Orden de confianza: lo ya guardado > `live-points` > `/player/{id}`.
    Nunca `players.raw`: el maestro público no publica la jornada en curso, y llega a
    guardar valores parciales, tomados a mitad de partido.
    """
    ids = [i[0] for i in once]
    async with Session() as s:
        guardado = dict((await s.execute(text(
            # `= ANY(:ids)` es de Postgres; `IN :ids` con `expanding` vale en los dos.
            "SELECT player_id, points FROM player_points "
            "WHERE jornada = :n AND player_id IN :ids AND points IS NOT NULL").bindparams(
                bindparam("ids", expanding=True)),
            {"n": n, "ids": list(ids)})).all())

    faltan = [(pid, team) for pid, _nom, _pos, team in once
              if pid not in guardado and str(pid) not in vivo and team in jugados]
    nuevos: dict[int, int] = {}
    for pid, _team in faltan:
        try:
            d = await protegido(cliente().get(f"{CMP}/player/{pid}", ttl=600, auth=tok))
            w = [x for x in (d.get("playerStats") or [])
                 if str(x.get("weekNumber")) == str(n)]
            if w and w[0].get("totalPoints") is not None:
                nuevos[pid] = int(w[0]["totalPoints"])
        except Exception as e:
            log.warning("no se pudo leer los puntos de %s: %s", pid, e)

    if nuevos:
        async with Session() as s:
            for pid, v in nuevos.items():
                await s.execute(text(
                    "INSERT INTO player_points (player_id, jornada, points) "
                    "VALUES (:p, :n, :v) ON CONFLICT (player_id, jornada) "
                    "DO UPDATE SET points = EXCLUDED.points"),
                    {"p": pid, "n": n, "v": v})
            await s.commit()
        log.info("player_points: %d jugadores cacheados de la J%d", len(nuevos), n)

    salida = dict(guardado)
    salida.update(nuevos)
    # `live-points` manda sobre lo guardado: es lo más fresco mientras se juega.
    for k, v in vivo.items():
        salida[int(k)] = v
    return salida


async def _fuerza_equipos() -> dict[int, float]:
    """Fuerza de cada equipo = valor medio de sus jugadores.

    Sirve para avisar de un rival duro sin mantener a mano una lista de nombres: los
    equipos grandes salen arriba solos porque sus jugadores valen más, y el orden se
    reajusta con el mercado sin tocar código.
    """
    async with Session() as s:
        filas = await s.execute(text("""
            SELECT p.team_id, AVG(CAST((p.raw->>'marketValue') AS INTEGER))
            FROM players p
            WHERE p.team_id IS NOT NULL AND p.status = 'ok'
            GROUP BY p.team_id
        """))
        return {int(r[0]): float(r[1] or 0) for r in filas}


# Traducción de los conceptos de `playerStats`. Vienen en inglés y como `[cantidad, puntos]`.
# Solo se listan los que de verdad puntúan o explican una jornada: la respuesta trae ~40
# claves y la mayoría valen 0 siempre, así que enseñarlas todas es ruido.
CONCEPTOS = {
    "mins_played": "Minutos", "goals": "Goles", "goal_assist": "Asistencias",
    "penalty_won": "Penaltis provocados", "penalty_save": "Penaltis parados",
    "saves": "Paradas", "effective_clearance": "Despejes",
    "penalty_failed": "Penaltis fallados", "own_goals": "Goles en propia",
    "goals_conceded": "Goles encajados", "yellow_card": "Amarillas",
    "second_yellow_card": "Segundas amarillas", "red_card": "Rojas",
    "total_scoring_att": "Tiros", "won_contest": "Regates",
    "ball_recovery": "Recuperaciones", "poss_lost_all": "Pérdidas",
    "penalty_conceded": "Penaltis cometidos", "marca_points": "Puntos Marca",
    "pen_area_entries": "Entradas al área", "offtarget_att_assist": "Tiros fuera",
    "duel_won": "Duelos ganados", "clean_sheet": "Portería a cero",
}


# Qué aporta cada función premium y qué hace falta para usarla. Se conocen las rutas,
# pero ninguna se ha ejecutado nunca: hace falta una liga premium para probarlas.
PREMIUM = {
    "captain": {
        "titulo": "Capitán",
        "que_hace": "Designas un capitán por jornada y sus puntos se multiplican.",
        "impacto": "El más rentable de todos: bien elegido añade los puntos de tu mejor "
                   "jugador otra vez, sin gastar un euro. Y es la decisión que más "
                   "premia acertar el calendario de dificultad.",
        "regla": "Un entrenador NO puede ser capitán (020.01.23).",
        "rutas": [],
    },
    "bench": {
        "titulo": "Banquillo",
        "que_hace": "Los suplentes entran automáticamente si un titular no juega.",
        "impacto": "Elimina el peor caso del sistema: un titular que no sale deja de "
                   "valer 0. Hoy eso solo lo cubre la guardia del once, y solo si el "
                   "jugador ya no está en la plantilla.",
        "regla": "Hoy `formation.bench` viene `{}`.",
        "rutas": [],
    },
    "formations": {
        "titulo": "Más formaciones",
        "que_hace": "Desbloquea alineaciones además de las 7 básicas.",
        "impacto": "Más margen para encajar la plantilla que tengas, sobre todo cuando "
                   "vas justo de una posición.",
        "regla": "",
        "rutas": ["GET /api/v4/teams/lineup/formations",
                  "PUT {CMP}/teams/{teamId}/tactical-formation"],
    },
    "loan": {
        "titulo": "Cesiones",
        "que_hace": "Prestas un jugador a otro manager durante un tiempo pactado.",
        "impacto": "Convierte plantilla parada en ingresos sin perder al jugador. Y "
                   "abre negociación con los rivales, que hoy no existe.",
        "regla": "Máximo 3 cesiones, duración 1 jornada, mínimo el 25 % del valor "
                 "(030.01.34 a 030.01.38).",
        "rutas": ["POST {CMP}/league/{lg}/loan",
                  "POST {CMP}/league/{lg}/loan/{loanOfferId}/accept",
                  "POST {CMP}/league/{lg}/loan/{loanOfferId}/reject",
                  "POST {CMP}/league/{lg}/loan/{loanOfferId}/cancel",
                  "POST {CMP}/league/{lg}/playerTeam/{ptid}/loan",
                  "POST {CMP}/league/{lg}/playerTeam/{ptid}/return-loan"],
    },
    "ideal": {
        "titulo": "Once ideal",
        "que_hace": "Paga una recompensa si aciertas jugadores del once ideal de la jornada.",
        "impacto": "Ingreso extra recurrente. La tarifa que declara la liga es de "
                   "100.000 €: el mismo orden que la recompensa diaria.",
        "regla": "",
        "rutas": ["GET {CMP}/ideal/{weekNumber}"],
    },
    "coach": {
        "titulo": "Entrenador",
        "que_hace": "Alineas un entrenador, que puntúa por el resultado de su equipo.",
        "impacto": "Una plaza más que rellenar y otra fuente de puntos. Hoy "
                   "`formation.coach` viene `[]` y los entrenadores del mercado no "
                   "sirven para nada.",
        "regla": "No puede ser capitán (020.01.23).",
        "rutas": [],
    },
}
