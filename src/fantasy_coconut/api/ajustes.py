"""`/ajustes`: configurar el bot de Telegram y qué avisos llegan al móvil.

Vive en la tabla `ajustes`, no en el `.env`: cambiar una variable de entorno obliga a
recrear el contenedor (`restart` no la relee), y un aviso que cuesta una operación
acaba no tocándose nunca. Desde aquí surte efecto en 15 s, que es lo que dura la caché.

`GET` nunca devuelve el token entero, solo sus cuatro últimos caracteres: un token
de bot permite escribir en el chat, y no hace falta que baje al navegador para pintar
un formulario.

`POST /ajustes/probar` manda un mensaje de prueba con las credenciales que se le
pasen, sin guardarlas. Así se comprueba que el token y el chat son correctos antes
de dejarlos puestos, en vez de guardar algo roto y enterarse el día del clausulazo.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import ajustes as aj
from ..alerts import _mandar
from ..config import a_dict

log = logging.getLogger("coconut.api.ajustes")
router = APIRouter(prefix="/ajustes", tags=["ajustes"])


class Cambios(BaseModel):
    telegram_token: str | None = Field(None, description="Vacío = no tocar el guardado")
    telegram_chat_id: str | None = None
    telegram_activo: bool | None = None
    alineacion_horas: list[str] | None = Field(
        None, description='Horas del cron de alineación, "HH:MM". No vive en `ajustes`: '
                          'se guarda en `jobs_config`, que es de donde lee el scheduler. '
                          'Se aplica en <=10 min, cuando pasa `job_reprogramar`')
    avisos: dict[str, bool] | None = Field(None, description="categoría recibir sí/no")

    # Cuánto ruido se tolera. Los avisos urgentes (robo, once roto, sistema) se saltan
    # el umbral y el silencio: que te roben de madrugada sigue siendo urgente.
    umbral_euros: int | None = Field(
        None, ge=0, description="Por debajo no se avisa de lo que se mide en dinero. 0 = sin filtro")

    # El tope de 3,0 no es técnico: es el punto a partir del cual dejas de parecerte al
    # cliente oficial, que se autoimpone 1 req/s. Se admite pasar de él a propósito, pero
    # no una cifra que cante desde fuera. `0` devuelve el control al `.env`.
    rate_limit_rps: float | None = Field(
        None, ge=0, le=3.0,
        description="Peticiones por segundo hacia LaLiga. El cliente oficial usa 1,0. "
                    "0 = el valor del .env. Se aplica en menos de un minuto")
    silencio_activo: bool | None = None
    silencio_desde: str | None = Field(None, description='"HH:MM". Puede cruzar medianoche')
    silencio_hasta: str | None = None
    resumen_activo: bool | None = Field(None, description="Un mensaje al día con lo que hay que atender")
    resumen_urgencia_minima: int | None = Field(None, ge=0, le=100)

    # Clave de the-odds-api para P(portería a cero). Mismo trato que el token de
    # Telegram: vacío = no tocar; el literal "-" la borra.
    odds_api_key: str | None = Field(
        None, description='Clave de the-odds-api. Vacío = no tocar. "-" la borra')


class Prueba(BaseModel):
    telegram_token: str | None = None
    telegram_chat_id: str | None = None


class PruebaOdds(BaseModel):
    odds_api_key: str | None = None


async def _horas_alineacion() -> list[str]:
    """Las horas del cron de alineación, leídas de donde las lee el scheduler.

    Viven en `jobs_config`, la misma fila que edita la pestaña Motor, y no en la tabla
    `ajustes`. Guardar aquí una segunda copia daría dos valores para un solo horario y el
    worker solo mira uno: cambiar el otro no haría nada ni daría error.
    """
    from ..trabajos import leer_config
    try:
        return list((await leer_config("alineacion")).get("horas") or [])
    except Exception:
        log.warning("no se pudo leer el horario de alineación", exc_info=True)
        return []


@router.get("", summary="Ajustes actuales y catálogo de avisos")
async def ver():
    return {**await aj.leer_publico(), "alineacion_horas": await _horas_alineacion()}


@router.put("", summary="Guardar ajustes (fusiona: lo que no mandes no se borra)")
async def guardar(c: Cambios):
    import re
    HORA = r"([01]\d|2[0-3]):[0-5]\d"
    horas = list(c.alineacion_horas or [])
    horas += [h for h in (c.silencio_desde, c.silencio_hasta) if h]
    for h in horas:
        if not re.fullmatch(HORA, h):
            raise HTTPException(400, f"hora inválida: {h!r}. Formato HH:MM en 24 h.")
    desconocidas = set(c.avisos or {}) - set(aj.CATEGORIAS)
    if desconocidas:
        raise HTTPException(400, f"categorías que no existen: {sorted(desconocidas)}. "
                                 f"Válidas: {sorted(aj.CATEGORIAS)}")
    if c.alineacion_horas is not None:
        from ..trabajos import guardar_config
        await guardar_config("alineacion", {"horas": list(c.alineacion_horas)})
    await aj.guardar(a_dict(c, exclude_none=True))
    return await ver()


@router.post("/probar", summary="Mandar un mensaje de prueba SIN guardar nada")
async def probar(p: Prueba):
    actual = await aj.leer()
    token = p.telegram_token or actual["telegram_token"]
    chat = p.telegram_chat_id or actual["telegram_chat_id"]
    if not token or not chat:
        raise HTTPException(400, "faltan el token o el chat_id")

    ok = await _mandar(token, chat, "🥥 <b>LaLiga Fantasy Coconut</b>\n"
                                    "Prueba de configuración: si lees esto, los avisos funcionan.")
    if not ok:
        raise HTTPException(502,
            "Telegram no lo ha aceptado. Comprueba el token en @BotFather y que hayas "
            "escrito al bot al menos una vez desde ese chat (si no, no puede contestarte).")
    return {"ok": True, "enviado_a": chat}


@router.post("/probar-odds", summary="Comprobar la clave de the-odds-api SIN guardarla")
async def probar_odds(p: PruebaOdds):
    """Verifica la clave contra el endpoint de deportes, que no consume créditos.

    Se prueba antes de guardar por la misma razón que el token de Telegram: enterarse
    de que la clave no vale el día que hace falta la predicción es tarde. Y se usa
    `/v4/sports` a propósito: la documentación de the-odds-api dice que esa ruta no
    descuenta de la cuota, así que comprobar es gratis.
    """
    import httpx
    actual = await aj.leer()
    clave = p.odds_api_key or actual.get("odds_api_key", "")
    if not clave:
        raise HTTPException(400, "no hay ninguna clave que probar")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get("https://api.the-odds-api.com/v4/sports",
                            params={"apiKey": clave})
    except Exception as e:
        raise HTTPException(502, f"no se pudo contactar con the-odds-api: {e}") from None

    if r.status_code == 401:
        raise HTTPException(401, "la clave no es válida (401). Revísala en tu panel de "
                                 "the-odds-api.com.")
    if r.status_code >= 400:
        raise HTTPException(502, f"the-odds-api respondió {r.status_code}: {r.text[:200]}")

    deportes = r.json() if r.headers.get("content-type", "").startswith("application/json") else []
    ligas = [d for d in deportes if isinstance(d, dict)
             and "spain" in (d.get("title", "") + d.get("group", "")).lower()]
    return {
        "ok": True,
        # La cuota viaja en cabeceras, no en el cuerpo: es lo que dice cuánto queda de
        # los 500 créditos del mes sin tener que fiarse de la cuenta propia.
        "creditos_restantes": r.headers.get("x-requests-remaining"),
        "creditos_usados": r.headers.get("x-requests-used"),
        "deportes_disponibles": len(deportes),
        "liga_espanola": [d.get("key") for d in ligas] or None,
        "nota": ("Comprobada con /v4/sports, que no consume créditos. "
                 "LaLiga es `soccer_spain_la_liga`."),
    }
