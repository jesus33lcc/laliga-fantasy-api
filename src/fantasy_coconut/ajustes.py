"""Ajustes editables en caliente: el bot de Telegram y qué avisos quieres recibir.

Por qué no basta el `.env`

Cambiar a qué avisa el sistema por variables de entorno exige recrear el contenedor: un
`restart` no relee el `.env`. Eso convierte una preferencia en una operación, y una
preferencia que cuesta una operación acaba sin ajustarse nunca.

Aquí viven en la BD y se leen en cada envío, con 15 s de caché para no consultar
Postgres en cada aviso. Se cambian desde Coconut y surten efecto en el acto.

El token

Se guarda en la tabla, pero `leer_publico()` nunca lo devuelve entero: solo los
cuatro últimos caracteres. Un token de bot permite escribir en el chat, así que no
tiene por qué viajar al navegador solo para pintar un formulario.

El `.env` funciona como valor de partida: si no hay nada guardado, se usa el de
`settings`. Así una instalación sin ajustes en la tabla avisa igual.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Any

from sqlalchemy import text

from .config import settings
from .db import Session, ddl

log = logging.getLogger("coconut.ajustes")

DDL = """
CREATE TABLE IF NOT EXISTS ajustes (
    clave       text PRIMARY KEY,
    valor       jsonb NOT NULL,
    actualizado timestamptz NOT NULL DEFAULT now()
)
"""

# Cada categoría corresponde a avisos que alguien manda de verdad (el módulo está en
# `quien`). Una casilla que no controle ningún aviso se apaga y el aviso sigue llegando.
#
# `quien` dice dónde está la llamada a `enviar()`, no dónde se detecta el hecho, y en
# tres categorías no es el mismo sitio: el radar produce avisos y quien los manda es
# `ingest/scheduler.py`, que saca la categoría del prefijo de la clave. Ni `radar.py`
# ni `salud.py` llaman a `enviar()` ni una vez.
#
# Se comprueba cruzando lo declarado aquí con los `categoria=` del código, y cuidado al
# hacerlo: los tres del radar se pasan en una variable, no como literal, así que un grep
# de literales los da por huérfanos y no lo están.
CATEGORIAS: dict[str, dict[str, Any]] = {
    "robo": {
        "titulo": "Te han quitado un jugador",
        "detalle": "Un rival ha pagado la cláusula de alguien tuyo.",
        "urgente": True, "defecto": True,
        "quien": "ingest/vigilante.py · ingest/centinela.py",
    },
    "once_roto": {
        "titulo": "No se puede alinear",
        "detalle": "El once no es legal o falta un portero. Sin once, la jornada da 0.",
        "urgente": True, "defecto": True,
        "quien": "piloto.py · guardia_once.py · api/accion.py · ingest/scheduler.py",
    },
    "sistema": {
        "titulo": "El sistema ha fallado",
        "detalle": "Parada por WAF, sesión caducada, error al ejecutar una escritura.",
        "urgente": True, "defecto": True,
        "quien": "piloto.py · ingest/scheduler.py · ingest/ofertas.py",
    },
    "tarea": {
        "titulo": "Tarea programada",
        "detalle": "Se ha ejecutado (o ha fallado) algo que dejaste programado.",
        "urgente": False, "defecto": True,
        "quien": "api/programar.py · seguimiento.py · ingest/scheduler.py",
    },
    "oferta": {
        "titulo": "Ofertas y ventas",
        "detalle": "Han ofertado por un jugador tuyo, o se ha cobrado una venta.",
        "urgente": False, "defecto": False, "quien": "ingest/ofertas.py",
    },
    "clausula_rival": {
        "titulo": "Un rival baja una cláusula",
        "detalle": "Solo bajadas reales (≥3 % y ≥100.000 €), no el reajuste de las 00:15.",
        "urgente": False, "defecto": False, "quien": "ingest/vigilante.py",
    },
    "blindaje_rival": {
        "titulo": "Un rival blinda a alguien",
        "detalle": "Blindar cuesta algo, así que señala al jugador que teme perder.",
        "urgente": False, "defecto": False, "quien": "ingest/vigilante.py",
    },
    "chollo": {
        "titulo": "Chollos en el mercado",
        "detalle": "Ojo: la oferta debe ser ≥ el valor, así que el descuento no siempre es comprable.",
        "urgente": False, "defecto": False,
        "quien": "lo detecta radar.py, lo manda ingest/scheduler.py · ingest/vigilante.py",
    },
    "puja_rival": {
        "titulo": "Te disputan una puja",
        "detalle": "Alguien más ha pujado por una ficha en la que tú ya pujaste. "
                   "La subasta es ciega y a primer precio, así que saber que hay "
                   "competencia es lo único que se puede saber antes de que resuelva.",
        "urgente": False, "defecto": True, "quien": "ingest/mercado.py",
    },
    "proteccion": {
        "titulo": "Se acaba una protección tuya",
        "detalle": "Un jugador tuyo sale de sus 14 días y queda clausulable.",
        "urgente": False, "defecto": False,
        "quien": "lo detecta radar.py, lo manda ingest/scheduler.py",
    },
    "lastre": {
        "titulo": "Jugadores que sobran",
        "detalle": "Lesionados o con menos del 20 % de titularidad: ocupan plaza y dinero.",
        "urgente": False, "defecto": False,
        "quien": "lo detecta radar.py, lo manda ingest/scheduler.py",
    },
    "mercado_filtro": {
        "titulo": "Mis búsquedas del mercado",
        "detalle": "Cuando la baraja nueva de las 13:48 trae algo que cumple un filtro tuyo.",
        "urgente": False, "defecto": True, "quien": "api/buscar.py",
    },
    "alineacion": {
        "titulo": "Se ha guardado tu once",
        "detalle": "Cada vez que el bot alinea por su cuenta. Útil para vigilarlo unos días.",
        "urgente": False, "defecto": False, "quien": "ingest/scheduler.py",
    },
}

POR_DEFECTO: dict[str, Any] = {
    "telegram_token": "",

    # ---------- cuotas de apuestas p(portería a cero) ----------
    # La clave de the-odds-api (capa gratis: 500 créditos/mes). Se trata como el token
    # de Telegram: nunca vuelve entera al navegador, solo una pista, y dejar el
    # campo vacío significa "no la toques".
    #
    # Sin clave, el sistema sigue funcionando: `models/dificultad.py` mantiene su
    # aproximación desde el ataque del rival. Lo que se pierde es la versión calibrada.
    "odds_api_key": "",
    "telegram_chat_id": "",
    "telegram_activo": True,
    "avisos": {k: v["defecto"] for k, v in CATEGORIAS.items()},

    # ---------- a qué ritmo se habla con laliga ----------
    #
    # Peticiones por segundo del token bucket compartido. Vive aquí y no solo en el
    # `.env` porque ahí cambiarlo obliga a recrear el contenedor, y eso se come la toma
    # de precios en curso.
    #
    # Referencia: el cliente oficial se autoimpone 1 req/s, y el valor de partida es la
    # mitad. Subirlo gana tiempo, no información (el proceso más lento tarda 106 s y corre
    # de madrugada); para datos más frescos se sube la frecuencia en Motor.
    "rate_limit_rps": 0.0,      # 0 = el valor del `.env`

    # ---------- cuánto ruido se tolera ----------
    # El interruptor por categoría decide de qué se avisa, pero no cuándo vale
    # la pena. Sin esto, un chollo de 300.000 € interrumpe igual que un clausulazo
    # de 15M: y el resultado conocido es que se dejan de leer los avisos buenos.

    # Por debajo de esto no se avisa de nada que se mida en dinero. 0 = sin filtro.
    "umbral_euros": 1_000_000,

    # De noche solo pasa lo urgente (robo, once roto, sistema). Lo demás espera al
    # resumen de la mañana: nada de lo otro se arregla a las 04:00.
    "silencio_activo": True,
    "silencio_desde": "23:30",
    "silencio_hasta": "08:00",

    # Un solo mensaje al día con lo que hay que atender (`/informe/hoy`) en vez de
    # goteo. Se calla si no hay nada, que es la regla de toda la casa.
    "resumen_activo": True,
    "resumen_urgencia_minima": 40,

    # ---------- copia de seguridad a un repositorio privado de github ----------
    # Qué se guarda y qué no (los secretos no salen) está en `respaldo.py`. El token
    # se trata como el de Telegram: nunca vuelve entero al navegador.
    "respaldo_activo": False,
    "respaldo_repo": "",            # "usuario/repositorio"
    "respaldo_rama": "main",
    "respaldo_hora": "04:20",       # de madrugada, separado de los volcados del host
    "respaldo_completa": False,     # incluir la serie de precios (30 MB la primera vez)
    "github_token": "",
    # Solo informativo, lo escribe el propio trabajo: la fecha de la última subida y su
    # huella.
    # Sirve para no volver a subir lo mismo y para que la pantalla diga algo cierto.
    "respaldo_ultima": "",
    "respaldo_huella": "",

    # ---------- quién manda si hay más de una instalación ----------
    # El identificador se crea solo la primera vez. Va aquí (y no en un fichero
    # suelto) para que viaje en la copia: restaurar en el mismo aparato conserva su
    # identidad y, con ella, el mando. El resto lo explica `dispositivo.py`.
    "dispositivo_id": "",
    "dispositivo_nombre": "",
    "mando_cache": None,
    "mando_cache_ts": "",
}

_cache: dict[str, Any] | None = None
_cache_ts = 0.0
_TTL = 15.0


async def crear_tabla() -> None:
    async with Session() as s:
        await s.execute(text(ddl(DDL)))
        await s.commit()


async def leer(refrescar: bool = False) -> dict[str, Any]:
    """Los ajustes efectivos. El `.env` es el valor de partida, la tabla manda."""
    global _cache, _cache_ts
    if not refrescar and _cache is not None and time.time() - _cache_ts < _TTL:
        return _cache

    datos = dict(POR_DEFECTO)
    datos["avisos"] = dict(POR_DEFECTO["avisos"])
    datos["telegram_token"] = settings.telegram_bot_token or ""
    datos["telegram_chat_id"] = str(settings.telegram_chat_id or "")

    try:
        async with Session() as s:
            fila = (await s.execute(text("SELECT valor FROM ajustes WHERE clave='general'"))).scalar()
        if fila:
            guardado = fila if isinstance(fila, dict) else json.loads(fila)
            avisos = {**datos["avisos"], **(guardado.pop("avisos", None) or {})}
            datos.update({k: v for k, v in guardado.items() if v not in (None, "")})
            datos["avisos"] = avisos
    except Exception:
        # Si la tabla aún no existe o la BD no responde, se sigue con el .env: que
        # falle la configuración no puede dejar al sistema sin avisar de un robo.
        log.warning("no se pudieron leer los ajustes; se usan los del .env", exc_info=True)

    _cache, _cache_ts = datos, time.time()
    return datos


async def guardar(cambios: dict[str, Any]) -> dict[str, Any]:
    """Fusiona con lo que ya hay. Un campo ausente no borra el guardado."""
    global _cache
    actual = await leer(refrescar=True)
    nuevo = {
        "telegram_token": cambios.get("telegram_token") or actual["telegram_token"],
        # Vacío = no tocar, igual que el token de Telegram. Para BORRARLA de verdad hay
        # que mandar el literal "-": si no, no habría forma de quitarla nunca.
        "odds_api_key": ("" if cambios.get("odds_api_key") == "-"
                         else cambios.get("odds_api_key") or actual.get("odds_api_key", "")),
        # Mismo trato que los otros dos secretos: vacío = no tocar, "-" = borrar.
        "github_token": ("" if cambios.get("github_token") == "-"
                         else cambios.get("github_token") or actual.get("github_token", "")),
        "telegram_chat_id": str(cambios.get("telegram_chat_id") or actual["telegram_chat_id"]),
        "telegram_activo": bool(cambios.get("telegram_activo", actual["telegram_activo"])),
        "avisos": {**actual["avisos"], **(cambios.get("avisos") or {})},
    }

    # Los ajustes simples se copian por bucle, no enumerándolos a mano en el diccionario
    # de arriba. Enumerarlos hace que cada ajuste nuevo se descarte en silencio: el
    # PUT responde 200 con el valor viejo y el guardado aparenta funcionar. Añadir uno
    # aquí es añadirlo a esta tupla.
    SIMPLES = ("umbral_euros", "rate_limit_rps",
               "silencio_activo", "silencio_desde", "silencio_hasta",
               "resumen_activo", "resumen_urgencia_minima",
               "respaldo_activo", "respaldo_repo", "respaldo_rama", "respaldo_hora",
               "respaldo_completa", "respaldo_ultima", "respaldo_huella",
               "dispositivo_id", "dispositivo_nombre", "mando_cache", "mando_cache_ts")
    for k in SIMPLES:
        nuevo[k] = cambios[k] if cambios.get(k) is not None else actual.get(k, POR_DEFECTO[k])

    # Solo se guardan las categorías que existen: una casilla inventada por el
    # cliente no debe quedarse en la tabla para siempre.
    nuevo["avisos"] = {k: bool(v) for k, v in nuevo["avisos"].items() if k in CATEGORIAS}

    async with Session() as s:
        await s.execute(text("""
            INSERT INTO ajustes (clave, valor) VALUES ('general', :v)
            ON CONFLICT (clave) DO UPDATE SET valor = :v, actualizado = now()"""),
            {"v": json.dumps(nuevo)})
        await s.commit()
    _cache = None
    log.info("ajustes guardados: avisos activos = %s",
             [k for k, v in nuevo["avisos"].items() if v])
    return await leer(refrescar=True)


def enmascarar(token: str) -> str:
    """`123456:AAE…xyz` -> `…xyz`. Suficiente para reconocerlo, inútil para usarlo."""
    return f"…{token[-4:]}" if len(token) > 6 else ("(sin token)" if not token else "…")


async def leer_publico() -> dict[str, Any]:
    d = await leer()
    return {
        "telegram_activo": d["telegram_activo"],
        "telegram_chat_id": d["telegram_chat_id"],
        "telegram_token_pista": enmascarar(d["telegram_token"]),
        "odds_api_key_pista": enmascarar(d.get("odds_api_key", "")),
        "odds_configurado": bool(d.get("odds_api_key")),
        "telegram_configurado": bool(d["telegram_token"] and d["telegram_chat_id"]),
        "avisos": d["avisos"],
        "categorias": CATEGORIAS,
        "umbral_euros": d["umbral_euros"],
        # 0 significa "el del `.env`", así que se enseña el que de verdad está en uso.
        "rate_limit_rps": float(d.get("rate_limit_rps") or settings.rate_limit_rps),
        "silencio_activo": d["silencio_activo"],
        "silencio_desde": d["silencio_desde"],
        "silencio_hasta": d["silencio_hasta"],
        "resumen_activo": d["resumen_activo"],
        "resumen_urgencia_minima": d["resumen_urgencia_minima"],

        "respaldo_activo": d["respaldo_activo"],
        "respaldo_repo": d["respaldo_repo"],
        "respaldo_rama": d["respaldo_rama"],
        "respaldo_hora": d["respaldo_hora"],
        "respaldo_completa": d["respaldo_completa"],
        "respaldo_ultima": d["respaldo_ultima"],
        # El token nunca vuelve entero, igual que los otros dos.
        "github_token_pista": enmascarar(d.get("github_token", "")),
        "respaldo_configurado": bool(d.get("github_token") and d.get("respaldo_repo")),
    }


def _es_urgente(categoria: str | None) -> bool:
    return bool(CATEGORIAS.get(categoria or "", {}).get("urgente"))


def en_silencio(d: dict[str, Any], ahora: dt.time | None = None) -> bool:
    """¿Estamos dentro de la franja de silencio?

    La franja cruza la medianoche (23:30 -> 08:00), así que la comparación no puede
    ser `desde <= t <= hasta`: con esos valores no se cumple nunca y el silencio no
    entraría jamás. Cuando `desde > hasta` la condición es la contraria (fuera del hueco).
    """
    if not d.get("silencio_activo"):
        return False
    try:
        desde = dt.time.fromisoformat(d["silencio_desde"])
        hasta = dt.time.fromisoformat(d["silencio_hasta"])
    except (ValueError, KeyError):
        return False
    t = ahora or dt.datetime.now().time()
    return (desde <= t or t < hasta) if desde > hasta else (desde <= t < hasta)


async def quiere(categoria: str | None, *, euros: int | None = None) -> bool:
    """¿Se manda este aviso? Sin categoría se deja pasar: hay llamadas que no la
    declaran, y perder esos avisos es peor que mandar de más.

    Tres filtros, en orden de lo que cuesta equivocarse:

    1. La categoría: de qué se quiere saber.
    2. El umbral (`euros`): un chollo de 300.000 € no merece una interrupción.
    3. El silencio nocturno: de madrugada solo lo que se arregla de madrugada.

    Los avisos marcados `urgente` (robo, once roto, sistema) se saltan el 2 y el 3:
    que te quiten un jugador a las 04:00 sigue siendo urgente a las 04:00.
    """
    if categoria is None:
        return True
    d = await leer()
    if not d["telegram_activo"]:
        return False
    if not d["avisos"].get(categoria, CATEGORIAS.get(categoria, {}).get("defecto", True)):
        return False
    if _es_urgente(categoria):
        return True
    if euros is not None and euros < int(d.get("umbral_euros") or 0):
        return False
    return not en_silencio(d)
