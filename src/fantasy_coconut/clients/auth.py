"""Autenticación contra el Azure B2C de LaLiga.

No se entra con usuario y contraseña, y no depende del tipo de cuenta: LaLiga no expone
policy ROPC. Las variantes `B2C_1A_ROPC` dan 404 y la de login falla sin llegar a
comprobar la contraseña. Por eso nunca se pide una: no funcionaría, y pedirla en una
pantalla que no es de LaLiga tiene la forma de un phishing.

Login interactivo una vez (`scripts/login.py` o Ajustes, Authorization Code + PKCE) y a
partir de ahí se renueva solo con el refresh token. El bearer que acepta la API es el
`id_token`, no el `access_token`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
from typing import Any

import httpx
import jwt

from ..config import settings
from ..secretos import escribir_json

log = logging.getLogger("coconut.auth")

B2C_BASE = "https://login.laliga.es/laligadspprob2c.onmicrosoft.com/oauth2/v2.0"
POLICY_SIGNIN = "B2C_1A_5ULAIP_PARAMETRIZED_SIGNIN"
CLIENT_ID = "af88bcff-1157-40a0-b579-030728aacf0b"  # cliente nativo
REDIRECT_URI = "authredirect://com.lfp.laligafantasy"
SCOPE = "openid offline_access"
MARGEN = 300  # se renueva 5 min antes de que caduque


class SinCredenciales(Exception):
    """No hay refresh token válido: toca login interactivo (Ajustes o `scripts/login.py`)."""


# Login Authorization Code + PKCE, compartido por `scripts/login.py` y `api/login.py`:
# vive aquí para que un cambio en las constantes de B2C llegue a los dos. Devuelve un
# `refresh_token`, no una cookie capturada, así que se renueva y se revoca desde la cuenta.

def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).decode().rstrip("=")


def construir_autorizacion(prompt: str = "login") -> tuple[str, str, str]:
    """URL de login de LaLiga (que federa a Google), el `code_verifier` PKCE y el `state`.

    `prompt="login"` obliga a autenticarse de nuevo y evita que unas cookies de Google
    acumuladas entren con la cuenta equivocada; `"select_account"` solo muestra el selector.

    El `verifier` no puede bajar al navegador: es el secreto que demuestra que quien canjea
    el `code` es quien pidió la URL. Se guarda en el servidor y se recupera con el `state`.
    """
    verifier = _b64(secrets.token_bytes(32))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    estado = _b64(secrets.token_bytes(16))
    params = {
        "p": POLICY_SIGNIN,
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": estado,
        "nonce": _b64(secrets.token_bytes(16)),
        "prompt": prompt,
    }
    return f"{B2C_BASE}/authorize?" + urllib.parse.urlencode(params), verifier, estado


def extraer_code(pegado: str) -> str:
    """Saca el `code` de lo pegado: la URL entera de `authredirect://...`, el fragmento
    `code=...` o el code pelado. Sin `code=` dentro devuelve lo pegado tal cual."""
    pegado = (pegado or "").strip()
    if "code=" in pegado:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(pegado).query)
            if q.get("code"):
                return q["code"][0]
        except Exception:
            pass
        return pegado.split("code=", 1)[1].split("&")[0]
    return pegado


async def intercambiar_code(code: str, verifier: str) -> dict:
    """Canjea el `code` por tokens y los guarda. Devuelve la respuesta cruda de B2C.

    Deja subir el `httpx.HTTPStatusError`: el llamante decide cómo mostrar un 400, que
    casi siempre es que el code caducó (dura unos minutos).
    """
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{B2C_BASE}/token?p={POLICY_SIGNIN}",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPE,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    r.raise_for_status()
    d = r.json()
    bearer = d.get("id_token") or d.get("access_token", "")
    if not bearer:
        raise ValueError("la respuesta de B2C no traía id_token ni access_token")
    tokens.guardar(bearer, d.get("refresh_token", ""))
    return d


def exp_de(token: str) -> float:
    """Caducidad leída del propio JWT. Sin verificar firma: solo hace falta el `exp`."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
        return float(claims.get("exp", 0))
    except Exception:
        return 0.0


class TokenStore:
    """Guarda el bundle en disco y lo renueva cuando toca. Único por proceso."""

    def __init__(self) -> None:
        self._bearer: str = ""
        self._refresh: str = ""
        self._expira: float = 0.0
        self._lock = asyncio.Lock()
        self._cargar()

    def _cargar(self) -> None:
        try:
            with open(settings.token_cache, encoding="utf-8") as f:
                d = json.load(f)
            self._bearer = d.get("bearer", "")
            self._refresh = d.get("refresh", "")
            self._expira = d.get("expira") or exp_de(self._bearer)
        except FileNotFoundError:
            pass
        except Exception:
            log.warning("no se pudo leer %s; hará falta re-login", settings.token_cache)

    def guardar(self, bearer: str, refresh: str) -> None:
        self._bearer = bearer
        self._refresh = refresh or self._refresh
        self._expira = exp_de(bearer)
        # El refresh token opera la cuenta sin contraseña ni 2FA, así que el fichero se
        # crea ya con permisos restringidos (`secretos.escribir_json`), no después.
        escribir_json(settings.token_cache,
                      {"bearer": self._bearer, "refresh": self._refresh,
                       "expira": self._expira})
        log.info("token guardado; caduca en %.0f min", (self._expira - time.time()) / 60)

    async def bearer(self) -> str:
        """Token listo para usar. Renueva si le quedan menos de 5 minutos."""
        if self._bearer and time.time() < self._expira - MARGEN:
            return self._bearer
        if not self._refresh:
            raise SinCredenciales(
                "no hay refresh token. Ejecuta el login interactivo: "
                "docker compose run --rm worker python scripts/login.py"
            )
        # Una sola renovación aunque la pidan N tareas a la vez.
        async with self._lock:
            if self._bearer and time.time() < self._expira - MARGEN:
                return self._bearer
            await self._renovar()
        return self._bearer

    async def _renovar(self) -> None:
        datos = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh,
            "client_id": CLIENT_ID,
            "scope": SCOPE,
        }
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{B2C_BASE}/token?p={POLICY_SIGNIN}", data=datos,
                             headers={"Content-Type": "application/x-www-form-urlencoded"})

        if r.status_code in (400, 401):
            # Refresh caducado o revocado. El bearer actual no se tira: puede que aún le
            # queden minutos útiles.
            self._refresh = ""
            log.error("refresh rechazado (%d); hace falta re-login interactivo", r.status_code)
            raise SinCredenciales("refresh token inválido; repite scripts/login.py")
        r.raise_for_status()

        d: dict[str, Any] = r.json()
        # La API usa el id_token como bearer; el access_token es el respaldo.
        self.guardar(d.get("id_token") or d.get("access_token", ""),
                     d.get("refresh_token", ""))


tokens = TokenStore()
