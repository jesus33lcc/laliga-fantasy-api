"""`/auth/laliga/*`: conectar la cuenta de LaLiga desde Coconut, sin terminal.

El flujo, y por qué es el que es

La cuenta es federada con Google, así que no hay usuario+contraseña (ROPC no funciona
con proveedores federados) ni forma de "capturar la cookie por detrás" (Google bloquea el
login embebido, y con llave FIDO2 es imposible por diseño). El único login válido es
Authorization Code + PKCE, que es el "entra con Google" de toda la vida:

    1. GET  /auth/laliga/url  ->  el navegador abre la página real de Google
    2. inicias sesión (Google + tu llave)
    3. el navegador acaba en `authredirect://…?code=…`; como no sabe abrir ese esquema,
       deja el code en la barra de direcciones
    4. POST /auth/laliga/code  ->  se canjea el code por un id_token + refresh_token

El copiar-pegar del paso 3 es inevitable en web: el `redirect_uri` es un enlace de
app móvil (`authredirect://com.lfp.laligafantasy`) registrado por LaLiga, no por esta app,
así que el navegador no puede volver a una ruta propia. En la APK con WebView
ese paso desaparece, porque un WebView sí intercepta ese esquema de forma nativa.

El verifier no baja al navegador

El `code_verifier` es el secreto que demuestra que quien canjea el code es quien pidió la
URL. Se guarda en el servidor (en /data, para que sobreviva a un reinicio de la API entre
los dos pasos) indexado por el `state`, y se borra al usarse.
"""

from __future__ import annotations

import json
import logging
import os
import time

import httpx
from fastapi import APIRouter, Body, HTTPException

from ..clients.auth import (
    MARGEN,
    construir_autorizacion,
    extraer_code,
    intercambiar_code,
    tokens,
)
from ..clients.fantasy import FantasyAPI
from ..config import settings
from ..secretos import escribir_json

log = logging.getLogger("coconut.api.login")
router = APIRouter(prefix="/auth/laliga", tags=["login LaLiga"])

# Junto al token, en el mismo volumen. Un solo login en curso a la vez (un solo usuario),
# así que un fichero con lo pendiente basta; se limpia al canjear o al caducar.
_PENDIENTE = os.path.join(os.path.dirname(settings.token_cache) or ".",
                          "login_pendiente.json")
_VIDA_PENDIENTE = 900  # 15 min: el code de B2C caduca antes, así que pasado esto no sirve


def _guardar_pendiente(estado: str, verifier: str) -> None:
    # El `verifier` es el secreto que demuestra que quien canjea el `code` es quien
    # pidió la URL: se guarda con permisos restringidos desde que se crea el fichero.
    escribir_json(_PENDIENTE, {"estado": estado, "verifier": verifier, "ts": time.time()})


def _leer_pendiente() -> dict | None:
    try:
        with open(_PENDIENTE, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if time.time() - float(d.get("ts", 0)) > _VIDA_PENDIENTE:
        return None
    return d


@router.get("/estado", summary="¿Está conectada la cuenta de LaLiga?")
async def estado():
    """Lo que la pantalla necesita para decidir qué enseñar. No pide nada a LaLiga."""
    tiene_refresh = bool(tokens._refresh)
    caduca_min = None
    if tokens._expira:
        caduca_min = round((tokens._expira - time.time()) / 60)
    # Conectado = hay refresh token. El bearer caduca cada hora y se renueva solo; lo que
    # de verdad mantiene viva la sesión es el refresh, que dura ~90 días.
    return {
        "conectado": tiene_refresh,
        "bearer_caduca_min": caduca_min,
        "bearer_vigente": bool(tokens._bearer and time.time() < tokens._expira - MARGEN),
        "hay_login_a_medias": _leer_pendiente() is not None,
    }


@router.get("/url", summary="Paso 1: URL para iniciar sesión con Google")
async def url(prompt: str = "login"):
    """Devuelve la URL de login. `prompt=login` obliga a autenticarse (recomendado);
    `prompt=select_account` solo muestra el selector de cuenta."""
    if prompt not in ("login", "select_account"):
        raise HTTPException(400, "prompt debe ser 'login' o 'select_account'")
    enlace, verifier, estado_ = construir_autorizacion(prompt=prompt)
    _guardar_pendiente(estado_, verifier)
    return {
        "url": enlace,
        "estado": estado_,
        "instrucciones": (
            "Abre la URL, inicia sesión con Google. Al terminar el navegador intentará ir "
            "a 'authredirect://…' y fallará: es normal. Copia esa URL entera de la barra de "
            "direcciones y pégala en el paso 2."),
    }


@router.post("/code", summary="Paso 2: pega la URL de vuelta (o el code) para conectar")
async def code(pegado: str = Body(..., embed=True)):
    """Canjea el code por tokens y los guarda. Puede tardar unos segundos."""
    pend = _leer_pendiente()
    if not pend:
        raise HTTPException(409, "no hay ningún login en curso, o ha caducado. "
                                 "Vuelve a empezar por el paso 1.")
    valor = extraer_code(pegado)
    if not valor:
        raise HTTPException(400, "no he encontrado ningún 'code' en lo que has pegado.")

    try:
        d = await intercambiar_code(valor, pend["verifier"])
    except httpx.HTTPStatusError as e:
        cuerpo = e.response.text[:400]
        log.warning("intercambio de code falló (%s): %s", e.response.status_code, cuerpo)
        # El 400 casi siempre es "el code caducó". Se dice claro, con el cuerpo real de
        # B2C, en vez de un 500 que mandaría a mirar el log para nada.
        raise HTTPException(
            400, f"LaLiga rechazó el code ({e.response.status_code}). "
                 "Suele ser que caducó (dura pocos minutos): repite desde el paso 1. "
                 f"Detalle: {cuerpo}") from None
    except ValueError as e:
        raise HTTPException(502, str(e)) from None

    try:
        os.remove(_PENDIENTE)  # un pendiente ya usado no debe poder reintentarse
    except OSError:
        pass

    # Conectar la cuenta no basta: hace falta saber QUÉ liga y QUÉ equipo son los tuyos, y
    # eso en el servidor vive en el `.env` pero en un móvil no existe. Sin descubrirlo, la
    # sesión sale en verde y todo lo demás en rojo. Si falla, el login no se da por malo:
    # la cuenta está conectada de verdad y la identidad se reintenta desde Ajustes.
    ident = {"ok": False, "motivo": "no intentado"}
    try:
        from .. import identidad

        # `bearer` vive en `.deps`, no en `..clients.auth` (ahí está `tokens`, que es el
        # objeto, y `bearer` es su método). Importarlo del sitio equivocado lanza
        # `ImportError`, el `except` de abajo se lo traga y el descubrimiento no ocurre
        # nunca, sin dejar rastro visible.
        from .deps import bearer, cliente
        ident = await identidad.descubrir(FantasyAPI(cliente()), await bearer())
    except Exception as e:
        log.warning("no se pudo descubrir la liga tras el login: %s", e, exc_info=True)
        ident = {"ok": False, "motivo": f"{type(e).__name__}: {e}"}

    caduca_min = round((tokens._expira - time.time()) / 60) if tokens._expira else None
    return {
        "ok": True,
        "conectado": True,
        "refresh_guardado": bool(d.get("refresh_token")),
        "bearer_caduca_min": caduca_min,
        "identidad": ident,
        "aviso": (None if d.get("refresh_token") else
                  " No vino refresh_token: habrá que reconectar cuando caduque el bearer."),
    }
