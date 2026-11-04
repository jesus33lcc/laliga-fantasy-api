"""Mapa de la API de LaLiga Fantasy (temporada 26/27).

Rutas verificadas contra la API. Todo cuelga de `/v1/competition/{id}/...`; `/api/v3/...`
responde 404. Conviven `/leagues/` y `/league/` según el recurso: no es errata.

El `ttl` de cada método es el valor por defecto, no un mínimo: si la respuesta trae
`Cache-Control`, manda ese (`http._ttl_de_cabeceras`). Cada método devuelve el JSON crudo y
la ingesta lo guarda en `raw`, así que un cambio de esquema se reparsea hacia atrás.
"""

from __future__ import annotations

from typing import Any

from ..config import settings
from .http import ClienteEducado

CMP = "/v1/competition/{cid}"


class FantasyAPI:
    """Construye las rutas y pone el `ttl` por defecto de cada una. El resto lo hace el
    transporte."""

    def __init__(self, http: ClienteEducado, competicion: int | None = None):
        self.http = http
        self.cid = competicion or settings.competition_id

    def _p(self, sufijo: str) -> str:
        return CMP.format(cid=self.cid) + sufijo

    # ---------- público (sin token) ----------

    async def jugadores(self, ttl: int = 8 * 3600) -> list[dict[str, Any]]:
        """Maestro con el valor de mercado de todos. De aquí sale la serie de precios.

        El defecto son las 8 h que declara el `Cache-Control` de LaLiga, y por eso este
        maestro puede ir hasta 8 h por detrás en `position` y `status`. Para eso NO se usa
        `ttl=0`: de esos dos campos manda la plantilla, que `ingest/league.py` lee fresca.
        `ttl=0` queda para comprobar a mano qué está publicando LaLiga ahora mismo.
        """
        return await self.http.get(self._p("/players"), ttl=ttl)

    async def jornada_actual(self) -> dict[str, Any]:
        return await self.http.get(self._p("/week/current"), ttl=3600)

    async def calendario(self, jornada: int | None = None) -> Any:
        params = {"weekNumber": jornada} if jornada else None
        return await self.http.get(self._p("/calendar"), ttl=6 * 3600, params=params)

    # ---------- privado (requiere token) ----------

    async def usuario(self, token: str) -> dict[str, Any]:
        return await self.http.get("/v4/user/me", ttl=3600, auth=token)

    async def ligas(self, token: str) -> Any:
        return await self.http.get(self._p("/leagues"), ttl=24 * 3600, auth=token)

    async def clasificacion(self, token: str, liga: str) -> Any:
        return await self.http.get(self._p(f"/leagues/{liga}/standing"), ttl=900, auth=token)

    async def plantilla(self, token: str, liga: str, equipo: str,
                        ttl: int = 900) -> Any:
        """Plantilla de un manager, con la cláusula de cada jugador.

        Los 15 min por defecto valen para mirar, no para decidir. Para comprobar si un
        jugador sigue siendo tuyo, `ttl=0`: con una copia vieja se alinea a alguien ya
        clausulado.
        """
        return await self.http.get(self._p(f"/leagues/{liga}/teams/{equipo}"),
                                   ttl=ttl, auth=token)

    async def actividad(self, token: str, liga: str, indice: int = 0) -> Any:
        """El tablón: compras, ventas, clausulazos, blindajes y recompensas.

        Una bajada de cláusula no sale aquí, no hay tipo para eso en los 33 de
        `tipos_actividad`: se detecta comparando snapshots en `ingest/vigilante.py`.
        """
        return await self.http.get(self._p(f"/leagues/{liga}/activity/{indice}"),
                                   ttl=300, auth=token)

    async def mercado(self, token: str, liga: str) -> Any:
        return await self.http.get(self._p(f"/league/{liga}/market"), ttl=300, auth=token)

    async def saldo(self, token: str, equipo: str, ttl: int = 300) -> Any:
        """Saldo del equipo. Para autorizar un gasto, `ttl=0`.

        Con la caché por defecto, dos operaciones seguidas leen las dos el saldo previo a
        la primera y la segunda pasa los topes con dinero que ya no existe.
        """
        return await self.http.get(self._p(f"/teams/{equipo}/money"), ttl=ttl, auth=token)

    async def jugador(self, token: str, jugador: str, liga: str) -> Any:
        return await self.http.get(self._p(f"/player/{jugador}/league/{liga}"),
                                   ttl=1800, auth=token)
