"""`/programar/*`: ejecutar una acción de `accion.py` a una hora futura.

Una tarea programada llama al mismo endpoint que pulsarla en Coconut, con `confirmar=True`
y `dry=False`, así que pasa por las mismas guardas.

Persistencia

Las tareas viven en la tabla `tareas` y se rehidratan al arrancar, así que sobreviven a un
reinicio a mitad de espera. Una tarea cuya hora pasa con el proceso caído se
marca `perdida` y no se ejecuta tarde: una puja fuera de plazo o un clausulazo a
destiempo hacen daño.

No usa el `SQLAlchemyJobStore` de APScheduler porque necesita un driver síncrono
(psycopg2) y aquí todo es asyncpg. La tabla propia guarda además estado, motivo, resultado
e historial.

Validación en dos momentos

Al crear se comprueba lo que se puede: las siete que llevan jugador se resuelven contra
la API (existe, es tuyo o no según toque) y las cuatro de ficha, que solo se validan
aquí, comprueban que estén sus campos. `alineacion` no se valida al crear.

Al ejecutar se revalidan las de `Objetivo` y `alineacion`, porque pasan por `/accion/*`
y ese relee el jugador. Las cuatro de ficha NO: disparan con el `market_id` que se
guardó, y puede haber caducado. `saltar_validacion=true` desactiva el bloque de creación
entero.

Estados

`pendiente`, `ejecutada`, `rechazada` (la regla del juego dijo que no), `fallida` (avería),
`perdida` (venció con el proceso caído) y `cancelada`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text

from ..db import Session, ddl

log = logging.getLogger("coconut.api.programar")
router = APIRouter(prefix="/programar", tags=["programar"])

_TAREAS: dict[str, asyncio.Task] = {}

# Toda acción de `/accion/*` que cambie estado en la liga debe poder programarse: si no,
# solo cabe lanzarla "ahora mismo" y hay que estar delante del móvil a la hora exacta.
#
# Las que no reciben un `Objetivo` son las que más lo necesitan: una puja se cancela antes
# de que la subasta resuelva o ya no se cancela, y una oferta recibida caduca.
#
# Las acciones programables, en un solo sitio. Añadir una es añadir una fila: de aquí
# salen la validación al crear la tarea, el ejecutor y el título del aviso.
#   accion: (función de `accion.py`, título en el móvil, cuerpo)
# `cuerpo` es None para las que reciben un `Objetivo` (jugador + importe). Las que
# actúan sobre una ficha del mercado llevan el modelo y los campos que exige.
_TABLA: dict[str, tuple[str, str, tuple[str, tuple[str, ...]] | None]] = {
    "blindar":         ("blindar", "Blindaje", None),
    "pujar":           ("pujar", "Puja enviada", None),
    "ofertar":         ("ofertar", "Oferta enviada", None),
    "clausulazo":      ("clausulazo", "Clausulazo", None),
    "vender":          ("vender", "Listado en el mercado", None),
    "subir-clausula":  ("subir_clausula", "Cláusula subida", None),
    "venta-inmediata": ("venta_inmediata", "Venta inmediata", None),
    "alineacion":      ("alineacion", "Alineación guardada", None),
    "cancelar-puja":   ("cancelar_puja", "Puja cancelada", ("Puja", ("market_id", "bid_id"))),
    "retirar-mercado": ("retirar_mercado", "Retirado del mercado", ("Ficha", ("market_id",))),
    "aceptar-oferta":  ("aceptar_oferta", "Oferta aceptada",
                        ("Oferta", ("market_id", "offer_id", "importe"))),
    "rechazar-oferta": ("rechazar_oferta", "Oferta rechazada",
                        ("Oferta", ("market_id", "offer_id"))),
}
ACCIONES = set(_TABLA)
POR_FICHA = {a: c for a, (_, _, c) in _TABLA.items() if c}

DDL = """
CREATE TABLE IF NOT EXISTS tareas (
    id            text PRIMARY KEY,
    accion        text        NOT NULL,
    params        jsonb       NOT NULL,
    cuando        timestamptz NOT NULL,
    estado        text        NOT NULL DEFAULT 'pendiente',
    creada        timestamptz NOT NULL DEFAULT (now()),
    ejecutada_en  timestamptz,
    resultado     text,
    nota          text
);
CREATE INDEX IF NOT EXISTS idx_tareas_pendientes ON tareas (cuando) WHERE estado = 'pendiente';
"""


class NuevaTarea(BaseModel):
    accion: str = Field(
        ..., description=" · ".join(_TABLA))
    params: dict = Field(
        default_factory=dict,
        description="El cuerpo que recibiría /accion/{accion}. Las de jugador llevan "
                    "`jugador` (+ `importe`); las de ficha, `market_id` (+ `bid_id`, "
                    "`offer_id` o `importe` según cuál)")
    cuando: dt.datetime = Field(
        ..., description="Hora local a la que ejecutar. Solo tiene que ser futura: "
                         "se acepta cualquier minuto y cualquier segundo")
    nota: str = Field("", description="Por qué se programa; se guarda para poder auditarlo")
    autorenovar: bool = Field(
        False, description="Al terminar, se reprograma sola 5 min después de que caduque")


def _en_local(fila: dict) -> dict:
    """Pasa a hora local las marcas de tiempo de una fila de `tareas`.

    En SQLite un `SELECT` de columna declarada devuelve `datetime`, igual que asyncpg,
    pero una fila puede traer `None` (una tarea pendiente no tiene `ejecutada_en`), así
    que se comprueba antes de convertir.
    """
    for campo in ("cuando", "ejecutada_en"):
        v = fila.get(campo)
        if isinstance(v, dt.datetime):
            fila[campo] = v.astimezone()
    return fila


async def _crear_tabla() -> None:
    async with Session() as s:
        for sentencia in ddl(DDL).strip().split(";"):
            if sentencia.strip():
                await s.execute(text(sentencia))
        await s.commit()


async def _ejecutar_tarea(tid: str) -> None:
    """Ejecuta una tarea y guarda el resultado. Nunca lanza: deja rastro y sigue."""
    from . import accion as acc
    from .accion import Ficha, Objetivo, Oferta, Puja
    async with Session() as s:
        fila = (await s.execute(
            text("SELECT accion, params, estado FROM tareas WHERE id=:i"), {"i": tid})).first()
    if not fila or fila[2] != "pendiente":
        return
    accion, params = fila[0], fila[1]
    log.warning("ejecutando tarea programada %s: %s %s", tid, accion, params)

    # Nota: no hace falta marcar aquí la orden explícita. Estas funciones son las de
    # `accion.py`, y su `_ejecutar()` ya la marca: programar algo pasa por el mismo
    # camino que pulsarlo en Coconut.
    #
    # `res` se inicializa aquí porque el aviso del final lo lee siempre. Asignándolo solo
    # dentro del `try`, una tarea que falla (y un 409 de caja o un 423 de candado son el
    # caso normal) revienta el aviso con `UnboundLocalError`, y el que se pierde es el de
    # los fallos, que es el único que hay que atender.
    res = None
    try:
        if accion == "alineacion":
            # OJO: `dry=False` va explícito, igual que en las demás. Estas funciones son
            # endpoints y sus valores por defecto son objetos `Query`/`Body`, no valores:
            # llamadas desde Python, omitir `dry` deja dentro un objeto que es *truthy*
            # y la tarea se queda en simulación diciendo que se ejecutó.
            # `respetar_candado=True` por el mismo motivo: una tarea programada es una
            # decisión del modelo, no una reparación. Saltárselo es solo de la guardia.
            res = await acc.alineacion(payload=params.get("payload"),
                                   auto=params.get("auto", False), dry=False,
                                   respetar_candado=True)
        elif accion in POR_FICHA:
            # Estas cuatro no reciben un `Objetivo`: el destinatario es una ficha del
            # mercado, no un jugador. Se construye su propio cuerpo con `confirmar=True`,
            # igual que las demás: programar algo es autorizarlo.
            modelo = {"Puja": Puja, "Ficha": Ficha, "Oferta": Oferta}[POR_FICHA[accion][0]]
            campos = {c: params.get(c) for c in POR_FICHA[accion][1] if c in params}
            cuerpo = modelo(**{**campos, "confirmar": True})
            fn = getattr(acc, _TABLA[accion][0])
            res = await fn(cuerpo, dry=False, idempotency_key=f"tarea:{tid}")
        else:
            cuerpo = Objetivo(**{**params, "confirmar": True})
            fn = getattr(acc, _TABLA[accion][0])
            # `forzar` solo lo entiende el clausulazo y `autorenovar` solo el blindaje, y
            # los dos van explícitos: un `Query` sin resolver es truthy, así que omitir
            # `autorenovar` lo activa y cada blindaje programado encadena una vuelta que
            # nadie pidió. Va en `False` fijo incluso cuando la tarea lo pide, porque la
            # cadena la crea `_reprogramar_auto()` más abajo y aquí saldría duplicada.
            extra = {"clausulazo": {"forzar": params.get("forzar", False)},
                     "blindar": {"autorenovar": False}}
            res = await fn(cuerpo, dry=False, idempotency_key=f"tarea:{tid}",
                           **extra.get(accion, {}))
        # Una tarea que devuelve una simulación no se ha ejecutado, y marcarla como tal
        # es el peor fallo posible aquí: deja creer que el once está guardado o que la
        # puja salió. Si aparece un `dry_run`, es un fallo de programación de la llamada.
        if isinstance(res, dict) and res.get("dry_run"):
            raise RuntimeError(
                f"la acción '{accion}' devolvió una SIMULACIÓN en vez de ejecutarse: "
                f"a la llamada le falta `dry=False`")
        estado, salida = "ejecutada", json.dumps(res, default=str)[:2000]
    except HTTPException as e:
        # Un rechazo por regla del juego no es una avería: "has llegado al límite de
        # blindajes" es una respuesta correcta y marcarla como fallida llena Telegram.
        # 4xx = rechazada, salvo los dos 4xx propios que no dicen nada del juego: el
        # lock de la caja ocupado (409) y el mando en otro dispositivo (423). Esa tarea
        # no ha llegado a LaLiga y no se repite sola, así que sale como fallida y avisa.
        detalle = str(e.detail)
        de_aqui = (detalle.startswith("otra operación está en curso")
                   or detalle.startswith("el mando lo tiene"))
        if 400 <= e.status_code < 500 and not de_aqui:
            estado, salida = "rechazada", str(e.detail)[:2000]
            log.warning("tarea %s rechazada por LaLiga: %s", tid, e.detail)
        else:
            estado, salida = "fallida", f"HTTP {e.status_code}: {e.detail}"[:2000]
            log.error("tarea %s falló: %s", tid, e.detail)
    except Exception as e:
        estado, salida = "fallida", f"{type(e).__name__}: {e}"[:2000]
        log.exception("tarea %s falló", tid)

    # ---------- autorrenovación ----------
    # `autorenovar` encadena la tarea consigo misma: al blindar, la API devuelve
    # `shieldedEndDate` y se reprograma para 5 min después de que caduque, si queda cupo
    # en la jornada. Si no queda, para cuando vuelva el cupo, y entre medias el jugador
    # está expuesto.
    #
    # Se reprograma también si falló: si no, un fallo suelto rompe la cadena para
    # siempre y el jugador se queda sin blindar sin que nadie lo note.
    if params.get("autorenovar"):
        try:
            await _reprogramar_auto(tid, accion, params,
                                    res if estado == "ejecutada" else None)
        except Exception:
            log.exception("no se pudo autorrenovar la tarea %s", tid)

    async with Session() as s:
        await s.execute(text("""UPDATE tareas SET estado=:e, ejecutada_en=now(), resultado=:r
                                WHERE id=:i"""), {"e": estado, "r": salida, "i": tid})
        await s.commit()

    from ..alerts import EMERGENCIA, INFO, enviar
    # Los tres estados tienen que distinguirse de un vistazo en Telegram: "hecha",
    # "el juego no la deja" y "se ha roto algo" piden reacciones distintas.
    icono = {"ejecutada": "✅", "rechazada": "🚫"}.get(estado, "❌")  # sin-rastro-ok: Telegram
    # Un rechazo es una regla del juego, no una avería, así que no despierta a nadie. Pero
    # OJO: tampoco se reintenta. Solo se encadenan las tareas con `autorenovar`, y un
    # blindaje rechazado ni eso (lo recoge `cupo_blindaje`, que avisa cuando haya cupo).
    # Una puja rechazada por caja o un clausulazo con la ventana cerrada mueren aquí y se
    # quedan en el log, así que conviene mirar las `rechazada` de vez en cuando.
    nivel = INFO if estado == "rechazada" else EMERGENCIA
    await enviar(f"{icono} <b>{_titulo_aviso(accion, params)}</b>\n{_resumen(res, salida)}",
                 nivel=nivel, categoria="tarea",
                 clave=f"tarea:{accion}:{estado}" if estado == "rechazada" else None,
                 silencio=6 * 3600)


# Cómo se lee cada acción en el móvil. El JSON crudo de la respuesta es ilegible justo
# donde hay que entenderlo de un vistazo: en la mano, de noche y con prisa. El detalle
# completo vive en `tareas.resultado` y en el log.
_TITULOS = {a: t for a, (_, t, _) in _TABLA.items()}


def _titulo_aviso(accion: str, params: dict) -> str:
    quien = params.get("jugador") or ""
    return f"{_TITULOS.get(accion, accion)}{f': {quien}' if quien else ''}"


def _eur(v) -> str:
    try:
        return f"{int(v):,} €".replace(",", ".")
    except (TypeError, ValueError):
        return str(v)


def _resumen(res, salida: str) -> str:
    """Las dos o tres cifras que se quieren leer, no la respuesta entera.

    Se sacan a cualquier profundidad porque cada acción envuelve su respuesta de una
    forma (`{ok, status, detalle:{ok, status, datos:{…}}}`), y dar por hecho un nivel
    concreto devuelve `None` en silencio en cuanto una acción envuelve distinto.
    """
    campos = [
        ("buyoutClause", "cláusula ahora", _eur),
        ("money", "importe", _eur),
        ("shieldedEndDate", "protegido hasta", lambda v: str(v)[:16].replace("T", " ")),
        ("expirationDate", "resuelve", lambda v: str(v)[:16].replace("T", " ")),
    ]
    lineas = []
    for clave, etiqueta, formato in campos:
        v = _buscar_clave(res, clave)
        if v is None:
            continue
        lineas.append(f"   {etiqueta}: {formato(v)}")
    return "\n".join(lineas) if lineas else salida[:200]


def _buscar_clave(obj, clave: str):
    """Primera aparición de `clave` a cualquier profundidad. `None` si no está."""
    if isinstance(obj, dict):
        if clave in obj:
            return obj[clave]
        for v in obj.values():
            if (r := _buscar_clave(v, clave)) is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            if (r := _buscar_clave(v, clave)) is not None:
                return r
    return None


async def _cuando_se_libera_un_blindaje() -> dt.datetime | None:
    """Cuándo vuelve a haber cupo de blindajes: al abrir la próxima jornada.

    El límite es por jornada, no por día ni por blindajes activos. Lo dice el propio
    mensaje de error:

        030.02.03: "...you have reached the maximum number of shields allowed
                     ON THIS FIXTURE"

    Con el cupo gastado sigue rechazando aunque caduque uno, así que esperar a que
    caduque un blindaje no sirve: el hueco lo libera el cambio de jornada, no la
    caducidad.
    """
    from .deps import api, protegido
    try:
        w = await protegido(api().jornada_actual())
        abre = w.get("openingWeekDate")
        if not abre:
            return None
        # Un poco después de que abra, para no competir con el cambio de estado.
        cuando = dt.datetime.fromisoformat(str(abre)) + dt.timedelta(minutes=10)
    except Exception:
        log.warning("no se pudo calcular cuándo abre la jornada", exc_info=True)
        return None

    if cuando <= dt.datetime.now(dt.UTC):
        return None       # ya abrió: el fallo era por otra cosa
    log.warning("cupo de blindajes agotado en esta jornada: se reintenta al abrir la "
                "siguiente (%s)", cuando.strftime("%d-%b %H:%M"))
    return cuando


async def _vigilar_cupo(params: dict) -> None:
    """Deja una vigilancia que avisa cuando vuelva a haber cupo de blindajes.

    Hace falta el `player_team_id`, que es lo que `check-shield` pregunta. Si la tarea se
    creó solo con el nombre, se resuelve aquí. Y si no se puede, se dice: quedarse sin
    vigilancia en silencio es lo que convierte un blindaje rechazado en uno olvidado.
    """
    # El import de `vigilantes` es el que REGISTRA los tipos con su decorador. Hoy lo
    # arrastra `api/seguimientos.py`, pero depender de que otro módulo se importe primero
    # deja esto a merced del orden de carga, y el fallo sería un `tipo desconocido` justo
    # cuando hay un blindaje que salvar.
    from .. import seguimiento as seg
    from .. import vigilantes  # noqa: F401
    ptid = params.get("player_team_id")
    nombre = params.get("jugador") or params.get("nombre") or ""
    if not ptid and nombre:
        try:
            from .accion import _resolver
            j = await _resolver(nombre)
            ptid, nombre = j.get("player_team_id"), j.get("nombre") or nombre
        except Exception as e:
            log.warning("no se pudo resolver %r para vigilar el cupo: %s", nombre, e)
    if not ptid:
        log.error("blindaje rechazado y SIN vigilancia: falta el player_team_id de %r",
                  nombre)
        return
    # Cada 30 min: el cupo se renueva al cambiar de jornada, o sea en días, y
    # `check-shield` es una petición por vuelta. Caduca a los 5 días, que cubre el parón
    # más largo entre jornadas sin dejar la vigilancia viva para siempre.
    await seg.crear("cupo_blindaje",
                    {"player_team_id": str(ptid), "nombre": nombre},
                    revisar_cada=1800, caduca_en_horas=120,
                    nota="blindaje rechazado por falta de cupo")
    log.warning("blindaje rechazado: vigilando el cupo para %s", nombre)


async def _reprogramar_auto(tid: str, accion: str, params: dict, res: dict | None) -> None:
    """Crea la siguiente vuelta de una tarea autorrenovable."""
    proxima = None
    if res:
        # `shieldedEndDate` dice exactamente cuándo deja de estar protegido. Se busca en
        # cualquier nivel: la respuesta va envuelta (`{ok, status, detalle:{…}}`).
        fin = _buscar_clave(res, "shieldedEndDate")
        if fin:
            try:
                proxima = dt.datetime.fromisoformat(str(fin)) + dt.timedelta(minutes=5)
            except ValueError:
                log.warning("shieldedEndDate no parseable: %r", fin)

        # El cupo es POR JORNADA (`030.02.03`, "on this fixture"), así que un blindaje que
        # caduca a mitad de jornada no se puede renovar al caducar: encadenar ahí da un 400
        # y deja el hueco abierto igual. `shieldsLimitReached` viene en la misma respuesta
        # y aquí DECIDE la hora, no solo se registra.
        if _buscar_clave(res, "shieldsLimitReached") is True:
            libre = await _cuando_se_libera_un_blindaje()
            if libre and (proxima is None or libre > proxima):
                log.warning("sin cupo en esta jornada: la renovación de %s pasa de %s a %s",
                            params.get("jugador", "?"),
                            proxima.strftime("%d-%b %H:%M") if proxima else "(sin fecha)",
                            libre.strftime("%d-%b %H:%M"))
                proxima = libre
    if accion == "blindar" and res is None:
        # No se reprograma un blindaje que LaLiga rechazó. Ningún endpoint publica cuántos
        # blindajes quedan, así que un reintento a una hora adivinada es un 400 y un aviso
        # más. Lo recoge `cupo_blindaje`, que pregunta a `check-shield` (gratis, no gasta
        # cupo) y avisa cuando de verdad se pueda.
        #
        # Hay que CREARLO aquí. El vigilante está escrito, pero si nadie lo arranca el
        # blindaje rechazado se queda sin reprogramar y sin vigilar, o sea perdido.
        await _vigilar_cupo(params)
        return

    if proxima is None and accion == "blindar":
        # Aquí se llega con un blindaje que SÍ se puso, pero cuya respuesta no trae un
        # `shieldedEndDate` legible. Sin esa fecha la referencia buena es el cambio de
        # jornada, que es lo que renueva el cupo: reintentar cada hora gasta peticiones y
        # avisa de un fallo que no lo es.
        proxima = await _cuando_se_libera_un_blindaje()

    if proxima is None:
        # Sin fecha fiable se reintenta en una hora: el blindaje dura 24 h, así que
        # sondear de vez en cuando cuesta poco y no deja el hueco abierto.
        proxima = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)

    # La hora sale de `shieldedEndDate + 5 min` y se respeta tal cual: cuadrarla a un
    # múltiplo de 5 solo puede moverla hacia atrás, y ahí LaLiga responde `030.02.01` (ya
    # blindado) porque el anterior sigue vivo. Solo se corrige si ya ha pasado.
    proxima = proxima.astimezone().replace(microsecond=0)
    if proxima <= dt.datetime.now(dt.UTC):
        proxima += dt.timedelta(minutes=5)

    nuevo = uuid.uuid4().hex[:12]
    async with Session() as s:
        await s.execute(text("""INSERT INTO tareas (id, accion, params, cuando, nota)
                                VALUES (:i, :a, :p, :c, :n)"""),
                        {"i": nuevo, "a": accion, "p": json.dumps(params), "c": proxima,
                         "n": f"autorrenovación de {tid}"})
        await s.commit()
    _TAREAS[nuevo] = asyncio.create_task(_armar(nuevo, proxima))
    log.warning("autorrenovada %s %s (%s)", accion, proxima.strftime("%d-%b %H:%M"), nuevo)


async def _armar(tid: str, cuando: dt.datetime) -> None:
    """Duerme hasta la hora y ejecuta. Un `asyncio.Task` por tarea: son pocas y cortas."""
    espera = (cuando - dt.datetime.now(dt.UTC)).total_seconds()
    if espera > 0:
        await asyncio.sleep(espera)
    await _ejecutar_tarea(tid)
    _TAREAS.pop(tid, None)


async def rehidratar() -> None:
    """Al arrancar: reprograma lo pendiente y marca como perdido lo que ya venció."""
    await _crear_tabla()
    ahora = dt.datetime.now(dt.UTC)
    async with Session() as s:
        filas = (await s.execute(
            text("SELECT id, cuando FROM tareas WHERE estado='pendiente'"))).all()
        perdidas = [r[0] for r in filas if r[1] <= ahora]
        if perdidas:
            # No se ejecutan tarde: una puja fuera de plazo se pierde igual y un
            # clausulazo a destiempo sale en el tablón a una hora que no se eligió. Se
            # marcan y se avisa.
            await s.execute(text("""UPDATE tareas SET estado='perdida',
                                    nota=coalesce(nota,'')||' [vencida mientras el proceso estaba caído]'
                                    WHERE id IN :ids""").bindparams(
                                        bindparam("ids", expanding=True)),
                            {"ids": list(perdidas)})
            await s.commit()
            log.error("tareas perdidas por reinicio: %s", perdidas)
    for tid, cuando in [(r[0], r[1]) for r in filas if r[1] > ahora]:
        _TAREAS[tid] = asyncio.create_task(_armar(tid, cuando))
    if _TAREAS:
        log.info("rehidratadas %d tareas programadas", len(_TAREAS))


@router.post("", summary="Programa una acción para una hora futura")
async def crear(t: NuevaTarea, saltar_validacion: bool = Query(False)):
    if t.accion not in ACCIONES:
        raise HTTPException(400, f"acción no soportada: {t.accion}. Válidas: {sorted(ACCIONES)}")

    cuando = t.cuando if t.cuando.tzinfo else t.cuando.astimezone()
    if cuando <= dt.datetime.now(dt.UTC):
        raise HTTPException(400, "esa hora ya ha pasado")

    # Validar ahora lo que se pueda: enterarse a las 12:00 de que era imposible es tarde.
    #
    # `pujar` es la que más lo necesita: la subasta resuelve a las 13:48 y no hay segunda
    # oportunidad ese día, así que una tarea que va a morir con un 400 se lleva por
    # delante la ventana entera.
    aviso = None
    if not saltar_validacion and t.accion in ("blindar", "clausulazo", "vender",
                                              "subir-clausula", "venta-inmediata",
                                              "pujar", "ofertar"):
        from .accion import _resolver
        j = await _resolver(t.params.get("jugador", ""))
        if t.accion in ("blindar", "vender", "subir-clausula", "venta-inmediata") \
                and not j.get("es_mio"):
            raise HTTPException(400, f"{j['nombre']} no es tuyo")
        if t.accion == "clausulazo" and j.get("es_mio"):
            raise HTTPException(400, f"{j['nombre']} ya es tuyo")
        if t.accion == "blindar" and j.get("blindado"):
            aviso = f"ojo: ya está blindado hasta {j.get('blindado_hasta')}"
        if t.accion in ("pujar", "ofertar"):
            mk = j.get("mercado")
            if not mk:
                raise HTTPException(400, f"{j['nombre']} no está en el mercado")
            # Cada tipo de ficha tiene su ruta y la otra da 404: `bid` es solo para
            # agentes libres y `direct-offer` solo para las que vende un manager.
            if t.accion == "pujar" and not mk["es_agente_libre"]:
                raise HTTPException(400, f"{j['nombre']} lo vende un manager, no es agente "
                                         "libre: `bid` da 404 ahí. Programa `ofertar`")
            if t.accion == "ofertar" and mk["es_agente_libre"]:
                raise HTTPException(400, f"{j['nombre']} es agente libre: `direct-offer` no "
                                         "aplica. Programa `pujar`")
            # Se avisa y no se rechaza: la ficha caduca antes de la hora programada, pero
            # el mercado se renueva a diario y puede volver a salir. Decidirlo es de quien
            # programa, no de aquí.
            if mk.get("expira") and str(mk["expira"]) < cuando.isoformat():
                aviso = (f"ojo: la ficha caduca el {str(mk['expira'])[:16]}, antes de la "
                         f"hora que has puesto")

    # Las acciones por ficha se validan por campos, no resolviendo un jugador: el
    # destinatario es un id del mercado. Comprobar aquí que están todos evita que la tarea
    # espere tres horas para morir con un 422.
    #
    # Ojo a la asimetría: esto es lo ÚNICO que se comprueba de ellas. Las de `Objetivo`
    # se revalidan al ejecutar (`/accion/*` relee el jugador contra la API), y estas no:
    # el `market_id` que se guarda aquí puede haber caducado a la hora de disparo, y lo
    # que se ve entonces es el 4xx de LaLiga, no un aviso previo.
    if t.accion in POR_FICHA:
        faltan = [c for c in POR_FICHA[t.accion][1]
                  if c != "importe" and not t.params.get(c)]
        if faltan:
            raise HTTPException(
                400, f"a «{t.accion}» le faltan parámetros: {faltan}. "
                     f"Necesita {list(POR_FICHA[t.accion][1])}")
        # `aceptar-oferta` exige el importe: la API de LaLiga lo usa como confirmación y
        # sin él responde 400. Mejor saberlo ahora que a la hora de cobrar.
        if t.accion == "aceptar-oferta" and not t.params.get("importe"):
            raise HTTPException(
                400, "«aceptar-oferta» necesita `importe`: LaLiga lo exige como "
                     "confirmación del cobro")

    tid = uuid.uuid4().hex[:12]
    async with Session() as s:
        params = dict(t.params)
        if t.autorenovar:
            params["autorenovar"] = True
        await s.execute(text("""INSERT INTO tareas (id, accion, params, cuando, nota)
                                VALUES (:i, :a, :p, :c, :n)"""),
                        {"i": tid, "a": t.accion, "p": json.dumps(params),
                         "c": cuando, "n": t.nota})
        await s.commit()
    _TAREAS[tid] = asyncio.create_task(_armar(tid, cuando))
    log.info("tarea %s programada para %s: %s %s", tid, cuando, t.accion, t.params)
    return {"id": tid, "accion": t.accion, "cuando": cuando.isoformat(),
            "estado": "pendiente", "autorenovar": t.autorenovar, "aviso": aviso}


@router.get("", summary="Tareas programadas y su estado")
async def listar(estado: str | None = Query(None), limite: int = Query(30, le=200)):
    """Las tareas, con las horas en LOCAL.

    La base guarda en UTC, así que sin convertir una tarea creada para las 04:00 se lista
    como las 02:00. El instante es el mismo y el desplazamiento viaja en el ISO, pero el
    POST que la crea responde en local y todo lo demás del proyecto habla hora de Madrid:
    con las dos convenciones a la vez, comprobar que una tarea quedó a la hora pedida pasa
    por restar dos horas de cabeza.
    """
    sql = "SELECT id, accion, params, cuando, estado, ejecutada_en, resultado, nota FROM tareas"
    par = {"l": limite}
    if estado:
        sql += " WHERE estado = :e"
        par["e"] = estado
    sql += " ORDER BY cuando DESC LIMIT :l"
    async with Session() as s:
        filas = await s.execute(text(sql), par)
        return [_en_local(dict(r._mapping)) for r in filas]


@router.delete("/{tid}", summary="Cancela una tarea pendiente")
async def cancelar(tid: str):
    tarea = _TAREAS.pop(tid, None)
    if tarea:
        tarea.cancel()
    async with Session() as s:
        r = await s.execute(text("""UPDATE tareas SET estado='cancelada'
                                    WHERE id=:i AND estado='pendiente' RETURNING id"""),
                            {"i": tid})
        await s.commit()
        if not r.first():
            raise HTTPException(404, "no hay tarea pendiente con ese id")
    return {"ok": True, "id": tid, "estado": "cancelada"}
