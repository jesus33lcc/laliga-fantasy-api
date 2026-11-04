"""Transporte HTTP educado: toda petición a LaLiga pasa por aquí.

Un bloqueo a mitad de temporada deja el proyecto sin datos, así que consume menos que la
app oficial. Cuatro defensas, por orden:

  1. Caché sembrada del `Cache-Control` que devuelve la propia API.
  2. Token bucket de 0,5 req/s y concurrencia 1.
  3. 403 inesperado: parada total, porque reintentarlo vuelve permanente el bloqueo del
     WAF. Las rutas de `_403_ESPERABLE` quedan fuera: ahí el 403 es de negocio.
  4. 429: enfriamiento global, respetando `Retry-After`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

import httpx

from .. import entorno as _env
from ..config import settings

log = logging.getLogger("coconut.http")


class BloqueoDetectado(Exception):
    """403 del WAF. No se reintenta: se para y se avisa."""


class ParadaManual(Exception):
    """Existe el fichero de parada o COCONUT_STOP=1."""


class TokenBucket:
    """Cubo de tokens asíncrono. `rate` en tokens/segundo.

    `ajustar()` cambia el ritmo en caliente, sin recrear el contenedor.
    """

    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def ajustar(self, rate: float) -> bool:
        """Cambia el ritmo. Devuelve si de verdad cambió, para poder registrarlo.

        No toca los tokens acumulados: subir el ritmo no debe regalar una ráfaga de
        golpe, que es justo lo que un WAF mira.
        """
        nuevo = max(0.05, float(rate))
        if abs(nuevo - self.rate) < 1e-9:
            return False
        self.rate = nuevo
        return True

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                ahora = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (ahora - self._updated) * self.rate
                )
                self._updated = ahora
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)


@dataclass
class _Entrada:
    valor: Any
    expira: float


@dataclass
class Contador:
    """Presupuesto diario de peticiones. Si se dispara, es que falta caché."""

    dia: str = ""
    origen: int = 0
    cache: int = 0

    def anota(self, desde_cache: bool) -> None:
        hoy = time.strftime("%Y-%m-%d")
        if hoy != self.dia:
            self.dia, self.origen, self.cache = hoy, 0, 0
        if desde_cache:
            self.cache += 1
        else:
            self.origen += 1


class ClienteEducado:
    """Un único cliente para todo el proceso: conexión reutilizada y estado compartido."""

    _MAX_EDAD_CACHE = 8 * 3600
    _REINTENTABLES = {429, 500, 502, 503, 504}
    # Rutas donde un 403 es respuesta normal de la API: pedir algo de otro manager.
    # Tratarlo como bloqueo pararía el sistema por una consulta rutinaria. El resto de
    # 403 sí se toman como bloqueo del WAF.
    #
    # El criterio para entrar aquí: que la ruta lleve el id de otro equipo o de otra
    # ficha. LaLiga reserva esos datos al dueño y contesta 403, que es una respuesta, no
    # un corte. Medido con `/money`: `/leagues/{lg}/teams` trae `teamMoney` en `null`
    # para los tres rivales y con valor solo para el propio.
    _403_ESPERABLE = ("/playerTeam/", "/player-team/", "/money",
                      "/favourite-players", "/check-daily-reward")

    def __init__(self) -> None:
        self._cliente: httpx.AsyncClient | None = None
        self._bucket = TokenBucket(settings.rate_limit_rps, settings.rate_limit_burst)
        # Marca de la última lectura del ritmo guardado en Ajustes.
        self._ritmo_visto = 0.0
        self._cache: dict[str, _Entrada] = {}
        self._en_vuelo: dict[str, asyncio.Task] = {}
        self._enfriamiento_hasta = 0.0
        self.contador = Contador()
        self.bloqueado = False
        self.motivo_bloqueo = ""

    async def __aenter__(self) -> ClienteEducado:
        self._cliente = httpx.AsyncClient(
            base_url=settings.fantasy_base_url.rstrip("/") + "/api",
            http2=True,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2,
                                keepalive_expiry=60.0),
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=30.0),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
                "x-lang": "es",
                "x-app": "2",
                "User-Agent": settings.user_agent,
            },
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._cliente:
            await self._cliente.aclose()
            self._cliente = None

    # ---------- guardas ----------

    def _comprueba_parada(self) -> None:
        if _env.var("STOP") == "1" or os.path.exists(settings.stop_file):
            raise ParadaManual(
                f"parada activa (COCONUT_STOP o {settings.stop_file}); no se hacen peticiones"
            )
        if self.bloqueado:
            raise BloqueoDetectado("circuito abierto tras un 403; hace falta revisión manual")

    async def _refrescar_ritmo(self) -> None:
        """Aplica el ritmo guardado en Ajustes, si cambió.

        Como mucho una vez por minuto: es una lectura de base de datos y va en el camino de
        cada petición. Si la base no responde se sigue con el ritmo que hubiera, que
        quedarse sin poder pedir por no leer una preferencia sería cambiar un ajuste por
        una avería.
        """
        ahora = time.monotonic()
        if ahora - self._ritmo_visto < 60:
            return
        self._ritmo_visto = ahora
        try:
            from ..ajustes import leer
            valor = float((await leer()).get("rate_limit_rps") or 0)
        except Exception:
            return
        if valor > 0 and self._bucket.ajustar(valor):
            log.warning("ritmo hacia LaLiga ajustado a %.2f req/s "
                        "(el cliente oficial usa 1,0)", valor)

    def _ttl_de_cabeceras(self, resp: httpx.Response, defecto: float) -> float:
        """Manda el `Cache-Control` de la respuesta, suba o baje el `defecto`. Este solo
        se usa si no hay cabecera o dice `no-cache`."""
        cc = resp.headers.get("cache-control", "")
        if "no-cache" in cc or "no-store" in cc:
            return defecto
        m = re.search(r"max-age=(\d+)", cc)
        if m:
            return min(float(m.group(1)), self._MAX_EDAD_CACHE)
        return defecto

    # ---------- petición ----------

    async def get(self, path: str, *, ttl: float = 300.0,
                  params: dict[str, Any] | None = None,
                  auth: str | None = None) -> Any:
        """GET cacheado. El `ttl` es el valor por defecto, no un mínimo: si la respuesta
        trae `Cache-Control`, manda ese.

        El query string se admite dentro del `path` y se normaliza aquí, que es lo natural
        al copiar una ruta. Pegado, httpx lo descarta al mezclar los `params`:
        `/calendar?weekNumber=5` devolvería la jornada en curso, sin error y sin aviso.
        """
        self._comprueba_parada()
        if "?" in path:
            path, _, qs = path.partition("?")
            # Lo explícito de `params` gana sobre lo que venga pegado al path.
            params = {**dict(parse_qsl(qs)), **(params or {})}
        clave = path + "?" + json.dumps(params or {}, sort_keys=True)

        # `ttl=0` es "sin caché" y se mira antes que la caché. La caducidad de una entrada
        # la fija el `Cache-Control` del momento en que se guarda, no el `ttl` de esta
        # llamada: sin
        # este corte, un `ttl=0` seguiría devolviendo la copia vieja. Hace falta justo
        # después de escribir, que es cuando la caché confirma el estado anterior.
        if ttl > 0:
            entrada = self._cache.get(clave)
            if entrada and entrada.expira > time.time():
                self.contador.anota(desde_cache=True)
                return entrada.valor

        # Coalescencia: si ya hay una petición idéntica en vuelo, se espera a esa.
        if clave in self._en_vuelo:
            return await asyncio.shield(self._en_vuelo[clave])

        tarea = asyncio.create_task(self._buscar(clave, path, params, ttl, auth))
        self._en_vuelo[clave] = tarea
        try:
            return await tarea
        finally:
            self._en_vuelo.pop(clave, None)

    async def _buscar(self, clave: str, path: str, params: dict[str, Any] | None,
                      ttl: float, auth: str | None) -> Any:
        """Pide y guarda. Ojo a la asimetría: `ttl=0` se salta la LECTURA de la caché
        (lo hace `get()`), pero aquí se ESCRIBE igual.

        Cuánto dura esa entrada lo decide `Cache-Control`, no el `ttl` de la llamada: en
        una ruta cacheable, un `ttl=0` deja copia para lo que diga LaLiga, y la siguiente
        lectura con `ttl` normal se la encuentra. En una ruta sin esa cabecera, el defecto
        es 0 y la entrada nace vencida.

        Importa al escribir y releer seguido: quien lee con `ttl=0` obtiene la verdad,
        pero deja servida a la siguiente lectura. Si las dos tienen que describir el mismo
        instante, ahí es donde se cuelan edades distintas.
        """
        resp = await self._peticion("GET", path, params=params, auth=auth)
        datos = resp.json()
        self._cache[clave] = _Entrada(datos, time.time() + self._ttl_de_cabeceras(resp, ttl))
        return datos

    async def escribe(self, metodo: str, path: str, *, json_body: Any = None,
                      auth: str | None = None) -> httpx.Response:
        """POST/PUT/DELETE. Sin reintentos: no son idempotentes (duplicaría una puja)."""
        self._comprueba_parada()
        return await self._peticion(metodo, path, json_body=json_body, auth=auth,
                                    reintentar=False)

    async def _peticion(self, metodo: str, path: str, *, params: dict[str, Any] | None = None,
                        json_body: Any = None, auth: str | None = None,
                        reintentar: bool = True) -> httpx.Response:
        intentos = settings.max_reintentos if reintentar else 1
        for intento in range(1, intentos + 1):
            espera = self._enfriamiento_hasta - time.monotonic()
            if espera > 0:
                log.warning("en enfriamiento, esperando %.0fs", espera)
                await asyncio.sleep(espera)

            await self._refrescar_ritmo()
            await self._bucket.acquire()
            self._comprueba_parada()

            cabeceras = {}
            if auth:
                cabeceras["Authorization"] = f"Bearer {auth}"
            if json_body is not None:
                cabeceras["Content-Type"] = "application/json"

            t0 = time.monotonic()
            try:
                assert self._cliente is not None, "usar dentro de 'async with'"
                resp = await self._cliente.request(
                    metodo, path,
                    params=params,
                    json=json_body, headers=cabeceras or None,
                )
            except httpx.TransportError as e:
                if intento >= intentos:
                    raise
                await asyncio.sleep(self._espera(intento, None))
                log.warning("red falló (%s), reintento %d/%d", e, intento, intentos)
                continue

            ms = (time.monotonic() - t0) * 1000
            self.contador.anota(desde_cache=False)
            log.info("%s %s -> %d (%.0f ms) [origen hoy: %d]",
                     metodo, path, resp.status_code, ms, self.contador.origen)

            if resp.status_code == 403:
                if any(t in path for t in self._403_ESPERABLE):
                    # Lógica de negocio: la ficha no es tuya. Ni se para ni se reintenta.
                    log.info("403 esperable en %s (ficha ajena); no es un bloqueo", path)
                    resp.raise_for_status()
                # No es transitorio: WAF o revocación. Se para todo.
                self.bloqueado = True
                self.motivo_bloqueo = f"403 en {path} ({time.strftime('%d-%b %H:%M')})"
                # `log.error` y no `warning`: esto para el sistema entero, y sin dejar
                # dicho qué ruta lo abrió hay que reconstruirlo a mano.
                log.error("CIRCUITO ABIERTO: %s. Ninguna petición saldrá hasta "
                          "reiniciar o llamar a POST /salud/reactivar", self.motivo_bloqueo)
                raise BloqueoDetectado(
                    f"403 en {path}. Sistema detenido para no empeorar el bloqueo."
                )

            if resp.status_code == 429:
                pausa = self._retry_after(resp) or 300.0
                self._enfriamiento_hasta = time.monotonic() + pausa
                log.error("429 en %s -> enfriamiento global de %.0fs", path, pausa)
                if intento >= intentos:
                    resp.raise_for_status()
                continue

            if resp.status_code in self._REINTENTABLES:
                if intento >= intentos:
                    resp.raise_for_status()
                await asyncio.sleep(self._espera(intento, resp))
                continue

            if resp.status_code >= 400:
                resp.raise_for_status()

            return resp

        raise RuntimeError(f"agotados los reintentos en {path}")

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        ra = resp.headers.get("retry-after")
        if not ra:
            return None
        try:
            return min(float(ra), 900.0)
        except ValueError:
            return None

    def _espera(self, intento: int, resp: httpx.Response | None) -> float:
        if resp is not None:
            ra = self._retry_after(resp)
            if ra:
                return ra
        # Full jitter: reparte los reintentos en vez de sincronizarlos.
        return random.uniform(0, min(120.0, 2.0 * 2 ** intento))
