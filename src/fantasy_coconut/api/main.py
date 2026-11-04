"""API Coconut: puente a LaLiga, informes de dominio y acciones.

Un único proceso, ya logueado y vivo, que evita el `docker compose run ... python -c
"..."` para todo lo interactivo. El `worker` sigue con la ingesta periódica y la
alineación automática de seguridad; esto le quita lo puntual.

Familias:
  · `/fantasy/*`: la API de LaLiga tal cual, ya autenticada (`puente.py`)
  · `/estado`: la foto operativa en una llamada (`estado.py`)
  · `/health`: sondeo del vigilante externo; 503 si algún invariante falla
  · histórico: lo que no está en la API de LaLiga y solo vive en la base local
  · `/informe/*`: los cruces de dominio · `/accion/*`: ejecutar · `/programar/*`: a una hora

Documentación viva en `/docs`, que FastAPI genera del propio código.
"""
import hashlib
import logging
import re
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .. import entorno
from ..db import Session
from ..ingest.market import biggest_movers
from ..models import clausulas
from ..salud import revisar
from ..tiempo import hace
from . import (
    accion,
    bloqueo,
    buscar,
    informe,
    programar,
    proxy,
    puente,
    seguimientos,
    sesion,
    tacticas,
    trabajos,
    trading,
    ventas,
)
from . import ajustes as ajustes_api
from . import estado as estado_mod
from . import login as login_laliga
from . import respaldo as respaldo_api
from .deps import ciclo_vida

app = FastAPI(
    title="LaLiga Fantasy Coconut",
    version="0.3",
    description="Puente e inteligencia sobre la API de LaLiga Fantasy. Interfaz en `/app`. Solo-VPN.",
    lifespan=ciclo_vida,
)

app.include_router(puente.router)
app.include_router(proxy.router)
app.include_router(buscar.router)
app.include_router(informe.router)
app.include_router(estado_mod.router)
app.include_router(sesion.router)
app.include_router(
    ajustes_api.router, dependencies=[Depends(sesion.exigir_usuario)])
app.include_router(
    bloqueo.router, dependencies=[Depends(sesion.exigir_usuario)])

# Lo que MUEVE DINERO o cambia estado va detrás de `exigir_usuario`. Con
# `COCONUT_AUTH=0` la dependencia deja pasar y la única puerta sigue siendo la
# VPN; en cuanto se encienda, estas familias quedan cerradas sin tener que tocar
# ni un endpoint.
app.include_router(accion.router, dependencies=[Depends(sesion.exigir_usuario)])
app.include_router(seguimientos.router, dependencies=[Depends(sesion.exigir_usuario)])
app.include_router(trabajos.router, dependencies=[Depends(sesion.exigir_usuario)])
# La copia lleva la liga entera y el token de GitHub: detrás del login, como
# todo lo que escribe. Con COCONUT_AUTH=0 la VPN sigue siendo la puerta.
app.include_router(respaldo_api.router, dependencies=[Depends(sesion.exigir_usuario)])
app.include_router(programar.router, dependencies=[Depends(sesion.exigir_usuario)])
# Solo lectura y registro: no compra nada. Aun así va detrás del login como
# el resto de lo que toca dinero, aunque sea para anotarlo.
app.include_router(trading.router, dependencies=[Depends(sesion.exigir_usuario)])
# El catálogo de tácticas: TODO es de lectura y nada ejecuta, pero describe operaciones
# de dinero al detalle, así que va detrás del login igual que el resto.
app.include_router(tacticas.router, dependencies=[Depends(sesion.exigir_usuario)])
# La política de venta decide qué se cobra solo y qué se trae para aprobar: es una
# regla sobre dinero aunque ella no mueva ninguno, así que va detrás del login.
app.include_router(ventas.router, dependencies=[Depends(sesion.exigir_usuario)])
# Conectar la cuenta de LaLiga. Escribe el token, así que va detrás del login de Coconut
# igual que lo que mueve dinero; con COCONUT_AUTH=0 la VPN es la única puerta.
app.include_router(login_laliga.router, dependencies=[Depends(sesion.exigir_usuario)])

# ---------- interfaz web ----------
# Mismo origen que la API a propósito: el navegador pide `/estado` con ruta relativa, no
# hay CORS que configurar y el id_token no sale de este proceso. La ruta se fija con
# `COCONUT_WEB` para el móvil, donde el paquete no vive en `repo/src`. Un montaje sin
# directorio se salta en silencio y la API arranca sin interfaz, así que se registra.
WEB = Path(entorno.var("WEB") or (Path(__file__).resolve().parents[3] / "web"))
if not WEB.is_dir():
    logging.getLogger("coconut.api").warning(
        "no encuentro la interfaz web en %s: la API funcionará pero /app dará 404. "
        "Fíjala con COCONUT_WEB si está en otro sitio (p. ej. dentro del APK).", WEB)
else:

    class WebSinCache(StaticFiles):
        """Sirve la app obligando a revalidar en cada carga.

        Sin esto, un despliegue no llega al navegador. `StaticFiles` manda `etag` y
        `last-modified` pero ningún `Cache-Control`, así que el navegador aplica su
        heurística y puede seguir usando el JS viejo *sin preguntar*. El síntoma despista
        del todo: se arregla un bug, se despliega, se verifica en headless (que siempre
        arranca sin caché) y en el dispositivo real sigue fallando, porque ejecuta el
        código anterior.

        `no-cache` no significa "no cachees": significa "cachea, pero pregunta antes
        de usarlo". Con el `etag` que ya se manda, la respuesta normal es un 304 sin
        cuerpo, así que no cuesta ancho de banda: solo un viaje de ida y vuelta.
        """

        async def get_response(self, path: str, scope):
            resp = await super().get_response(path, scope)

            # ---------- caché versionada ----------
            # Con `no-cache` a secas cada carga revalida los ~15 ficheros, y por VPN
            # desde el móvil se nota. El `?v=` tiene que coincidir con el hash del fichero
            # que se sirve; si no coincide se cae a `no-cache`, así que lo peor que puede
            # pasar es revalidar de más, nunca quedarse pegado a una copia vieja.
            v = _query(scope).get("v")
            if v and v == _huella(path):
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                resp.headers["Cache-Control"] = "no-cache, must-revalidate"
            return resp

    def _query(scope) -> dict[str, str]:
        from urllib.parse import parse_qs
        qs = (scope.get("query_string") or b"").decode("latin-1")
        return {k: val[0] for k, val in parse_qs(qs).items()}

    # Los assets que cuelgan del HTML y a los que se les pone `?v=`. Compilado una vez.
    RE_ASSET = re.compile(r'(href=|src=)"((?:css|js)/[^"?]+)"')

    _HUELLAS: dict[str, tuple[float, str]] = {}

    def _huella(rel: str) -> str | None:
        """Hash corto del CONTENIDO, recalculado solo si cambia el `mtime`.

        Se usa el contenido y no el `mtime` a secas porque el mtime cambia al copiar
        ficheros sin que cambie nada dentro, y eso invalidaría la caché sin motivo.
        """
        f = WEB / rel
        try:
            mt = f.stat().st_mtime
        except OSError:
            return None
        cacheado = _HUELLAS.get(rel)
        if cacheado and cacheado[0] == mt:
            return cacheado[1]
        try:
            h = hashlib.md5(f.read_bytes()).hexdigest()[:10]
        except OSError:
            return None
        _HUELLAS[rel] = (mt, h)
        return h

    class WebVersionada(WebSinCache):
        """Reescribe `index.html` al vuelo para colgar `?v=<hash>` de cada asset.

        Se hace al servir, no en el fichero, para que el repo no lleve URLs generadas:
        editar un CSS y recargar sigue funcionando sin ningún paso de compilación, que es
        lo que hace usable este proyecto sin bundler.

        Los `import` internos de los módulos JS (`app.js` `./vistas.js`) los resuelve
        el navegador y no pasan por aquí, así que esos siguen revalidando. Se versiona
        lo que cuelga del HTML: los 5 CSS y `app.js`. Reescribir los imports exigiría
        parsear los módulos, y un error ahí deja la web en blanco: no compensa.
        """

        async def get_response(self, path: str, scope):
            if path not in ("", ".", "index.html"):
                return await super().get_response(path, scope)
            # `index.html` se lee en cada petición y así se queda. Bloquea el bucle unos
            # microsegundos sobre 5 KB que el sistema ya tiene en caché. Cachearlo por
            # `mtime` rompería el versionado: el `mtime` de `index.html` no cambia al
            # editar un CSS, así que se serviría el `?v=` viejo.
            try:
                html = (WEB / "index.html").read_text(encoding="utf-8")
            except OSError:
                return await super().get_response(path, scope)

            def marcar(m):
                rel = m.group(2)
                h = _huella(rel)
                return f'{m.group(1)}"{rel}?v={h}"' if h else m.group(0)

            html = RE_ASSET.sub(marcar, html)
            return Response(html, media_type="text/html",
                            headers={"Cache-Control": "no-cache, must-revalidate"})

    app.mount("/app", WebVersionada(directory=WEB, html=True), name="web")
    logging.getLogger("coconut.api").info("interfaz web servida desde %s", WEB)

    @app.get("/", include_in_schema=False)
    async def raiz():
        return RedirectResponse("/app/")


@app.get("/health", tags=["estado"],
         summary="Salud e invariantes de datos: 503 si algo falla")
async def health(response: Response):
    """Sondeo del vigilante externo. Devuelve 503 si algo va mal, para que el cron
    de fuera lo detecte sin tener que interpretar el cuerpo."""
    inf = await revisar()
    if not inf.ok:
        response.status_code = 503
    return {
        "ok": inf.ok,
        "fallos": [{"que": c.nombre, "detalle": c.detalle} for c in inf.fallos],
        "chequeos": {c.nombre: c.detalle for c in inf.chequeos},
    }


@app.post("/salud/reactivar", tags=["estado"])
async def reactivar_circuito(response: Response):
    """Cierra el circuito del transporte tras un 403, comprobando antes con LaLiga.

    Por qué existe

    `ClienteEducado` se para en seco ante un 403, porque reintentarlo es lo que convierte
    un bloqueo temporal en permanente. Sin este endpoint, cerrar el circuito exige
    reiniciar el contenedor, que es lo peor que puede tocarte con una puja programada.

    El orden no es negociable: primero se comprueba, después se cierra

    Se hace una sola petición directa a LaLiga, con un cliente nuevo, sin puente y
    sin caché, a una ruta pública y barata:

      - 200: el WAF no ha cortado, el 403 era de lógica de negocio, así que se
        cierra el circuito.
      - 403: el bloqueo es real, no se cierra, y se dice por qué.

    Si se reactivara sin comprobar y el 403 fuese real, seguir pidiendo agravaría un
    bloqueo temporal hasta hacerlo permanente, que es la única forma de perder la liga de
    golpe.
    """
    import logging

    import httpx as _httpx

    from ..config import settings as _cfg
    from .deps import cliente as _cliente

    _log = logging.getLogger("coconut.api.salud")

    c = _cliente()
    if not getattr(c, "bloqueado", False):
        return {"ok": True, "cambiado": False, "estado": "el circuito ya estaba cerrado"}

    motivo = getattr(c, "motivo_bloqueo", "")

    # Cliente NUEVO a propósito: el compartido está bloqueado y rechazaría la petición
    # antes de salir. Ruta pública, sin token y cacheable: es la sonda más barata posible.
    try:
        async with _httpx.AsyncClient(
            timeout=20, base_url=_cfg.fantasy_base_url.rstrip("/") + "/api",
            headers={"x-lang": "es", "x-app": "2"},
        ) as sonda:
            r = await sonda.get("/v1/competition/1/players")
        codigo = r.status_code
    except Exception as e:
        response.status_code = 503
        return {"ok": False, "cambiado": False,
                "motivo_bloqueo": motivo,
                "detalle": f"no se pudo sondear LaLiga: {e}"}

    if codigo == 403:
        response.status_code = 409
        return {
            "ok": False, "cambiado": False, "sonda": codigo, "motivo_bloqueo": motivo,
            "detalle": ("EL BLOQUEO ES REAL: LaLiga sigue devolviendo 403. NO se reactiva. "
                        "Insistir ahora es lo que convierte un bloqueo temporal en "
                        "permanente. Esperar y volver a probar más tarde."),
        }
    if codigo >= 400:
        response.status_code = 503
        return {"ok": False, "cambiado": False, "sonda": codigo,
                "motivo_bloqueo": motivo,
                "detalle": f"la sonda devolvió {codigo}: no se reactiva sin un 200 claro"}

    c.bloqueado = False
    c.motivo_bloqueo = ""
    _log.warning("circuito REACTIVADO a mano tras sonda %s. Bloqueo previo: %s",
                 codigo, motivo)
    return {"ok": True, "cambiado": True, "sonda": codigo, "bloqueo_previo": motivo,
            "detalle": "LaLiga responde 200: el 403 era de negocio. Circuito cerrado."}


@app.get("/market/movers", tags=["histórico"],
         summary="Los que más se han movido de precio en 24 h")
async def movers(limit: int = Query(20, le=100)):
    return await biggest_movers(limit)


@app.get("/players/{player_id}/history", tags=["histórico"],
         summary="Serie de precio de un jugador, de tu propia instalación")
async def history(player_id: int, days: int = 30):
    async with Session() as s:
        rows = await s.execute(text("""
            SELECT ts, value, delta_24h FROM market_values
            WHERE player_id = :pid AND ts > :desde
            ORDER BY ts
        """), {"pid": player_id, "desde": hace(dias=days)})
        return [dict(r._mapping) for r in rows]


@app.get("/clausulas/objetivos", tags=["histórico"],
         summary="A quién puedes clausular, del más barato por punto al más caro")
async def objetivos(saldo: int, limite: int = 15):
    return await clausulas.objetivos_baratos(saldo, limite)


@app.get("/clausulas/exposicion", tags=["histórico"],
         summary="Cuáles de los tuyos alcanza el rival más rico")
async def exposicion(saldo_rival_max: int):
    return await clausulas.exposicion_propia(saldo_rival_max)


@app.get("/league/feed", tags=["histórico"],
         summary="Tablón ya ingerido, con nombres resueltos")
async def feed(hours: int = 24):
    async with Session() as s:
        rows = await s.execute(text("""
            SELECT e.ts, e.kind, m.name AS manager, p.name AS player, e.amount
            FROM league_events e
            LEFT JOIN managers m ON m.id = e.manager_id
            LEFT JOIN players  p ON p.id = e.player_id
            WHERE e.ts > :desde
            ORDER BY e.ts DESC
        """), {"desde": hace(horas=hours)})
        return [dict(r._mapping) for r in rows]
