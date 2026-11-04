"""`ClienteViaAPI`: el worker lee por la API en vez de ir a LaLiga.

Cada `ClienteEducado` tiene su token bucket, así que dos procesos con cliente propio no
comparten el límite de 0,5 req/s ni el contador diario. Aquí las lecturas del worker pasan
por el cliente único de la API y comparten su caché.

Escrituras y fallback van por el cliente directo: las escrituras porque los frenos están
en `/accion/*`. Misma firma que `ClienteEducado`, los jobs no saben cuál tienen delante, y
los desvíos se cuentan o un fallback permanente pasa desapercibido.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from urllib.parse import parse_qsl

import httpx

from .. import entorno as _env
from .http import ClienteEducado

log = logging.getLogger("coconut.clients.via_api")

# Dentro de la red de Compose la API es `api:8000`. Fuera (pruebas a mano) vale la de VPN.
BASE_API = _env.var("API_INTERNA", "http://api:8000")

# Prefijo que `FantasyAPI._p` antepone a todo; se recorta para hablar con `/puente`. Es
# `/v1/competition/`, sin `/api`: eso lo añade el transporte. Con el prefijo mal escrito
# ninguna ruta casa y todo cae al fallback en silencio; por eso se cuentan los desvíos.
_PREFIJO = "/v1/competition/"


def clave_servicio() -> dict[str, str]:
    """Cabecera de identidad del worker. Vacía si no está configurada, que con
    `COCONUT_AUTH=0` es lo normal."""
    c = _env.var("CLAVE_SERVICIO", "")
    return {"X-Clave-Servicio": c} if c else {}


def _clave_idempotencia(nombre: str, cuerpo: dict | None) -> str:
    """Identifica la OPERACIÓN, no la llamada.

    `/accion/*` cachea el resultado por esta cabecera durante una hora y devuelve
    `{"idempotente": true, …}` si la repites. Con una clave aleatoria por llamada esa
    protección no se activa nunca: la cabecera viaja, el servidor la honra y el cliente
    garantiza que no coincida, que es peor que no mandarla porque parece que cubre algo.

    Importa porque los jobs recurrentes vuelven a pasar por aquí: `job_league` cada 30
    min, la guardia del once cada hora, y el piloto propone lo mismo mientras la
    situación no cambie. Dos clausulazos idénticos en una hora son la cláusula pagada
    dos veces.

    Derivarla del cuerpo la hace estable sin dejar de distinguir operaciones: si cambia
    el jugador o el importe, cambia la clave. Y si la primera lanzó, no se cachea nada,
    así que un reintento tras un fallo sigue ejecutándose.
    """
    material = json.dumps({"accion": nombre, "cuerpo": cuerpo}, sort_keys=True,
                          ensure_ascii=False, default=str)
    return hashlib.sha256(material.encode()).hexdigest()[:32]


async def accion(nombre: str, cuerpo: dict, *, params: dict | None = None,
                 base: str | None = None, idempotencia: str | None = None) -> Any:
    """Ejecuta una acción por `/accion/*`, no contra LaLiga.

    Es por donde escribe el worker: esos endpoints traen los frenos de `politica.py`, los
    locks y la revalidación previa. Un job con cliente propio se los salta.

    Lanza si la API no responde, sin reintentar. Decide el que llama: para la alineación,
    mejor la vía vieja que no alinear.

    `idempotencia` solo se pasa para forzar que dos llamadas de cuerpo idéntico cuenten
    como operaciones distintas. Por defecto se deriva del cuerpo, que es lo que se quiere
    casi siempre.
    """
    url = f"{(base or BASE_API).rstrip('/')}/accion/{nombre}"
    clave = idempotencia or _clave_idempotencia(nombre, cuerpo)
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(url, json=cuerpo, params=params or {},
                         headers={**clave_servicio(), "idempotency-key": clave})
        r.raise_for_status()
        return r.json()


def activo() -> bool:
    """Interruptor. Apagado por defecto: se enciende job a job, midiendo."""
    return _env.var("WORKER_VIA_API", "0") not in ("0", "", "no", "false")


class ClienteViaAPI:
    """Mismo contrato que `ClienteEducado`, pero por la API."""

    def __init__(self, base: str | None = None):
        self.base = (base or BASE_API).rstrip("/")
        self._http: httpx.AsyncClient | None = None
        self._directo: ClienteEducado | None = None
        self.desvios = 0          # cuántas veces hubo que caer al cliente directo
        # `_con_jitter` registra `http.contador.origen`. Aquí ese número es cuántas se
        # hicieron directas: las que van por el puente ya las cuenta la API, y sumarlas
        # dos veces daría un consumo inflado.
        self.contador = _Contador(self)

    async def __aenter__(self) -> ClienteViaAPI:
        self._http = httpx.AsyncClient(timeout=45)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._http:
            await self._http.aclose()
        if self._directo:
            await self._directo.__aexit__(*exc)
        self._http = None
        self._directo = None

    async def _fallback(self) -> ClienteEducado:
        """Abre el cliente directo solo si de verdad hace falta, y lo reutiliza."""
        if self._directo is None:
            self._directo = ClienteEducado()
            await self._directo.__aenter__()
            log.warning("la API no respondió: se sigue con el cliente directo")
        self.desvios += 1
        return self._directo

    @staticmethod
    def _a_puente(path: str) -> str | None:
        """`/v1/competition/1/week/current` -> `week/current`. `None` si no encaja."""
        if not path.startswith(_PREFIJO):
            return None
        resto = path[len(_PREFIJO):]
        return resto.split("/", 1)[1] if "/" in resto else None

    async def get(self, path: str, *, ttl: int = 300, auth: str | None = None,
                  **kw: Any) -> Any:
        sufijo = self._a_puente(path)
        if sufijo and self._http is not None:
            # El query se separa del sufijo y viaja en `params`: httpx reemplaza el query
            # string cuando recibe `params`, así que pegado a la URL se pierde y
            # `calendar?weekNumber=5` llegaría como `calendar`, con la jornada equivocada.
            sufijo, _, qs = sufijo.partition("?")
            consulta: dict[str, Any] = {"ttl": ttl, **dict(parse_qsl(qs)),
                                        **(kw.get("params") or {})}
            try:
                r = await self._http.get(f"{self.base}/puente/{sufijo}", params=consulta)
                if r.status_code == 200:
                    return r.json()
                # Un 4xx del puente es una respuesta legítima de LaLiga (403 al leer
                # ofertas ajenas, 404 de una ruta que no existe). Repetirlo por el
                # cliente directo solo gastaría otra petición para el mismo error.
                if 400 <= r.status_code < 500:
                    r.raise_for_status()
                log.warning("puente devolvió %s en %s; se reintenta directo",
                            r.status_code, sufijo)
            except httpx.HTTPStatusError:
                raise
            except Exception as e:
                log.warning("puente no disponible (%s); se sigue directo", type(e).__name__)
        return await (await self._fallback()).get(path, ttl=ttl, auth=auth, **kw)

    async def escribe(self, metodo: str, path: str, **kw: Any) -> Any:
        """Las escrituras no pasan por el puente: van por el cliente directo, que es quien
        respeta el "sin reintentos" y la parada por WAF. El sitio para escribir con frenos
        es `/accion/*`, no un proxy."""
        return await (await self._fallback()).escribe(metodo, path, **kw)

    def __getattr__(self, nombre: str) -> Any:
        """Cualquier otra cosa (contadores, kill switch…) la atiende el directo."""
        if nombre.startswith("_") or nombre == "contador":
            raise AttributeError(nombre)

        async def delegado(*a, **k):
            return await getattr(await self._fallback(), nombre)(*a, **k)
        return delegado


class _Contador:
    """Expone `.origen` como el de `ClienteEducado`, para no tocar quien lo lee."""

    def __init__(self, dueño: ClienteViaAPI):
        self._dueño = dueño

    @property
    def origen(self) -> int:
        return self._dueño.desvios


def cliente_lectura() -> Any:
    """Lo que deben usar los jobs de SOLO LECTURA. Un único sitio decide el transporte."""
    return ClienteViaAPI() if activo() else ClienteEducado()
