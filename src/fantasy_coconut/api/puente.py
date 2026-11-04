"""Puente `/fantasy/*`: la API de LaLiga tal cual, pero ya autenticada.

Todo lo conocido de la API oficial, expuesto sin tener que pelear con el token ni
levantar un contenedor. Es el sustituto de `docker compose run ... python -c "..."`.

Los TTL de caché los pone el transporte a partir del `Cache-Control` real de LaLiga, así
que pedir esto en bucle no multiplica las peticiones al origen.

`GET /fantasy/teams` devuelve todas las plantillas en una sola petición, y trae campos
que no están en el endpoint individual: `fixturePoints` (puntos de la jornada EN curso),
`position`/`previousPosition`, `loanedPlayers` y `banned`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..config import settings
from .deps import api, bearer, cliente, protegido

router = APIRouter(prefix="/fantasy", tags=["puente"])

CMP = "/v1/competition/1"


def _liga() -> str:
    return settings.fantasy_league_id


def _yo() -> str:
    return settings.fantasy_team_id


# ---------- público (sin token) ----------

@router.get("/players", summary="Maestro de jugadores con su valor de mercado")
async def players():
    return await protegido(api().jugadores())


@router.get("/week/current", summary="Jornada actual: apertura, cierre y si está viva")
async def week_current():
    return await protegido(api().jornada_actual())


@router.get("/calendar/{jornada}", summary="Los 10 partidos de una jornada")
async def calendar(jornada: int):
    return await protegido(api().calendario(jornada))


# ---------- liga ----------

@router.get("/standing", summary="Clasificación")
async def standing():
    return await protegido(api().clasificacion(await bearer(), _liga()))


@router.get("/teams", summary="Todas las plantillas + fixturePoints en UNA petición")
async def teams():
    """Incluye `fixturePoints`: los puntos de la jornada en curso de cada manager.

    Sirve para detectar una jornada anulada: si el resto de la liga suma y tu equipo
    sigue a 0 con la jornada jugada, la alineación está incompleta.

    Pero es INCOMPLETO: no trae `isShielded` ni `shieldedEndDate`. Solo los
    devuelve la plantilla individual (`/fantasy/team/{id}`). Para cualquier decisión sobre
    blindajes (propios o rivales) hay que usar el individual; si no, todo el mundo parece
    desprotegido. Tampoco trae `managerId`.
    """
    return await protegido(
        cliente().get(f"{CMP}/leagues/{_liga()}/teams", ttl=300, auth=await bearer()))


@router.get("/team/{team_id}", summary="Plantilla de un manager (con cláusulas)")
async def team(team_id: str):
    return await protegido(api().plantilla(await bearer(), _liga(), team_id))


@router.get("/money", summary="teamMoney + teamInvestment (solo del equipo propio)")
async def money(team_id: str | None = None):
    return await protegido(api().saldo(await bearer(), team_id or _yo()))


@router.get("/activity", summary="Tablón de la liga (TODAS las páginas)")
async def activity(indice: int | None = Query(None, ge=0),
                   paginas: int = Query(8, ge=1, le=20)):
    """El tablón entero, no solo la primera página.

    `GET .../activity/{indice}` está paginado, y las páginas no tienen el mismo tamaño
    (29, 74, 50…). Pedir solo el índice 0 devuelve una fracción del tablón, y parece que
    en la liga no ha pasado nada más.

    Se recorren hasta que una vuelve vacía. No cuesta apenas: van cacheadas 5 min y son
    las mismas que ya pide la ingesta.

    Con `indice` se pide una sola página.
    """
    tok = await bearer()
    if indice is not None:
        return await protegido(api().actividad(tok, _liga(), indice))

    todo: list = []
    for pagina in range(paginas):
        pg = await protegido(api().actividad(tok, _liga(), pagina))
        if isinstance(pg, dict):
            pg = pg.get("data") or []
        if not pg:
            break
        todo.extend(pg)
    # De más reciente a más antiguo, que es como se lee un tablón.
    todo.sort(key=lambda e: str(e.get("createdAt") or ""), reverse=True)
    return todo


# ---------- mercado ----------

@router.get("/market", summary="Mercado: agentes libres y fichas de rivales")
async def market():
    return await protegido(api().mercado(await bearer(), _liga()))


@router.get("/offers/{player_team_id}", summary="Ofertas recibidas por un jugador propio")
async def offers(player_team_id: str):
    """Un 403 aquí significa "esa ficha no es tuya", no un bloqueo del WAF.

    Se captura a propósito: dejarlo escapar abriría el circuito de parada del transporte
    y detendría el sistema entero por una respuesta que es de lógica de negocio.
    """
    try:
        return await cliente().get(
            f"{CMP}/league/{_liga()}/playerTeam/{player_team_id}/offer",
            ttl=0, auth=await bearer())
    except Exception as e:
        if "403" in str(e):
            raise HTTPException(
                403, "esa ficha no es tuya (respuesta normal de LaLiga)") from e
        raise


# ---------- alineación ----------

@router.get("/lineup", summary="Once actual (el que se congelará en la próxima jornada)")
async def lineup(team_id: str | None = None):
    return await protegido(
        cliente().get(f"{CMP}/teams/{team_id or _yo()}/lineup", ttl=30, auth=await bearer()))


@router.get("/lineup/week/{jornada}", summary="Once CONGELADO de una jornada")
async def lineup_week(jornada: int, team_id: str | None = None):
    """El once que de verdad puntuó. Nunca uses `/lineup` para auditar una jornada:
    ese devuelve el actual, y lo que cuenta es el que había al abrirse la jornada."""
    return await protegido(
        cliente().get(f"{CMP}/teams/{team_id or _yo()}/lineup/week/{jornada}",
                      ttl=60, auth=await bearer()))


# ---------- jugador ----------

@router.get("/player/{player_id}", summary="Ficha con estadísticas POR JORNADA")
async def player(player_id: str):
    """`playerStats` trae el desglose de cada jornada: minutos, goles, asistencias,
    tarjetas, recuperaciones y `isInIdealFormation`. Ojo: la ruta es `player` en
    singular: `/players/{id}` da 404."""
    return await protegido(
        cliente().get(f"{CMP}/player/{player_id}", ttl=900, auth=await bearer()))
