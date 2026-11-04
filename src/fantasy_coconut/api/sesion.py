"""`/auth/*`: login, logout y sesión permanente.

Segunda capa por debajo de la VPN: la interfaz ejecuta operaciones con dinero, así que
quien alcance el puerto puede pujar, vender o clausular.

Apagado por defecto (`COCONUT_AUTH=0`), porque encenderlo sin usuarios dados de alta deja
fuera al propio dueño: primero `scripts/crear_usuario.py`, luego la variable. Cookie
`HttpOnly` en vez de token en `localStorage`, `scrypt` de la biblioteca estándar y tokens
guardados hasheados, así que leer la tabla `sesiones` no da sesiones utilizables.

El flujo de Google está escrito y probado por debajo, no contra un navegador. Necesita
credenciales de Google Cloud Console; sin ellas `/auth/google/inicio` devuelve 503
diciendo qué falta.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from .. import entorno as _env
from ..db import Session, ddl

log = logging.getLogger("coconut.api.sesion")
router = APIRouter(prefix="/auth", tags=["sesión"])

COOKIE = "coconut_sesion"
DIAS_SESION = 400            # el techo que aplican los navegadores a una cookie
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}

DDL = """
CREATE TABLE IF NOT EXISTS usuarios (
    id          bigserial PRIMARY KEY,
    email       text UNIQUE NOT NULL,
    nombre      text NOT NULL DEFAULT '',
    clave       text,                      -- scrypt$sal$hash; NULL si solo entra por Google
    google_sub  text UNIQUE,               -- identificador estable de Google
    creado      timestamptz NOT NULL DEFAULT (now()),
    ultimo_acceso timestamptz
);
CREATE TABLE IF NOT EXISTS sesiones (
    token_hash  text PRIMARY KEY,          -- nunca el token en claro
    usuario_id  bigint NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    creada      timestamptz NOT NULL DEFAULT (now()),
    expira      timestamptz NOT NULL,
    agente      text
);
CREATE INDEX IF NOT EXISTS idx_sesiones_usuario ON sesiones (usuario_id);
"""


def auth_activa() -> bool:
    return _env.var("AUTH", "0") not in ("0", "", "no", "false")


async def crear_tablas() -> None:
    """Crea las tablas si no están.

    Los comentarios `--` se quitan antes de partir por `;`: uno que contenga un punto y
    coma ("-- scrypt$sal$hash; NULL si…") trocea la sentencia por la mitad y la tabla no
    llega a crearse.
    """
    limpio = re.sub(r"--[^\n]*", "", ddl(DDL))
    async with Session() as s:
        for sentencia in limpio.split(";"):
            if sentencia.strip():
                await s.execute(text(sentencia))
        await s.commit()


# ---------- contraseñas ----------

def cifrar(clave: str) -> str:
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(clave.encode(), salt=sal, **SCRYPT)
    return f"scrypt${base64.b64encode(sal).decode()}${base64.b64encode(h).decode()}"


def verificar(clave: str, guardado: str | None) -> bool:
    """Comparación en tiempo constante. Devuelve False sin reventar si el formato
    no es el esperado: un registro corrupto no debe dejar entrar ni dar un 500."""
    if not guardado:
        return False
    try:
        algo, sal_b64, hash_b64 = guardado.split("$")
        if algo != "scrypt":
            return False
        h = hashlib.scrypt(clave.encode(), salt=base64.b64decode(sal_b64), **SCRYPT)
        return hmac.compare_digest(h, base64.b64decode(hash_b64))
    except Exception:
        log.warning("registro de clave con formato inesperado")
        return False


# ---------- sesiones ----------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def abrir_sesion(usuario_id: int, response: Response, agente: str = "") -> str:
    token = secrets.token_urlsafe(32)
    expira = datetime.now(UTC) + timedelta(days=DIAS_SESION)
    async with Session() as s:
        await s.execute(text("""INSERT INTO sesiones (token_hash, usuario_id, expira, agente)
                                VALUES (:t, :u, :e, :a)"""),
                        {"t": _hash_token(token), "u": usuario_id, "e": expira, "a": agente[:300]})
        await s.execute(text("UPDATE usuarios SET ultimo_acceso = now() WHERE id = :u"),
                        {"u": usuario_id})
        await s.commit()
    response.set_cookie(
        COOKIE, token,
        max_age=DIAS_SESION * 86400,
        httponly=True,                       # fuera del alcance del JS de la página
        samesite="lax",                      # no viaja en peticiones de otros sitios
        secure=_env.var("HTTPS", "0") == "1",
        path="/",
    )
    return token


async def usuario_de(token: str | None) -> dict | None:
    if not token:
        return None
    async with Session() as s:
        fila = (await s.execute(text("""
            SELECT u.id, u.email, u.nombre FROM sesiones s
            JOIN usuarios u ON u.id = s.usuario_id
            WHERE s.token_hash = :t AND s.expira > now()"""),
            {"t": _hash_token(token)})).first()
    return dict(fila._mapping) if fila else None


async def usuario_actual(coconut_sesion: str | None = Cookie(None)) -> dict | None:
    """Dependencia laxa: devuelve el usuario o None. No exige nada."""
    return await usuario_de(coconut_sesion)


async def exigir_usuario(coconut_sesion: str | None = Cookie(None),
                         x_clave_servicio: str | None = Header(None)) -> dict:
    """Dependencia estricta para lo que mueve dinero.

    Con `COCONUT_AUTH=0` deja pasar a todo el mundo a propósito: la VPN es entonces la
    única puerta, y encender la autenticación es una decisión aparte.

    La clave de servicio

    El `worker` no es una persona: no puede tener contraseña ni cookie, pero necesita
    llamar a `/accion/alineacion` para alinear. Si al encender `COCONUT_AUTH=1` no
    tuviera por dónde entrar, el equipo dejaría de alinearse y eso son 0 puntos.

    Por eso acepta una cabecera `X-Clave-Servicio` comparada en tiempo constante
    contra `COCONUT_CLAVE_SERVICIO`. Si la variable está vacía, la vía no existe: no se
    puede colar nadie por "no configurado".
    """
    if not auth_activa():
        return {"id": 0, "email": "sin-auth", "nombre": "acceso por VPN"}

    esperada = _env.var("CLAVE_SERVICIO", "")
    if esperada and x_clave_servicio and hmac.compare_digest(x_clave_servicio, esperada):
        return {"id": -1, "email": "worker", "nombre": "proceso interno"}

    u = await usuario_de(coconut_sesion)
    if not u:
        raise HTTPException(401, "sesión no válida: vuelve a entrar")
    return u


# ---------- rutas ----------

class Credenciales(BaseModel):
    # `str` y no `EmailStr`: esa exige el paquete `email-validator`, que no está en la
    # imagen y hace que la API arranque y muera en bucle. Para un panel de un solo
    # usuario basta comprobar que hay algo antes y después de una arroba, y son cero
    # dependencias nuevas que auditar.
    email: str = Field(..., min_length=5, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    clave: str = Field(..., min_length=8, max_length=256)


@router.get("/yo", summary="Quién está dentro, y si hace falta estarlo")
async def yo(u: dict | None = Depends(usuario_actual)):
    return {"auth_activa": auth_activa(), "usuario": u,
            "google": bool(os.getenv("GOOGLE_CLIENT_ID"))}


@router.post("/login", summary="Entrar con correo y contraseña")
async def login(c: Credenciales, response: Response, request: Request):
    async with Session() as s:
        fila = (await s.execute(text("SELECT id, clave FROM usuarios WHERE email = :e"),
                                {"e": c.email.lower()})).first()
    # Se cifra igualmente cuando el usuario no existe, para que el tiempo de
    # respuesta no revele qué correos están dados de alta.
    guardado = fila[1] if fila else cifrar(secrets.token_hex(16))
    if not fila or not verificar(c.clave, guardado):
        raise HTTPException(401, "correo o contraseña incorrectos")
    await abrir_sesion(fila[0], response, request.headers.get("user-agent", ""))
    log.info("sesión abierta para %s", c.email)
    return {"ok": True, "email": c.email}


@router.post("/logout", summary="Cerrar esta sesión")
async def logout(response: Response, coconut_sesion: str | None = Cookie(None)):
    if coconut_sesion:
        async with Session() as s:
            await s.execute(text("DELETE FROM sesiones WHERE token_hash = :t"),
                            {"t": _hash_token(coconut_sesion)})
            await s.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.post("/logout-todo", summary="Cerrar la sesión en todos los dispositivos")
async def logout_todo(response: Response, u: dict = Depends(exigir_usuario)):
    async with Session() as s:
        await s.execute(text("DELETE FROM sesiones WHERE usuario_id = :u"), {"u": u["id"]})
        await s.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


# ---------- google ----------

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_INFO = "https://openidconnect.googleapis.com/v1/userinfo"

_estados: dict[str, float] = {}      # state -> caduca (defensa contra CSRF)


def _google_config() -> tuple[str, str, str]:
    cid = os.getenv("GOOGLE_CLIENT_ID")
    secreto = os.getenv("GOOGLE_CLIENT_SECRET")
    redirect = os.getenv("GOOGLE_REDIRECT", "http://TU_SERVIDOR:8140/auth/google/callback")
    if not cid or not secreto:
        raise HTTPException(503,
            "Google no está configurado. Hacen falta GOOGLE_CLIENT_ID y GOOGLE_CLIENT_SECRET "
            "en el .env, creados en Google Cloud Console, con este redirect autorizado: "
            f"{redirect}")
    return cid, secreto, redirect


@router.get("/google/inicio", summary="Empezar el login con Google")
async def google_inicio():
    cid, _, redirect = _google_config()
    estado = secrets.token_urlsafe(24)
    _estados[estado] = datetime.now(UTC).timestamp() + 600
    params = {
        "client_id": cid, "redirect_uri": redirect, "response_type": "code",
        "scope": "openid email profile", "state": estado,
        "access_type": "online", "prompt": "select_account",
    }
    return {"url": f"{GOOGLE_AUTH}?{urllib.parse.urlencode(params)}"}


@router.get("/google/callback", summary="Vuelta de Google", include_in_schema=False)
async def google_callback(code: str, state: str, response: Response):
    ahora = datetime.now(UTC).timestamp()
    for k, v in list(_estados.items()):
        if v < ahora:
            _estados.pop(k, None)
    if _estados.pop(state, None) is None:
        raise HTTPException(400, "state desconocido o caducado: reinicia el login")

    cid, secreto, redirect = _google_config()
    async with httpx.AsyncClient(timeout=20) as c:
        t = await c.post(GOOGLE_TOKEN, data={
            "code": code, "client_id": cid, "client_secret": secreto,
            "redirect_uri": redirect, "grant_type": "authorization_code"})
        if t.status_code != 200:
            raise HTTPException(400, f"Google rechazó el código: {t.text[:200]}")
        acceso = t.json()["access_token"]
        info = (await c.get(GOOGLE_INFO, headers={"Authorization": f"Bearer {acceso}"})).json()

    sub, email = info.get("sub"), (info.get("email") or "").lower()
    if not sub or not email:
        raise HTTPException(400, "Google no ha devuelto identidad utilizable")

    # No se crean cuentas solas: esto es un panel privado, no un servicio con
    # registro. La cuenta se da de alta a mano y aquí solo se enlaza.
    async with Session() as s:
        fila = (await s.execute(
            text("SELECT id FROM usuarios WHERE google_sub = :g OR email = :e"),
            {"g": sub, "e": email})).first()
        if not fila:
            raise HTTPException(403,
                f"{email} no tiene cuenta aquí. Créala con scripts/crear_usuario.py")
        await s.execute(text("UPDATE usuarios SET google_sub = :g WHERE id = :i"),
                        {"g": sub, "i": fila[0]})
        await s.commit()
    await abrir_sesion(fila[0], response)
    response.status_code = 303
    response.headers["location"] = "/app/"
    return {"ok": True}
