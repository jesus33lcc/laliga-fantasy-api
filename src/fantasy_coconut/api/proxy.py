"""`/puente/{ruta}`: el único camino hacia LaLiga.

El problema que resuelve

Cada `ClienteEducado` tiene su propio token bucket, así que dos procesos con cliente
propio no comparten el límite de 0,5 req/s: el worker puede estar en su ciclo de liga
mientras alguien abre Coconut, y nadie cuenta esas peticiones juntas. El contador
"peticiones al origen hoy" tampoco diría la verdad, porque solo vería las de la API.

Y una cuenta cerrada por abusar de la API deja el proyecto sin nada que leer.

Esto reenvía una ruta cualquiera de LaLiga por el cliente compartido. Con el worker
apuntando aquí hay un solo token bucket, una sola caché y un solo contador.

Por qué solo GET

Un proxy que aceptara escrituras sería un agujero: cualquiera con acceso a la API podría
pujar o clausular saltándose `politica.py`, los locks y la revalidación previa. Las
escrituras siguen yendo por `/accion/*`, que es donde viven los frenos.

Es solo-VPN, pero eso es una capa sola; no conviene que además sea una capa ancha.

Qué no hace

No interpreta lo que devuelve. Es transporte: la misma respuesta que daría LaLiga, ya
autenticada y ya cacheada. Los endpoints con criterio propio siguen en `puente.py`.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Query
from starlette.requests import Request

from .deps import api, bearer, cliente, protegido

log = logging.getLogger("coconut.api.proxy")
router = APIRouter(prefix="/puente", tags=["puente"])

# Un proxy abierto a cualquier host sería un SSRF de manual. Solo se permiten rutas
# dentro de la competición, que es lo único que consume el worker.
PREFIJOS = ("players", "week", "calendar", "leagues", "league", "teams", "team",
            "player", "daily-reward", "config", "ideal", "ranking", "stats")


@router.get("/{ruta:path}", summary="Reenvía una ruta de LaLiga por el cliente compartido")
async def puente(request: Request, ruta: str,
                 ttl: int = Query(300, ge=0, le=28800,
                                  description="Segundos de caché. 0 = sin caché")):
    """Solo lectura. El `ttl` lo decide quien llama porque no todo caduca igual:
    el maestro de jugadores aguanta 8 h y el mercado, minutos.

    El query string se reenvía explícitamente. FastAPI no lo mete en `{ruta:path}`
    (eso solo captura el camino), así que sin este reenvío `/puente/calendar?weekNumber=5`
    llega como `calendar` a secas y LaLiga contesta con su jornada por defecto: no falla,
    devuelve otra cosa.
    """
    limpia = ruta.strip("/")
    if not limpia:
        raise HTTPException(400, "falta la ruta")
    if ".." in limpia or limpia.startswith(("http://", "https://", "//")):
        raise HTTPException(400, "ruta no permitida")
    if limpia.split("/")[0] not in PREFIJOS:
        raise HTTPException(
            403, f"prefijo no permitido: {limpia.split('/')[0]!r}. "
                 f"Permitidos: {', '.join(sorted(PREFIJOS))}")

    tok = await bearer()
    # `_p` antepone el prefijo de competición, que es lo mismo que hace el worker al
    # construir sus rutas: así lo que se le pasa aquí es idéntico a lo de siempre.
    ruta_api = api()._p(f"/{limpia}")
    # Todo el query menos el `ttl`, que es del cliente y no de LaLiga.
    extra = {k: v for k, v in request.query_params.items() if k != "ttl"}
    log.debug("puente %s %s (ttl %ss)", ruta_api, extra or "", ttl)
    try:
        return await protegido(cliente().get(ruta_api, ttl=ttl, params=extra or None,
                                             auth=tok))
    except httpx.HTTPStatusError as e:
        # Un 4xx de LaLiga es una respuesta, no una avería: "ya cobraste la recompensa"
        # (400), "esas ofertas no son tuyas" (403), "esa ruta no existe" (404). Dejarlo
        # escapar lo convierte en un 500, que dice "el sistema está roto" y manda a mirar
        # el log para nada.
        codigo = e.response.status_code
        if 400 <= codigo < 500:
            try:
                detalle = e.response.json()
            except Exception:
                detalle = {"message": e.response.text[:300]}
            raise HTTPException(codigo, detalle) from e
        raise
