"""`GET /estado`: la foto operativa en una llamada.

Lee de la API en vivo, no de la BD, porque los `models/*` van hasta 2 h por detrás y
esta pantalla se usa justo para decidir. Cuesta 3 peticiones, todas cacheadas.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter

from ..config import settings
from ..politica import LIMITES, gasto_de_hoy, modo
from .deps import api, bearer, cliente, protegido

_log = logging.getLogger("coconut.api.estado")

router = APIRouter(tags=["estado"])

CMP = "/v1/competition/1"
POS = {1: "POR", 2: "DEF", 3: "MED", 4: "DEL", 5: "ENTRENADOR"}


def _web_desplegada() -> str | None:
    """mtime del fichero más nuevo de `web/`. Barato: son unas pocas decenas."""
    from pathlib import Path
    raiz = Path(__file__).resolve().parents[3] / "web"
    if not raiz.is_dir():
        return None
    try:
        ultimo = max(f.stat().st_mtime for f in raiz.rglob("*")
                     if f.is_file() and f.suffix in (".js", ".css", ".html"))
        return dt.datetime.fromtimestamp(ultimo, dt.UTC).isoformat()
    except ValueError:
        return None


async def _identidad() -> dict:
    """Los nombres guardados al conectar la cuenta. Si no hay, se devuelve vacío: una
    interfaz sin nombre de liga es correcta; con el nombre de otra liga, no."""
    import json as _json

    from sqlalchemy import text as _text

    from ..db import Session
    try:
        async with Session() as s:
            fila = (await s.execute(
                _text("SELECT valor FROM ajustes WHERE clave = 'identidad'"))).scalar()
        return _json.loads(fila) if isinstance(fila, str) else (fila or {})
    except Exception as e:
        # En una instalación nueva la tabla `ajustes` puede no existir todavía, y una
        # identidad vacía es una respuesta correcta. Cualquier otro motivo también acaba
        # aquí, así que se registra: es lo que distingue "aún no hay nada" de "se rompió".
        _log.warning("no se pudo leer la identidad (%s)", type(e).__name__)
        return {}


async def _puntos_cerrados() -> dict[int, int]:
    """Puntos por jugador de las jornadas ya cerradas, de `player_points`.

    Los llena `job_puntos` leyendo la ficha individual, que es la fuente que sí tiene
    los puntos de todo el mundo.
    """
    from sqlalchemy import text as _text

    from ..db import Session
    try:
        async with Session() as s:
            filas = await s.execute(_text(
                "SELECT player_id, sum(points) FROM player_points "
                "WHERE points IS NOT NULL GROUP BY player_id"))
        return {int(r[0]): int(r[1] or 0) for r in filas}
    except Exception as e:
        _log.warning("no se pudieron leer los puntos por jornada (%s)", type(e).__name__)
        return {}


def _mejor_total(pm: dict, cerrados: dict[int, int]) -> int:
    """El total de puntos más completo que se conoce, de las dos fuentes.

    Ninguna de las dos lo tiene todo, y se quedan cortas por motivos opuestos:

    - el maestro omite jornadas de algunos jugadores sin avisar;
    - `player_points` solo guarda jornadas cerradas, así que durante una jornada viva
      va por detrás de quien acaba de puntuar.

    Quedarse con el mayor da siempre el total más completo que se conoce, porque el fallo
    de las dos es por defecto: omiten, no inventan.
    """
    try:
        maestro = int(pm.get("points") or 0)
    except (TypeError, ValueError):
        maestro = 0
    try:
        pid = int(pm.get("id") or 0)
    except (TypeError, ValueError):
        return maestro
    return max(maestro, cerrados.get(pid, 0))


def _manager_id(equipo: dict) -> str:
    """El id del manager de un equipo. `manager` llega como objeto o como id suelto, y
    algunas respuestas lo traen además en `managerId`: se miran los tres."""
    m = equipo.get("manager")
    if isinstance(m, dict):
        return str(m.get("id") or "")
    return str(m or equipo.get("managerId") or "")


@router.get("/estado", summary="Foto operativa: caja, plantilla, jornada y sistema")
async def estado():
    tok = await bearer()
    liga, yo = settings.fantasy_league_id, settings.fantasy_team_id

    semana = await protegido(api().jornada_actual())
    equipos = await protegido(
        cliente().get(f"{CMP}/leagues/{liga}/teams", ttl=300, auth=tok))
    agregado = next((t for t in equipos if str(t.get("id")) == str(yo)), {})

    # El endpoint agregado `/leagues/{lg}/teams` no trae `isShielded` ni
    # `shieldedEndDate`: solo los devuelve la plantilla individual. Fiarse del agregado da
    # "blindados: ninguno" con alguno blindado, un falso negativo que lleva a creerte
    # desprotegido o a intentar un clausulazo contra alguien blindado (400). Por eso la
    # plantilla propia se pide aparte: cuesta 1 petición más y evita decidir con datos
    # incompletos.
    propia = await protegido(api().plantilla(tok, liga, yo))
    mio = {**agregado, "players": propia.get("players", agregado.get("players", []))}

    cerrados = await _puntos_cerrados()
    gastado, n_ops = await gasto_de_hoy()
    ahora = dt.datetime.now(dt.UTC)

    jugadores = []
    for p in mio.get("players", []):
        pm = p.get("playerMaster", {})
        valor = int(pm.get("marketValue") or 1)
        clausula = int(p.get("buyoutClause") or 0)
        lock = p.get("buyoutClauseLockedEndTime") or ""
        protegido_hasta = None
        if lock:
            try:
                protegido_hasta = lock if dt.datetime.fromisoformat(lock) > ahora else None
            except ValueError:
                pass
        jugadores.append({
            "nombre": pm.get("nickname"),
            "posicion": POS.get(pm.get("positionId"), "?"),
            "player_team_id": p.get("playerTeamId"),
            "valor": valor,
            "clausula": clausula,
            "ratio": round(clausula / valor, 3) if valor else None,
            "puntos_temporada": _mejor_total(pm, cerrados),
            "estado": pm.get("playerStatus"),
            "blindado": bool(p.get("isShielded")),
            "blindado_hasta": p.get("shieldedEndDate"),
            "protegido_hasta": protegido_hasta,
        })

    # Las pujas y ofertas vivas se apuntan en `teamInvestment` y no bajan de
    # `teamMoney`. El agregado de la liga no trae ese campo, así que se pide
    # `/teams/{id}/money`: sin él, con una puja abierta la interfaz enseña más dinero del
    # que hay y se puede comprometer dos veces el mismo. Con `teamInvestment` a 0 el fallo
    # queda latente, que es lo que lo hace fácil de pasar por alto.
    dinero = await protegido(cliente().get(f"{CMP}/teams/{yo}/money", ttl=120, auth=tok))
    caja = int(dinero.get("teamMoney") or mio.get("teamMoney") or 0)
    comprometido = int(dinero.get("teamInvestment") or 0)
    libre = caja - comprometido
    return {
        "sistema": {
            "modo": modo(),
            # Fecha del fichero de interfaz más reciente. Si el menú enseña una fecha
            # anterior a un despliegue que se acaba de hacer, el navegador está usando
            # una copia cacheada.
            "web_desplegada": _web_desplegada(),
            "peticiones_al_origen_hoy": cliente().contador.origen,
            "servidas_de_cache_hoy": cliente().contador.cache,
            "bloqueado_por_waf": cliente().bloqueado,
        },
        "jornada": {
            "numero": semana.get("weekNumber"),
            "en_juego": semana.get("isLive"),
            "abre": semana.get("openingWeekDate"),
            "cierra": semana.get("closingWeekDate"),
            "siguiente": semana.get("nextWeek"),
        },
        "caja": {
            # `total` es el dinero LIBRE, ya descontado lo comprometido en pujas.
            "total": libre,
            "en_banco": caja,
            "comprometido_en_pujas": comprometido,
            "disponible_tras_reserva": libre - LIMITES.reserva_intocable,
            "reserva_intocable": LIMITES.reserva_intocable,
            "gastado_hoy": gastado,
            "operaciones_hoy": n_ops,
            "tope_diario": LIMITES.max_gasto_diario,
        },
        # El nombre de la liga y el del equipo salen de aquí, nunca escritos en la
        # interfaz: un valor de una instalación concreta metido en el código es una fuga,
        # y además le enseña a cualquier otro el nombre de una liga que no es la suya.
        "liga": {
            "nombre": (await _identidad()).get("nombre_liga") or "",
            "nombre_equipo": (mio.get("manager") or {}).get("managerName")
                             or mio.get("name") or "",
        },
        "equipo": {
            "puntos_totales": mio.get("teamPoints"),
            # Si esto marca 0 con jugadores que ya han puntuado, el once está
            # incompleto y la jornada no cuenta.
            "puntos_jornada_actual": mio.get("fixturePoints"),
            "posicion": mio.get("position"),
            "posicion_anterior": mio.get("previousPosition"),
            "valor_plantilla": mio.get("teamValue"),
            "num_jugadores": len(mio.get("players", [])),
        },
        "clasificacion": sorted(
            [{"manager": (t.get("manager") or {}).get("managerName"),
              # Hace falta para abrir su plantilla desde la clasificación.
              "team_id": str(t.get("id")),
              # El tablón identifica a la gente por `manager_id` (`user1Id`, `user2Id`),
              # que no es el `team_id`. Sin este campo la interfaz no puede poner nombre
              # a un evento y tendría que llevar la lista escrita en el código, que es
              # falso en cuanto lo instala otro.
              "manager_id": _manager_id(t),
              "puntos": t.get("teamPoints"),
              "puntos_jornada": t.get("fixturePoints"),
              "valor_plantilla": t.get("teamValue"),
              "num_jugadores": len(t.get("players", [])),
              "es_mio": str(t.get("id")) == str(yo)} for t in equipos],
            key=lambda x: -(x["puntos"] or 0)),
        "plantilla": sorted(jugadores, key=lambda j: (j["posicion"], -(j["puntos_temporada"] or 0))),
        "blindados": [j["nombre"] for j in jugadores if j["blindado"]],
    }
