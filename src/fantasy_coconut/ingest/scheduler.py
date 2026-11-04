"""Planificador de la ingesta. Todo en hora de Madrid, que es la que manda en este juego.

Dos criterios que no son negociables:
  - El maestro de jugadores se cachea 8 h en origen, así que sondearlo más de 3-4
    veces al día es tirar peticiones sin obtener un dato nuevo.
  - Jitter en los jobs de ingesta pesada: disparar en el minuto exacto (:00, :15) es
    lo que hace todo el mundo, y es justo el pico que conviene no engordar.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .. import entorno as _env
from ..clients.auth import SinCredenciales, tokens
from ..clients.fantasy import FantasyAPI
from ..clients.http import BloqueoDetectado, ClienteEducado, ParadaManual
from ..clients.via_api import cliente_lectura
from ..config import settings
from .league import ingest_league
from .mercado import ingest_mercado
from .onces import ingest_onces
from .players import ingest_players
from .vigilante import vigilar

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
# httpx registra la URL COMPLETA de cada petición, y la de Telegram lleva el token del
# bot dentro de la ruta: en INFO acabaría en `docker logs` y en cualquier visor. Se sube a
# WARNING; el registro útil (método, path, código, ms) ya lo hace `fantasy_coconut.http`.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("coconut.scheduler")

# Las tareas de fondo vivas. Se guardan aquí porque el bucle de eventos solo mantiene una
# referencia débil: una tarea sin dueño puede desaparecer a mitad, en silencio.
_TAREAS_DE_FONDO: set = set()


async def _con_jitter(tarea, nombre: str, max_seg: int = 60) -> None:
    espera = random.uniform(0, max_seg)
    log.info("%s: arranca en %.0fs (jitter)", nombre, espera)
    await asyncio.sleep(espera)
    try:
        # Entrada común de los jobs que pasan por `_con_jitter`: cambiar aquí el
        # transporte los migra a la vez, y volver atrás es apagar el flag.
        async with cliente_lectura() as http:
            res = await tarea(FantasyAPI(http))
            log.info("%s OK: %s | peticiones al origen hoy: %d",
                     nombre, res, http.contador.origen)
    except ParadaManual as e:
        log.warning("%s cancelado: %s", nombre, e)
    except BloqueoDetectado as e:
        # Aquí no se reintenta. El circuito vive en el cliente, así que por el camino
        # directo la siguiente pasada abre uno nuevo y vuelve a salir; el de la API se
        # cierra con `POST /salud/reactivar`.
        log.critical("%s: BLOQUEO. %s", nombre, e)
    except Exception:
        log.exception("%s falló", nombre)


async def job_players() -> None:
    await _con_jitter(ingest_players, "ingest_players")
    await _predicciones_de_precio()


async def _predicciones_de_precio() -> None:
    """Apunta la predicción del día y liquida las que ya se pueden comprobar.

    Va pegado a `job_players` porque depende de que la serie de precios del día esté ya
    ingerida: predecir con la foto de ayer sería medir otra cosa.

    Va en el scheduler y no a mano: un backtest que solo se ejecuta cuando alguien se
    acuerda no es un backtest, y el síntoma de que no corre es silencio, no un error.

    No se deja caer la ingesta si esto falla: la serie de precios no se puede rellenar
    hacia atrás y una predicción perdida solo cuesta un día de muestra.
    """
    try:
        from ..models import backtest
        log.info("precio: %s | %s",
                 await backtest.registrar_momentum_de_hoy(),
                 await backtest.liquidar_precio())
    except Exception:
        log.exception("predicciones de precio fallaron (la ingesta sí se guardó)")


async def job_onces() -> None:
    """Onces probables. No toca la API de LaLiga: son 20 peticiones a futbolfantasy."""
    try:
        res = await ingest_onces()
        log.info("onces OK: %s", res)
    except Exception:
        log.exception("onces falló")


async def job_emergencia() -> None:
    """Ronda rápida solo para huecos críticos.

    Va aparte del ciclo de liga (cada 30 min) porque una plaza sin cubrir se mide en
    minutos: si te clausulan al portero, el mercado puede cerrar antes del siguiente
    ciclo. Cuesta 2 peticiones y solo actúa si de verdad no se puede alinear.
    """
    from ..models.emergencia import revisar_plantilla
    huecos = [h for h in await revisar_plantilla() if h.critico]
    if not huecos:
        return
    log.error("HUECOS CRITICOS: %s", "; ".join(str(h) for h in huecos))

    async def tarea(api: FantasyAPI):
        try:
            token = await tokens.bearer()
        except SinCredenciales as e:
            return {"saltado": str(e)}
        from ..piloto import ejecutar as pilotar
        await ingest_mercado(api, token)
        return await pilotar(api, token)

    await _con_jitter(tarea, "emergencia", max_seg=15)


async def job_centinela() -> None:
    """Vigilancia rápida de clausulazos contra tu equipo. Ver `ingest/centinela.py`.

    Sin jitter a propósito: existe por educación con la API, y aquí la pasada barata no
    pide nada, porque con la ventana cerrada sale sin una sola petición. Con la ventana
    abierta gasta una por pasada, y a cambio se reacciona en minutos en vez de esperar al
    ciclo de liga. La frecuencia la fija `catalogo_jobs`.
    """
    from .centinela import revisar
    try:
        r = await revisar()
        # Solo se registra cuando hay algo que contar: a 6 pasadas por hora, un INFO por
        # cada una ahogaría el log justo donde se buscan los avisos de verdad.
        if r.get("nuevos"):
            log.error("centinela: %s", r)
        elif r.get("robos"):
            log.info("centinela: %s", r)
    except Exception:
        log.exception("centinela falló")


async def job_radar() -> None:
    """Detectores de oportunidad y peligro. Los avisos accionables salen por Telegram."""
    from ..alerts import enviar
    from ..models.saldos import estimar
    from ..models.vigilancia import mi_saldo
    from ..radar import barrido
    try:
        saldo = await mi_saldo()
        saldos, _ = await estimar()
        rival = max((x.estimado or 0) for x in saldos if not x.es_mio) if saldos else 0
        avisos = await barrido(saldo, rival)
        for a in avisos:
            # Va a Telegram, donde no hay CSS: el icono es lo único que distingue de un
            # vistazo un aviso informativo de uno urgente.
            icono = {1: "ℹ️", 2: "⚠️", 3: "🚨"}.get(a.urgencia, "•")  # sin-rastro-ok: texto de Telegram
            # El radar mezcla tres tipos de aviso en una sola llamada, así que la
            # categoría se saca del prefijo de su clave. Si aparece uno nuevo cae a
            # `sistema`, que está activo: mejor de más que perderlo.
            cat = {"expone": "proteccion", "chollo": "chollo",
                   "lastre": "lastre"}.get(a.clave.split(":")[0], "sistema")
            await enviar(f"{icono} <b>{a.titulo}</b>\n{a.detalle}",
                         clave=a.clave, silencio=12 * 3600, categoria=cat)
        log.info("radar: %d avisos", len(avisos))
    except Exception:
        log.exception("radar fallo")


async def _guardar_once(payload: dict, *, reparacion: bool = False) -> dict:
    """Manda el once por `/accion/alineacion`, que además de escribir comprueba que sean
    11 y que todos sigan en plantilla. El camino directo no tiene esa guarda.

    Si la API no responde se cae al cliente propio, porque no alinear son 0 puntos. Es el
    punto por el que pasan las dos vías: `guardia_once` también entra por aquí.

    `reparacion=True` pide saltarse el candado por la vía buena. Sin él, la guardia se
    lleva un 423, el `except` de abajo no lo distingue de una caída de la API y cae al
    cliente directo, que escribe sin las comprobaciones de `/accion/alineacion`.
    """
    from ..clients.acciones import Acciones
    from ..clients.via_api import accion as accion_api
    from ..clients.via_api import activo as via_api
    if via_api():
        try:
            return await accion_api("alineacion", payload,
                                    params={"respetar_candado": "false"} if reparacion else None)
        except Exception as e:
            log.warning("no se pudo alinear por /accion (%s); se hace directo",
                        type(e).__name__)
    token = await tokens.bearer()
    async with ClienteEducado() as http:
        return await Acciones(http, token).guardar_alineacion(payload)


async def _refrescar_plantilla_si_hace_falta(max_minutos: float | None = None) -> float | None:
    """Vuelve a ingerir la liga si el último snapshot está viejo. Devuelve su edad.

    `mejor_once()` y la validación del once fijado leen la base. Con una foto vieja se
    alinea a alguien ya clausulado, LaLiga lo descarta en silencio y el once se queda en
    diez. Si la foto está vieja, lo correcto no es decidir con ella: es sacar otra.

    Se refresca la liga entera, no solo el equipo propio: las consultas de plantilla cogen
    el último snapshot con un `max(ts)` global, así que escribir un solo manager con `ts`
    nuevo haría desaparecer a los demás sin dar error.

    Sin `max_minutos` el umbral sale del ciclo configurado de `league` más un margen, y no
    de un número suelto: un número fijo se queda atrás en cuanto alguien cambia el ciclo.

    Mientras `league` no falle no llega a reingerir: corre en `*/30` (:00 y :30) y la
    alineación en el :15, así que el snapshot tiene 15 min contra un umbral de 35. Esto
    actúa cuando esa cadena se rompe (sin credenciales, WAF, parada manual), que es
    justo cuando la foto vieja haría más daño. Su
    configuración guardada dice `minuto: 25`, pero ese campo no manda: `_triggers()` mira
    `cada_minutos` antes que `minuto` y se queda con el primero.
    """
    if max_minutos is None:
        # `leer_config()` ya devuelve el defecto del catálogo pisado por lo guardado, así
        # que basta con preguntarle. Si no contesta se cae a 30, que es el defecto de hoy.
        try:
            from ..trabajos import leer_config
            cfg = (await leer_config()).get("league") or {}
            ciclo = float(cfg.get("cada_minutos") or 30)
        except Exception:
            log.warning("no se pudo leer el ciclo de league; umbral a 30 min")
            ciclo = 30.0
        max_minutos = ciclo + 5

    from ..db import Session
    from ..salud import _EDAD_LIGA
    from ..tiempo import ahora, horas_entre
    try:
        async with Session() as s:
            edad_h = horas_entre(ahora(), (await s.execute(_EDAD_LIGA)).scalar())
    except Exception:
        log.exception("no se pudo medir la edad del snapshot; se sigue con lo que hay")
        return None

    minutos = None if edad_h is None else float(edad_h) * 60
    if minutos is not None and minutos <= max_minutos:
        log.info("alineación: el snapshot tiene %.0f min, sirve", minutos)
        return minutos

    log.warning("alineación: snapshot de %s; se reingiere la liga antes de decidir",
                "edad desconocida" if minutos is None else f"{minutos:.0f} min")
    try:
        async with cliente_lectura() as http:
            await ingest_league(FantasyAPI(http), await tokens.bearer())
    except Exception:
        # Si la reingesta falla se decide con la foto vieja. Puede meter en el once a
        # alguien ya vendido, y LaLiga lo descarta dejando diez; no alinear son once
        # plazas a cero.
        log.exception("no se pudo refrescar la plantilla; se decide con la foto vieja")
    return minutos


async def job_alineacion() -> None:
    """Calcula y (en modo real) guarda el mejor once.

    No alinear son 0 puntos, y eso no se recupera. Va aparte del piloto porque no gasta
    dinero y por tanto no debe frenarse por los límites de gasto.
    """
    from ..alerts import EMERGENCIA, enviar
    from ..api.bloqueo import estado as candado
    from ..models.alineacion import a_payload, mejor_once, valido

    # El candado del once manda también sobre el cron: si un humano lo ha fijado y
    # cerrado, el bot no recalcula por encima.
    if await candado("once"):
        log.info("once BLOQUEADO: el cron no recalcula")
        return
    from ..once_fijado import leer as leer_fijado
    from ..politica import SOMBRA, modo

    # Los dos caminos que vienen (el once fijado y `mejor_once()`) leen la base, así que
    # la foto tiene que estar fresca antes de decidir, no después.
    await _refrescar_plantilla_si_hace_falta()

    try:
        # ¿Hay un once fijado a mano para la próxima jornada? Manda sobre el modelo.
        # Sin esto, una decisión humana tomada con información que el bot no tiene
        # (previas, lesiones, ruedas de prensa) la borra el siguiente cron sin avisar.
        fijado = None
        try:
            async with cliente_lectura() as _h:
                w = await FantasyAPI(_h).jornada_actual()
            objetivo = w.get("nextWeek") if w.get("isLive") else w.get("weekNumber")
            if objetivo:
                fijado = leer_fijado(int(objetivo))
        except Exception:
            log.exception("no se pudo comprobar el once fijado; sigue el modelo")

        if fijado:
            # Validar que los once siguen siendo míos antes de reenviarlos: entre que
            # se fija un once y se guarda pueden venderse o clausularse jugadores, y
            # reenviar la lista sin comprobar deja la plaza vacía.
            #
            # Un once fijado es una decisión humana sobre una plantilla concreta; si la
            # plantilla cambia, la decisión ya no es válida y manda el modelo.
            from ..models.alineacion import mi_plantilla
            try:
                tengo = {j.player_team_id for j in await mi_plantilla()}
                pedidos = {str(fijado["payload"].get("goalkeeper"))}
                for k in ("defender", "midfield", "striker"):
                    pedidos |= {str(x) for x in (fijado["payload"].get(k) or [])}
                perdidos = {p for p in pedidos if p and p not in tengo}
            except Exception:
                log.exception("no se pudo validar el once fijado; se usa el modelo")
                perdidos = {"?"}
            if perdidos:
                log.error("el once fijado incluye jugadores que YA NO son míos (%s): "
                          "se descarta y decide el modelo", ", ".join(sorted(perdidos)))
                await enviar("⚠️ <b>Once fijado descartado</b>\n"  # sin-rastro-ok: texto de Telegram
                             f"Incluía {len(perdidos)} jugador(es) que ya no están en la "
                             "plantilla. Recalculado con el modelo para no dejar huecos.",
                             nivel=EMERGENCIA, clave="once-fijado-invalido",
                             silencio=6 * 3600, categoria="once_roto")
                from ..once_fijado import soltar as soltar_fijado
                soltar_fijado()
                fijado = None

        if fijado:
            payload = fijado["payload"]
            log.info("once FIJADO A MANO para la J%s (%s): no se recalcula",
                     fijado["jornada"], fijado.get("motivo", ""))
            if modo() == SOMBRA:
                log.info("[SOMBRA] once fijado que se guardaria: %s", payload)
                return
            res = await _guardar_once(payload)
            log.info("alineacion (fijada) guardada: %s", str(res)[:120])
            return

        once = await mejor_once()
        log.info("mejor once: %s", once)

        ok, motivo = valido(once)
        if not ok:
            # No se manda una alineacion que consta rota: dejarla como esta es mejor
            # que sustituirla por una peor.
            await enviar(
                f"⚠️ <b>No puedo alinear del todo</b>\n{once}\n"  # sin-rastro-ok: texto de Telegram
                f"Motivo: {motivo}\n"
                f"Con lo que hay, lo mejor seria "
                f"{once.formacion[0]}-{once.formacion[1]}-{once.formacion[2]}.",
                clave=f"once-invalido:{motivo}", silencio=12 * 3600,
                nivel=EMERGENCIA, categoria="once_roto")
            return

        payload = a_payload(once)
        # Se guarda en cualquier modo salvo SOMBRA, como todo lo que no mueve dinero.
        # Aquí además se puede rehacer hasta el pitido inicial, y no guardarla son 0
        # puntos seguros.
        if modo() == SOMBRA:
            log.info("[SOMBRA] alineacion que se guardaria: %s", payload)
            return

        res = await _guardar_once(payload)
        log.info("alineacion guardada: %s", res)
        d, m, dl = once.formacion
        await enviar(f"⚽ <b>Alineación guardada</b>: {d}-{m}-{dl}\n"  # sin-rastro-ok: texto de Telegram
                     + "\n".join(f"• {j.nombre}" for j in once.titulares),
                     clave=f"once:{d}{m}{dl}:{int(once.total)}", silencio=6 * 3600, categoria="alineacion")
    except Exception:
        log.exception("alineacion fallo")


async def job_alineacion_guardia() -> None:
    """Red de seguridad: guarda el once si la jornada está a punto de ABRIR.

    La alineación se congela en el `openingWeekDate` y lo que puntúa es la que hubiera en
    ese instante (`GET .../lineup/week/{N}` devuelve esa foto).
    Con el cron de horas fijas (10:15/18:15/20:15 por defecto) solo sirve la última
    pasada anterior a esa hora: una jornada que abriera a las 11:00 se jugaría con el once
    de las 10:15, y una que abriera a las 10:00, con el de las 20:15 del día anterior.

    Esto lo cubre: cada hora mira cuándo arranca el primer partido de la próxima jornada
    y, si falta menos del margen configurado, fuerza una pasada. Cuesta ~3 peticiones/hora:
    `week/current` y `calendar` van cacheados, pero la revisión del once pregunta la
    propiedad con `ttl=0` y esa sale siempre al origen.
    """
    from datetime import datetime
    try:
        async with cliente_lectura() as http:
            api = FantasyAPI(http)
            w = await api.jornada_actual()
            # Si la jornada actual sigue viva, la que hay que preparar es la siguiente.
            n = w.get("nextWeek") if w.get("isLive") else w.get("weekNumber")
            if not n:
                return
            ahora = datetime.now(dt.UTC)
            # La apertura sale de `jornada.apertura_de`: prefiere `openingWeekDate` (un
            # hecho que publica la API) y, cuando no lo hay, el primer partido FUTURO,
            # nunca `min(matchDate)` a secas, porque el calendario trae aplazados de
            # jornadas anteriores. Ver `jornada.py`.
            from ..jornada import apertura_de
            apertura = apertura_de(w, await api.calendario(int(n)), ahora=ahora,
                                   etiqueta=f"guardia J{n}")
            if apertura is None:
                log.info("guardia: la J%s no tiene partidos futuros; nada que preparar", n)
                return
            faltan = (apertura - ahora).total_seconds() / 60
    except Exception:
        log.exception("guardia de alineación: no se pudo calcular la apertura")
        return

    log.info("guardia: la J%s abre en %.0f min", n, faltan)

    # Antes que nada, ¿sigue completo el once? `job_alineacion` no puede comprobarlo,
    # porque se para en seco si hay candado: un clausulazo contra un titular dejaría el
    # once en 10 hasta que alguien lo mirase. Esto sí repara, y se salta el candado a
    # propósito: el criterio humano ya no se puede cumplir si el jugador no es tuyo.
    try:
        from ..guardia_once import revisar as revisar_once
        r = await revisar_once(motivo=f"guardia J{n}, abre en {faltan:.0f} min")
        if r.get("reparado"):
            log.warning("guardia: ONCE REPARADO -> %s", r.get("sustituciones"))
    except Exception:
        log.exception("guardia: fallo al revisar el once")

    from ..trabajos import leer_config
    cfg = await leer_config("alineacion_guardia")
    margen = int(cfg.get("aviso_minutos") or 75)

    # El disparo exacto, atado al `openingWeekDate` real y no a un cron. El cron horario
    # ya cubre "no perder la jornada", pero dispara en algún momento de los N minutos
    # previos: con la jornada abriendo a las 21:00, la pasada de las 20:00 guarda el once
    # con una hora de antelación y lo que ocurra en esos 60 min no entra. Aquí se programa
    # una pasada única a `apertura - MIN_ANTES_DE_CONGELAR`. El cron queda como red por si
    # el proceso reinicia y pierde la tarea puntual.
    _programar_alineacion_exacta(n, apertura)

    if 0 < faltan <= margen:
        log.warning("J%s abre en %.0f min (margen %d) -> se guarda el once AHORA",
                    n, faltan, margen)
        await job_alineacion()


# Cuánto antes de la congelación se guarda el once. No es 0 porque el PUT tarda y, si
# falla, `guardia_once` necesita margen para reparar y reintentar.
MIN_ANTES_DE_CONGELAR = 12

# Y cuánto antes se refrescan los onces probables, que son quienes deciden el titular.
# El cron de `onces` (9:40 y 19:40 por defecto) deja el dato con horas de antigüedad
# justo en la pasada que importa, y la fuente afina según se acerca el partido.
MIN_ANTES_DE_REFRESCAR_ONCES = 40

_SCHED = None               # lo rellena main(); None fuera del proceso del worker


def _programar_alineacion_exacta(jornada, apertura) -> None:
    """Deja una pasada única de `job_alineacion` justo antes del `openingWeekDate`.

    Idempotente: el id lleva la jornada, así que reprogramarla cada hora sustituye la
    anterior en vez de acumular disparos (`replace_existing`). Si la hora ya pasó,
    APScheduler la descartaría, así que se comprueba antes y se calla.
    """
    from datetime import datetime, timedelta
    if _SCHED is None:
        return
    ahora = datetime.now(dt.UTC)

    # Primero los onces, para que el once se decida con la probabilidad más fresca que
    # haya: alinear a quien la fuente ya da por suplente cuesta la plaza entera.
    onces = apertura - timedelta(minutes=MIN_ANTES_DE_REFRESCAR_ONCES)
    if onces > ahora:
        try:
            _SCHED.add_job(_reg("onces"), DateTrigger(run_date=onces),
                           id=f"onces_exacto_J{jornada}", replace_existing=True,
                           max_instances=1, misfire_grace_time=600)
            log.info("onces de la J%s se refrescarán a las %s (%d min antes de abrir)",
                     jornada, onces.astimezone().strftime("%d-%b %H:%M"),
                     MIN_ANTES_DE_REFRESCAR_ONCES)
        except Exception:
            log.exception("no se pudo programar el refresco de onces")

    cuando = apertura - timedelta(minutes=MIN_ANTES_DE_CONGELAR)
    if cuando <= ahora:
        return
    try:
        _SCHED.add_job(_reg("alineacion"), DateTrigger(run_date=cuando),
                       id=f"alineacion_exacta_J{jornada}", replace_existing=True,
                       max_instances=1, misfire_grace_time=600)
        log.info("alineación exacta de la J%s programada para %s (%d min antes de abrir)",
                 jornada, cuando.astimezone().strftime("%d-%b %H:%M"),
                 MIN_ANTES_DE_CONGELAR)
    except Exception:
        log.exception("no se pudo programar la alineación exacta")


async def job_ofertas() -> None:
    """Cobra las ofertas recibidas por los jugadores que tengo listados.

    Va cada 30 min y no cada 2 h porque las ofertas del sistema caducan en minutos.
    Cuesta 1-2 peticiones y solo actúa si hay una ficha propia con oferta encima.
    """
    from .ofertas import revisar_ofertas
    try:
        token = await tokens.bearer()
    except SinCredenciales as e:
        log.warning("ofertas: %s", e)
        return
    from ..trabajos import leer_config
    cfg = await leer_config("ofertas")
    try:
        res = await revisar_ofertas(token, solo_avisar=bool(cfg.get("solo_avisar")))
        if res.get("con_oferta"):
            log.info("ofertas: %s", res)
    except Exception:
        log.exception("revisión de ofertas falló")


async def job_salud() -> None:
    """Autodiagnóstico. Avisa solo si algo está mal: un 'todo OK' diario se ignora."""
    from ..alerts import enviar
    from ..salud import revisar
    try:
        inf = await revisar()
        if inf.ok:
            log.info("salud: todo correcto")
            return
        log.error("salud: %d fallos\n%s", len(inf.fallos), inf.texto())
        texto = "🚨 <b>fantasy-coconut: algo va mal</b>\n\n" + "\n".join(  # sin-rastro-ok: texto de Telegram
            f"❌ {c.nombre}: {c.detalle}" for c in inf.fallos)  # sin-rastro-ok: texto de Telegram
        # Se repite cada 12 h mientras siga roto: es un problema, no una notificación.
        await enviar(texto, clave="salud:" + ",".join(c.nombre for c in inf.fallos),
                     silencio=12 * 3600, categoria="sistema")
    except Exception:
        log.exception("el chequeo de salud falló")


async def job_league() -> None:
    """Plantillas, cláusulas y tablón. Unas 15 peticiones por ciclo.

    El agregado, una plantilla por manager (4), el saldo y una por página del tablón; y
    encadena `ingest_mercado` (1) y `vigilar()` (3), que van dentro de este mismo job. El
    "6" de antes llega solo hasta el saldo: deja fuera el tablón, que es una de las tres
    cosas que la primera línea enumera.
    """
    async def tarea(api: FantasyAPI):
        try:
            token = await tokens.bearer()
        except SinCredenciales as e:
            log.warning("liga: %s", e)
            return {"saltado": "sin credenciales"}
        res = await ingest_league(api, token)
        # El orden importa: el mercado es donde se tapa un hueco, la vigilancia compara
        # este snapshot con el anterior, y el piloto decide ya con todo fresco. En modo
        # agente no gasta solo, porque el freno de `Acciones` solo deja pasar lo que no
        # mueve dinero.
        res.update(await ingest_mercado(api, token))
        res.update(await vigilar())
        from ..piloto import ejecutar as pilotar
        res.update({'piloto': await pilotar(api, token)})
        return res

    await _con_jitter(tarea, "ingest_league")


# ---------- horario de la alineación ----------
#
# El horario vive en `jobs_config` (`catalogo_jobs["alineacion"]["horas"]`) y lo resuelve
# `_triggers()` como el de cualquier otro proceso. Se edita desde la pestaña Motor o desde
# Ajustes, que escribe en esa misma fila.
#
# El cron solo reparte el día. La red de seguridad es `job_alineacion_guardia`, que mira
# el `openingWeekDate` real y fuerza una pasada en los minutos previos, abra a la hora que
# abra.


async def job_reprogramar(sched) -> None:
    """Relee la configuración y reprograma lo que haya cambiado. Cada 10 min.

    Es lo que hace que tocar un horario o apagar un proceso desde Coconut surta efecto
    sin reiniciar el contenedor. Un ajuste que exige recrear el worker para aplicarse
    acaba sin tocarse nunca, y recrearlo se come la toma que estuviera en curso.
    """
    from ..trabajos import leer_config
    try:
        cfgs = await leer_config()
    except Exception:
        log.exception("no se pudo releer la configuración de los procesos")
        return

    for nombre in _catalogo():
        trigs = _triggers(nombre, cfgs.get(nombre, {}))
        ids = [j.id for j in sched.get_jobs()
               if j.id == nombre or j.id.startswith(f"{nombre}#")]
        # `str(trigger)` es la forma barata de saber si cambió: reprogramar siempre
        # reiniciaría la cuenta atrás en cada pasada y un job cada 2 h no correría nunca.
        quiere = [str(t) for t in trigs]
        tiene = [str(sched.get_job(i).trigger) for i in sorted(ids)]
        if quiere == tiene:
            continue
        for i in ids:
            sched.remove_job(i)
        for k, trig in enumerate(trigs):
            sched.add_job(_reg(nombre), trig, id=nombre if k == 0 else f"{nombre}#{k}",
                          max_instances=1, misfire_grace_time=1800)
        log.warning("proceso %s %s", nombre,
                    "APAGADO desde Coconut" if not trigs else f"reprogramado: {quiere}")

async def job_recompensa() -> None:
    """Cobra los 100.000 €/día que regala la liga.

    No tiene riesgo deportivo ni decisión que tomar: es dinero disponible todos los días
    que solo se pierde si nadie lo pide.

    Corre dos veces al día porque el reset es diario y una toma puede fallar (sesión,
    corte de red). La segunda pasada gasta su petición igual; lo que no hace es ingresar
    dos veces: si ya se cobró, LaLiga responde 400 con `errorCode: 050.01.04`, que se
    trata como sin efecto y no como avería.
    """
    from ..clients.acciones import Acciones
    from ..clients.via_api import accion as accion_api
    from ..clients.via_api import activo as via_api

    # Sin cuenta conectada `/accion` responde 503, se toma por la API caída, y la vía
    # directa acaba en `SinCredenciales` registrada como error. En una instalación nueva
    # es lo normal, así que se registra como INFO.
    try:
        await tokens.bearer()
    except SinCredenciales as e:
        log.info("recompensa diaria: sin cuenta conectada, no se cobra (%s)", e)
        return

    # Por `/accion/recompensa`, para que el cobro use el token bucket compartido y entre
    # en el contador de peticiones. El respaldo al cliente propio se queda: dejar de
    # cobrar 100.000 €/día por un reinicio de la API sería peor que la petición suelta
    # que se ahorra.
    if via_api():
        try:
            r = await accion_api("recompensa", None)
            if r.get("sin_efecto"):
                log.info("recompensa diaria: %s", r.get("detalle"))
            else:
                log.warning("recompensa diaria cobrada por /accion: %s", str(r)[:160])
            return
        except Exception as e:
            # Un 4xx de `/accion` es una respuesta, no una avería: el respaldo se queda
            # solo para cuando la API no contesta. La distinción es "no me han respondido"
            # contra "me han dicho que no".
            from ..clients.acciones import cuerpo_de
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status and 400 <= status < 500:
                log.info("recompensa diaria: %s", (cuerpo_de(e) or str(e))[:160])
                return
            log.warning("la API no respondió al cobrar (%s); se hace directo",
                        type(e).__name__)

    try:
        async with ClienteEducado() as http:
            token = await tokens.bearer()
            antes = (await FantasyAPI(http).saldo(token, settings.fantasy_team_id)) or {}
            res = await Acciones(http, token).cobrar_recompensa_diaria()
        if res.get("sombra"):
            log.info("recompensa diaria: modo sombra, no se cobra")
            return
        cobrado = int(res.get("teamMoney", 0)) - int(antes.get("teamMoney", 0) or 0)
        log.warning("recompensa diaria cobrada: +%s € (redenciones hoy: %s/%s)",
                    f"{cobrado:,}", res.get("dailyRewardsRedeemed"), res.get("dailyRewardLimit"))
    except Exception as e:
        # Que ya esté cobrada no es un fallo: es el caso normal de la segunda pasada del
        # día. httpx no mete el cuerpo en el mensaje de la excepción, así que hay que
        # leerlo de `e.response`; mirando solo `str(e)`, esto se registra como error.
        #
        # Se mira el `errorCode` porque es lo estable: el texto llega como "Team daily
        # reward limit reached", así que "LimitReached" pegado no casa nunca y todo el
        # reconocimiento se apoya en el "limit" suelto de al lado.
        from ..clients.acciones import cuerpo_de
        cuerpo = cuerpo_de(e)
        if "050.01.04" in cuerpo or "limit" in cuerpo.lower() or "already" in cuerpo.lower():
            log.info("recompensa diaria: ya estaba cobrada hoy (%s)", cuerpo[:120])
        else:
            log.exception("no se pudo cobrar la recompensa diaria: %s", cuerpo[:200])


async def job_mercado_avisos() -> None:
    """Revisa las búsquedas guardadas contra la baraja nueva.

    A las 13:50 porque el mercado se renueva a las 13:48: dos minutos después las
    fichas del día ya están y aún queda todo el día para pujar.
    """
    try:
        from ..api.buscar import revisar_avisos
        log.info("avisos de mercado: %s", await revisar_avisos())
    except Exception:
        log.exception("no se pudieron revisar los avisos de mercado")


async def job_precios_oficiales() -> None:
    """Un lote del histórico oficial de precios: una petición por jugador del lote.

    LaLiga publica el histórico de precios de cada jugador, con más recorrido que la serie
    propia. Se trae por lotes porque entero serían ~800 peticiones, treinta veces el
    consumo de un día.
    """
    async def tarea(api):
        from ..trabajos import leer_config
        from .precios_oficiales import POR_LOTE, ingerir_lote
        cfg = await leer_config("precios_oficiales")
        # Cada jugador del lote es UNA petición: subirlo acerca el baneo, así que el
        # número sale de la configuración y no de una constante escondida.
        return await ingerir_lote(api, await tokens.bearer(),
                                  int(cfg.get("lote") or POR_LOTE))
    await _con_jitter(tarea, "precios_oficiales")


async def job_puntos() -> None:
    """Puntos por jornada de las plantillas, leídos de la ficha individual.

    Por qué no basta el maestro público está en `ingest/puntos.py`. Aquí importa la
    cadencia: dos veces al día y no una, porque una jornada solo se ingiere cuando ya ha
    cerrado y el once de la siguiente se decide pocas horas después. Con una sola pasada
    puede tocar justo en medio y decidir el once con una jornada de menos.
    """
    from ..trabajos import leer_config
    from .puntos import ingest_puntos

    cfg = await leer_config("puntos")
    # Las cuatro plantillas por defecto: los puntos de un rival deciden si su cláusula
    # sale a cuenta, así que tenerlos mal cuesta lo mismo que tener mal los propios.
    res = await ingest_puntos(solo_mios=bool(cfg.get("solo_mios")))
    log.info("puntos por jornada: %s", res)


async def _bucle_peticiones() -> None:
    """Recoge los jobs que se piden desde Coconut. El worker no tiene servidor HTTP, así
    que el canal de vuelta es una tabla: la API deja la petición y esto la consume."""
    from ..trabajos import recoger_peticiones
    while True:
        try:
            await recoger_peticiones({n: _reg(n) for n in _catalogo()})
        except Exception:
            log.exception("bucle de peticiones de job")
        await asyncio.sleep(20)


async def job_resumen_diario() -> None:
    """Un mensaje al día con lo que hay que atender, en vez de goteo.

    Se pide por HTTP a la API, no importando el módulo: `/informe/hoy` usa
    `deps.cliente()`, que solo existe en el proceso de la API, y aquí revienta con un 503.
    `job_mercado_avisos` sí importa el suyo porque `_mercado_crudo()` captura ese 503 y
    abre un cliente propio.

    Se calla si no hay nada por encima del umbral de urgencia.
    """

    import httpx

    from .. import ajustes as _aj
    from ..alerts import enviar

    cfg = await _aj.leer()
    if not cfg.get("resumen_activo"):
        return
    minima = int(cfg.get("resumen_urgencia_minima") or 40)

    base = _env.var("API_INTERNA", "http://api:8000")
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.get(f"{base}/informe/hoy")
            r.raise_for_status()
            datos = r.json()
    except Exception:
        log.exception("no se pudo componer el resumen diario")
        return

    asuntos = [a for a in datos.get("asuntos", []) if a.get("urgencia", 0) >= minima]
    if not asuntos:
        log.info("resumen diario: sin asuntos por encima de %d, no se manda", minima)
        return

    lineas = [f"📋 <b>{datos.get('resumen', 'Resumen')}</b>", ""]  # sin-rastro-ok: texto de Telegram
    for a in asuntos:
        lineas.append(f"<b>{a['titulo']}</b>")
        if a.get("detalle"):
            lineas.append(a["detalle"])
        lineas.append(f"→ {a.get('accion', '')}")
        lineas.append("")
    # `tarea` es la categoría que ya está encendida por defecto y no es urgente: el
    # resumen respeta el silencio nocturno como cualquier otro aviso no urgente.
    await enviar("\n".join(lineas).strip(), categoria="tarea",
                 clave=f"resumen_{dt.date.today()}")



# ---------- catálogo de jobs ----------

async def job_trading() -> None:
    """Detecta oportunidades de comprar-para-vender y avisa. No compra.

    A las 13:52, cuatro minutos después de que el mercado se renueve, y dos después de
    `job_mercado_avisos` para no pedir el mercado a la vez.

    Está en modo propuesta a propósito: la señal de momentum es fuerte (77,6 % de
    persistencia) pero solo el 10,6 % de los agentes libres está en racha, y el retorno
    real de la operación se apoya en seis casos. Con esa muestra se mide, no se automatiza.

    Como `job_mercado_avisos`, pide por HTTP a la propia API: `/trading/oportunidades`
    usa `deps.cliente()`, que solo existe dentro del proceso de la API.
    """
    import httpx

    from ..alerts import INFO, enviar

    base = _env.var("API_INTERNA", "http://api:8000")
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.get(f"{base}/trading/oportunidades")
            r.raise_for_status()
            d = r.json()
            cart = (await c.get(f"{base}/trading/cartera")).json()
    except Exception as e:
        log.warning("job_trading: no se pudo consultar (%s)", type(e).__name__)
        return

    cand = d.get("candidatos") or []
    vender = cart.get("a_vender") or []
    log.info("trading: %d candidato(s) · %d abierta(s) · %d por vender · %s",
             len(cand), cart.get("n", 0), len(vender), d.get("bloqueo") or "sin bloqueo")

    if not cand and not vender:
        return                       # silencio: la regla de la casa es avisar solo si hay acción

    lineas = []
    for x in cand[:3]:
        lineas.append(f"📈 <b>{x['nombre']}</b> {x['valor']/1e6:.2f}M · "  # sin-rastro-ok: texto de Telegram
                      f"momentum {x['momentum']*100:+.1f}% · pujar {x['pujar']:,} €"
                      .replace(",", "."))
    for o in vender:
        lineas.append(f"💰 <b>{o['nombre']}</b> lleva {o['dias']:.0f} días: toca listarlo")  # sin-rastro-ok: texto de Telegram
    await enviar("📊 <b>Trading</b>: hay movimiento\n" + "\n".join(lineas),  # sin-rastro-ok: texto de Telegram
                 clave="trading:" + ",".join(sorted(x["nombre"] for x in cand)),
                 nivel=INFO, categoria="chollo")


async def job_respaldo() -> None:
    """Sube la copia de seguridad al repositorio privado del usuario.

    Se hace aquí y no por la API, al revés que `job_resumen_diario`. Ese tiene que
    ir por HTTP porque `/informe/hoy` usa `deps.cliente()`, que solo existe en el proceso
    de la API; esto solo lee la base de datos y habla con GitHub, así que no hay motivo
    para el rodeo: y uno menos que pueda fallar.

    No toca LaLiga ni una vez: ni gasta cuota ni pasa por el token bucket.
    """
    from .. import ajustes as _aj
    from .. import respaldo as _R
    from ..respaldo_git import Destino, ErrorGitHub
    from ..respaldo_git import subir as _subir

    cfg = await _aj.leer()
    if not cfg.get("respaldo_activo"):
        return
    destino = Destino(repo=cfg.get("respaldo_repo", ""),
                      token=cfg.get("github_token", ""),
                      rama=cfg.get("respaldo_rama") or "main")
    if not destino.valido:
        log.warning("respaldo activo pero sin repositorio o token: no se copia nada")
        return

    ficheros = await _R.exportar(completa=cfg.get("respaldo_completa", False))
    huella = _R.huella(ficheros)
    # Dos copias iguales dan la misma huella, así que esto se salta el trabajo entero
    # cuando no ha cambiado nada, y de paso evita un commit vacío al día: la historia
    # solo sirve para ver qué cambió si no está llena de copias idénticas.
    if huella == cfg.get("respaldo_huella"):
        log.info("respaldo: sin cambios desde la última copia (%s)", huella)
        return

    ahora = dt.datetime.now(dt.UTC)
    try:
        res = await _subir(destino, ficheros, f"copia {ahora:%Y-%m-%d %H:%M} · {huella}")
    except ErrorGitHub as e:
        # Un fallo de GitHub no es una emergencia de la liga: no despierta a nadie, pero
        # se registra con su código para poder arreglarlo.
        log.error("respaldo: GitHub rechazó la subida (%s) %s", e.status, e.detalle)
        return
    if res.get("subido"):
        await _aj.guardar({"respaldo_ultima": ahora.isoformat(),
                           "respaldo_huella": huella})
        log.warning("respaldo subido: commit %s · %d fichero(s)",
                    res.get("commit"), len(res.get("ficheros") or []))

    # Renovar el mando aquí es lo que hace que no haya que acordarse: mientras el aparato
    # que manda siga vivo y copiando, lo conserva; en cuanto deja de hacerlo, caduca solo
    # y otro puede tomarlo sin que nadie vaya a desbloquear nada.
    try:
        from ..dispositivo import renovar_si_es_mio
        if await renovar_si_es_mio():
            log.info("mando renovado por %s h", 36)
    except Exception as e:
        log.warning("no se pudo renovar el mando: %s", e)


# Un solo sitio que empareja el nombre corto (el que ve Coconut) con su función. Sirve
# para dos cosas: envolverlos y que se registren solos, y poder lanzarlos a mano desde
# la web sin que el navegador nombre funciones arbitrarias del worker.
#
# Los jobs se definen encima de esta función a propósito: `_catalogo()` los referencia al
# cargar el módulo, así que uno definido después revienta el arranque con un `NameError`.
def _catalogo() -> dict:
    return {
        "players": job_players, "league": job_league, "onces": job_onces,
        "radar": job_radar, "emergencia": job_emergencia, "centinela": job_centinela,
        "alineacion": job_alineacion, "alineacion_guardia": job_alineacion_guardia,
        "ofertas": job_ofertas, "salud": job_salud, "recompensa": job_recompensa,
        "mercado_avisos": job_mercado_avisos, "precios_oficiales": job_precios_oficiales,
        "trading": job_trading, "puntos": job_puntos,
        "resumen_diario": job_resumen_diario,
        "respaldo": job_respaldo,
    }


_ENVUELTOS: dict = {}


def _reg(nombre: str):
    """Devuelve el job envuelto para que se apunte en `jobs_log` al correr."""
    from ..trabajos import envolver
    if nombre not in _ENVUELTOS:
        _ENVUELTOS[nombre] = envolver(nombre, _catalogo()[nombre])
    return _ENVUELTOS[nombre]


def _triggers(nombre: str, cfg: dict) -> list[CronTrigger]:
    """Traduce la configuración de un proceso a sus `CronTrigger`.

    Devuelve una lista porque una sola expresión cron no siempre basta:

    `CronTrigger(hour="9,14", minute="40,15")` NO significa "a las 9:40 y a las
    14:15". Cron cruza los campos, así que dispara cuatro veces: 9:15, 9:40, 14:15
    y 14:40. Con horas de minutos distintos hay que registrar un trigger por cada
    minuto.

    Lista vacía = el proceso está apagado. Ante una configuración que no se entiende,
    cae al valor por defecto del catálogo y lo registra: dejar un job sin programar
    por un ajuste mal escrito es peor que ignorar el ajuste.
    """
    from ..catalogo_jobs import defecto
    if not cfg.get("activo", True):
        return []
    try:
        if cfg.get("horas"):
            # Se agrupan las horas por minuto: las que lo comparten van en un trigger.
            por_minuto: dict[int, list[int]] = {}
            for h in cfg["horas"]:
                hh, mm = str(h).split(":")
                por_minuto.setdefault(int(mm), []).append(int(hh))
            return [CronTrigger(hour=",".join(str(x) for x in sorted(hs)), minute=m)
                    for m, hs in sorted(por_minuto.items())]
        # `cada_minutos` gana a `cada_horas` cuando está puesto: es lo que permite bajar de
        # una hora sin cambiar la forma de la configuración guardada, que seguiría siendo
        # válida. Se mira primero por eso mismo, para que ponerlo surta efecto.
        if cfg.get("cada_minutos"):
            paso = max(1, min(59, int(cfg["cada_minutos"])))
            return [CronTrigger(minute=f"*/{paso}")]
        if cfg.get("cada_horas"):
            return [CronTrigger(hour=f"*/{int(cfg['cada_horas'])}",
                                minute=int(cfg.get("minuto", 0)))]
        if cfg.get("minutos"):
            mins = cfg["minutos"]
            if isinstance(mins, str):
                mins = [m.strip() for m in mins.split(",") if m.strip()]
            return [CronTrigger(minute=",".join(str(int(m)) for m in mins))]
        if cfg.get("minuto") is not None:
            return [CronTrigger(minute=int(cfg["minuto"]))]
    except Exception:
        log.exception("configuración ilegible de %s (%s): se usa la de por defecto",
                      nombre, cfg)
        d = defecto(nombre)
        if d != cfg:
            return _triggers(nombre, d)
    return []


async def _registrar_jobs(sched) -> None:
    """Da de alta cada job con el horario que diga su configuración."""
    from ..trabajos import leer_config
    cfgs = await leer_config()
    apagados = []
    for nombre in _catalogo():
        trigs = _triggers(nombre, cfgs.get(nombre, {}))
        if not trigs:
            apagados.append(nombre)
            continue
        # Con varios triggers hacen falta varios ids; el primero se queda con el nombre
        # limpio para que `sched.get_job(nombre)` siga funcionando.
        for k, trig in enumerate(trigs):
            sched.add_job(_reg(nombre), trig, id=nombre if k == 0 else f"{nombre}#{k}",
                          max_instances=1, misfire_grace_time=1800)
    if apagados:
        log.warning("procesos APAGADOS en la configuración: %s", ", ".join(apagados))


async def main() -> None:
    global _SCHED
    sched = AsyncIOScheduler(timezone="Europe/Madrid")
    # La guardia necesita el scheduler para dejar la pasada exacta antes de que la
    # jornada congele el once. Se guarda aquí y no se pasa por `args` porque el job ya
    # está registrado por nombre desde el catálogo.
    _SCHED = sched

    # ---------- el bucle de seguimientos ----------
    # No es un job del scheduler a propósito: no se dispara a horas, vive. Es una
    # corrutina que despierta cada pocos segundos y mira qué vigilancias tocan. Su estado
    # vive entero en la tabla, así que al arrancar retoma lo que quedó a medias sin hacer
    # nada especial.
    from .. import vigilantes  # noqa: F401  (registra los tipos)
    from ..seguimiento import bucle as bucle_seguimientos
    from ..trabajos import crear_tablas as _crear_tablas_jobs
    await _crear_tablas_jobs()
    # Hay que guardar la referencia. `asyncio.create_task` solo deja una
    # referencia débil en el bucle de eventos: sin guardarla, el recolector puede
    # llevarse la tarea a mitad de ejecución. Aquí eso significa que las vigilancias y
    # las peticiones de trabajos dejan de funcionar sin un solo error en el log, que
    # es el modo de fallo más difícil de notar en algo cuyo síntoma es el silencio.
    #
    # Además es latente: puede no darse nunca, o darse el día que el proceso pase apuros
    # de memoria, que es justo cuando menos conviene.
    _TAREAS_DE_FONDO.add(asyncio.create_task(bucle_seguimientos(), name="seguimientos"))
    _TAREAS_DE_FONDO.add(asyncio.create_task(_bucle_peticiones(), name="peticiones"))
    log.info("bucle de seguimientos arrancado")

    # ---------- registro de los jobs, gobernado por `jobs_config` ----------
    #
    # Ningún `add_job` lleva su horario escrito a mano: el horario y el interruptor salen
    # de la configuración, que se edita desde Coconut. El catálogo (`catalogo_jobs.py`)
    # pone el valor por defecto y lo guardado manda por encima, de modo que cambiar un
    # defecto no pisa lo que el usuario haya elegido.
    #
    # Un trigger mal configurado no puede dejar al sistema sin ingerir: si la
    # configuración no se entiende, `_triggers()` cae al defecto del catálogo; si el que
    # falla es el propio defecto, el proceso queda apagado y se ve en el log.
    await _registrar_jobs(sched)

    sched.add_job(job_reprogramar, CronTrigger(minute="*/10"), id="reprogramar",
                  args=[sched], max_instances=1, misfire_grace_time=300)

    sched.start()
    log.info("scheduler arrancado | jobs: %s", [j.id for j in sched.get_jobs()])

    # Primera toma al arrancar: no esperamos a la próxima ventana para tener datos.
    # Van por `_reg()` como las del cron, no llamadas a pelo: si no, la pasada de arranque
    # no deja fila en `jobs_log` y Coconut da por "nunca ha corrido" un job que acaba de
    # correr. Un proceso que solo se dispara aquí figuraría como muerto para siempre.
    for _nombre in ("recompensa", "players", "league"):
        await _reg(_nombre)()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
