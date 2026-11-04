"""Avisos por Telegram, filtrados por categoría.

Hace falta un bot propio: dos procesos consultando `getUpdates` del mismo bot se roban
los mensajes.

Qué decide si un aviso sale, y son dos caminos distintos

  · Con categoría manda el interruptor de Ajustes (`ajustes.quiere()`), más el umbral
    en euros y el silencio nocturno. El nivel no interviene, así que también salen
    avisos que no son emergencias cuando su categoría está encendida.
  · Sin categoría se aplica el filtro de nivel: solo pasa `EMERGENCIA`, salvo con
    `COCONUT_ALERTAS=todas` (útil para depurar).

Un aviso solo vale si hace que alguien actúe

Por eso las categorías informativas (chollos, cláusulas rivales, "alineación guardada")
vienen apagadas de fábrica: quedan en el log, que es donde se consultan a posteriori,
y no interrumpen en el móvil. Encendidas solo están las que hay que saber sin preguntar:
te han clausulado a alguien, no se puede alinear un once legal, el sistema se ha roto, una
tarea programada se ha ejecutado, o una búsqueda tuya del mercado ha encontrado algo.

Antirrebote aparte: solo se avisa si hay acción. Un bot que manda "todo normal" cada
dos horas se acaba silenciando, y el día que importa no lo lees.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

import httpx

from . import entorno as _env

log = logging.getLogger("coconut.alerts")

# Niveles. Deciden solo en los avisos SIN categoría: con categoría manda el
# interruptor de Ajustes.
EMERGENCIA = "emergencia"
INFO = "info"


def solo_emergencias() -> bool:
    return _env.var("ALERTAS", "emergencias").strip().lower() != "todas"

# Antirrebote: no repetir el mismo aviso en horas. La ventana de cláusula dura 48 h y
# el poller pasa cada 2 h; sin esto, el mismo evento avisa 24 veces.
#
# Vive en la BD y no en memoria a propósito: con un diccionario, cada reinicio del worker
# borra el historial y se vuelve a mandar todo.
_SILENCIO = 6 * 3600


async def enviar(texto: str, *, clave: str | None = None,
                 silencio: float = _SILENCIO, nivel: str = INFO,
                 categoria: str | None = None, euros: int | None = None) -> bool:
    """Manda un aviso. `clave` identifica el evento para no repetirlo.

    `categoria` es lo que decide: se mira en los ajustes, que se cambian desde Coconut
    sin reiniciar nada. Un aviso sin categoría se filtra solo por `nivel` +
    `COCONUT_ALERTAS`, para no perder los que no la declaran.

    `euros` es lo que hay en juego, si el aviso se mide en dinero. Sirve para el
    umbral: por debajo de él no interrumpe. Los avisos urgentes lo ignoran.
    """
    from . import ajustes as _aj
    cfg = await _aj.leer()

    if categoria is not None:
        if not await _aj.quiere(categoria, euros=euros):
            log.info("[%s desactivado] %s", categoria, " | ".join(texto.split("\n"))[:200])
            return False
    elif nivel != EMERGENCIA and solo_emergencias():
        # No se pierde: queda en el log, que es donde se consulta a posteriori.
        log.info("[no-emergencia, no enviado] %s", " | ".join(texto.split("\n"))[:200])
        return False

    token, chat = cfg["telegram_token"], cfg["telegram_chat_id"]
    if not token or not chat:
        log.warning("Telegram sin configurar; aviso descartado:\n%s", texto)
        return False

    if clave:
        from sqlalchemy import text as _text

        from .db import Session as _Session
        from .tiempo import hace
        h = hashlib.sha256(clave.encode()).hexdigest()[:32]
        async with _Session() as s:
            # Insert-or-refresh atómico: solo se envía si no hay uno reciente.
            enviar_ahora = (await s.execute(_text("""
                INSERT INTO alert_log (clave, enviado) VALUES (:k, now())
                ON CONFLICT (clave) DO UPDATE SET enviado = now()
                 WHERE alert_log.enviado < :corte
                RETURNING 1
            """), {"k": h, "corte": hace(segundos=silencio)})).scalar()
            await s.commit()
        if not enviar_ahora:
            log.debug("aviso repetido, silenciado: %s", clave)
            return False

    # Una emergencia insiste; lo demás, un intento. El envío está en el camino de jobs que
    # corren cada pocos minutos, así que reintentar todo los haría esperar por un aviso
    # que igual ni interrumpe.
    ok = await _mandar(token, chat, texto,
                       intentos=3 if nivel == EMERGENCIA else 1)

    # Si no salió, se suelta la reserva del antirrebote.
    #
    # El apunte se hace ANTES de enviar y tiene que seguir así: es lo que impide que dos
    # procesos manden el mismo aviso a la vez. Pero dejarlo puesto tras un fallo silencia
    # ese aviso durante `silencio` segundos (6 h por defecto), así que un timeout de red
    # no cuesta un mensaje: cuesta la ventana entera. Soltarlo devuelve el derecho a
    # reintentar al siguiente que lo detecte.
    if not ok and clave:
        from sqlalchemy import text as _text2

        from .db import Session as _Session2
        try:
            async with _Session2() as s:
                await s.execute(_text2("DELETE FROM alert_log WHERE clave = :k"), {"k": h})
                await s.commit()
            log.warning("aviso no enviado (%s): se suelta el antirrebote para reintentar",
                        clave)
        except Exception:
            log.exception("no se pudo soltar el antirrebote de %s", clave)
    return ok


async def _mandar(token: str, chat: str, texto: str, *, intentos: int = 1) -> bool:
    """El envío en crudo. Aparte, para que la pantalla de ajustes pueda probar unas
    credenciales nuevas SIN guardarlas antes.

    Qué se reintenta y qué no, porque no da igual:

      · Red caída o timeout, y 5xx de Telegram: transitorio, se repite.
      · 429: Telegram dice cuánto esperar en `retry_after`; se respeta, con tope.
      · Cualquier otro 4xx: token mal, chat_id mal o HTML inválido. Insistir manda la
        misma petición al mismo error y con el token dentro. Se corta.

    `intentos=1` por defecto: la pantalla de ajustes quiere una respuesta rápida, y un
    aviso informativo no justifica tener esperando al job que lo manda.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    cuerpo = {"chat_id": chat, "text": texto, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    for intento in range(1, max(1, intentos) + 1):
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(url, json=cuerpo)
            if r.status_code < 400:
                if intento > 1:
                    log.warning("aviso enviado al intento %d", intento)
                return True
            espera = _espera_telegram(r)
            if espera is None:
                # No transitorio: no se reintenta y el texto crudo va al log, que es lo
                # que dice qué está mal en la configuración.
                log.error("Telegram respondió %d: %s", r.status_code, r.text[:200])
                return False
            log.warning("Telegram respondió %d; reintento %d/%d en %.0fs",
                        r.status_code, intento, intentos, espera)
        except Exception as e:
            if intento >= intentos:
                log.exception("no se pudo enviar el aviso")
                return False
            espera = min(2.0 * intento, 5.0)
            log.warning("fallo de red al avisar (%s); reintento %d/%d en %.0fs",
                        type(e).__name__, intento, intentos, espera)
        if intento >= intentos:
            return False
        await asyncio.sleep(espera)
    return False


def _espera_telegram(r: httpx.Response) -> float | None:
    """Segundos a esperar antes de repetir, o `None` si el error no es transitorio."""
    if r.status_code == 429:
        try:
            return min(float((r.json().get("parameters") or {}).get("retry_after") or 5), 30.0)
        except Exception:
            return 5.0
    return 3.0 if r.status_code >= 500 else None
