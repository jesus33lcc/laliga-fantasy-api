"""`/respaldo/*`: copia de seguridad y restauración desde Coconut.

Tres operaciones:

  · `POST /respaldo/probar`    ¿sirven el token y el repositorio? Sin guardar nada.
  · `POST /respaldo/copiar`    sube la copia ahora.
  · `POST /respaldo/restaurar` la trae de vuelta y la mete.

> *Un backup sin restaurar no es un backup, es una carpeta.*

Restaurar no es "para más adelante": una copia que nadie ha devuelto nunca es una
suposición, no una garantía. El ensayo automático vive en `scripts/probar_respaldo.py` y
hace el viaje entero contra una base vacía.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Body, HTTPException, Query

from .. import ajustes as aj
from .. import respaldo as R
from ..respaldo_git import Destino, ErrorGitHub
from ..respaldo_git import comprobar as gh_comprobar
from ..respaldo_git import descargar as gh_descargar
from ..respaldo_git import subir as gh_subir

log = logging.getLogger("coconut.api.respaldo")
router = APIRouter(prefix="/respaldo", tags=["respaldo"])


async def _destino(token: str | None = None, repo: str | None = None,
                   rama: str | None = None) -> Destino:
    """El destino efectivo: lo que venga en la petición manda sobre lo guardado.

    Así `probar` puede validar unas credenciales antes de guardarlas, que es lo que
    evita enterarse de que el token no vale el día que hace falta restaurar.
    """
    d = await aj.leer()
    return Destino(repo=repo or d.get("respaldo_repo", ""),
                   token=token or d.get("github_token", ""),
                   rama=rama or d.get("respaldo_rama") or "main")


def _traducir(e: ErrorGitHub) -> HTTPException:
    """Un fallo de GitHub sale con SU código, no como un 500.

    Un token caducado o un repositorio que no existe no son averías del sistema: son
    respuestas, y devolverlas como 500 manda a mirar el log para nada.
    """
    pistas = {
        401: "el token no vale o ha caducado. Crea uno nuevo en GitHub: Settings > "
             "Developer settings > Personal access tokens.",
        403: "el token no tiene permiso de escritura sobre ese repositorio. Hace falta "
             "el ámbito «repo» (o «Contents: read and write» si es de acceso fino).",
        404: "no existe ese repositorio, o el token no puede verlo. Ojo al formato: "
             "«usuario/repositorio», sin la URL entera.",
    }
    return HTTPException(e.status, f"{e.detalle}. {pistas.get(e.status, '')}".strip())


@router.post("/probar", summary="¿Sirven el token y el repositorio? No guarda nada")
async def probar(cuerpo: dict = Body(default={})):
    try:
        d = await _destino(cuerpo.get("github_token"), cuerpo.get("respaldo_repo"),
                           cuerpo.get("respaldo_rama"))
        info = await gh_comprobar(d)
    except ErrorGitHub as e:
        raise _traducir(e) from e
    if not info["permiso_escritura"]:
        raise HTTPException(403, f"el token puede LEER {info['repo']} pero no escribir. "
                                 f"Sin escritura no se puede guardar la copia.")
    aviso = None if info["privado"] else (
        "⚠️ ese repositorio es PÚBLICO. La copia lleva tus ajustes, tus decisiones y tu "
        "serie de precios: es tu estrategia a la vista.")
    return {"ok": True, **info, "aviso": aviso}


@router.post("/copiar", summary="Sube la copia ahora")
async def copiar(completa: bool | None = Query(None, description="Incluir la serie de precios"),
                 forzar: bool = Query(False, description="Subir aunque no haya cambiado")):
    d = await aj.leer()
    destino = await _destino()
    if not destino.valido:
        raise HTTPException(400, "falta configurar el repositorio y el token en Ajustes")

    con_serie = d.get("respaldo_completa", False) if completa is None else completa
    ficheros = await R.exportar(completa=con_serie)
    huella = R.huella(ficheros)

    # Si no ha cambiado nada, no se sube: un commit vacío cada día ensucia el historial,
    # que es justo lo que aporta guardar la copia en git.
    if not forzar and huella == d.get("respaldo_huella"):
        return {"subido": False, "motivo": "la copia es idéntica a la última", "huella": huella}

    ahora = dt.datetime.now(dt.UTC)
    try:
        res = await gh_subir(destino, ficheros,
                             f"copia {ahora:%Y-%m-%d %H:%M} · {huella}")
    except ErrorGitHub as e:
        raise _traducir(e) from e

    if res.get("subido"):
        await aj.guardar({"respaldo_ultima": ahora.isoformat(), "respaldo_huella": huella})
    return {**res, "huella": huella,
            "tamano_kb": round(sum(len(v) for v in ficheros.values()) / 1024, 1)}


@router.post("/restaurar", summary="Trae la copia del repositorio y la mete")
async def restaurar(reemplazar: bool = Query(
        False, description="Pisar lo que coincida. Sin esto solo AÑADE lo que falte")):
    """Por defecto no borra nada: inserta lo que falte y deja lo que ya hay.

    El momento de restaurar es el peor para descubrir que has sustituido algo bueno por
    algo viejo, así que pisar hay que pedirlo. Y los secretos no vienen en la copia (el
    token de Telegram, la clave de las cuotas y el de GitHub se vuelven a poner a mano),
    por eso `ajustes` se mezcla en vez de sobrescribirse.
    """
    destino = await _destino()
    if not destino.valido:
        raise HTTPException(400, "falta configurar el repositorio y el token en Ajustes")
    try:
        ficheros = await gh_descargar(destino)
    except ErrorGitHub as e:
        raise _traducir(e) from e
    if not ficheros.get("meta.json"):
        raise HTTPException(404, "en ese repositorio no hay ninguna copia de Coconut")

    from ..db import asegurar_tablas
    await asegurar_tablas()          # una base recién creada no tiene todas las tablas
    metido = await R.importar(ficheros, reemplazar=reemplazar)
    log.warning("respaldo restaurado: %s", metido)
    return {"ok": True, "reemplazar": reemplazar, "filas": metido}


@router.get("/estado", summary="Qué se copia, cuándo fue la última y cuánto ocupa")
async def estado():
    d = await aj.leer()
    ficheros = await R.exportar(completa=d.get("respaldo_completa", False))
    huella = R.huella(ficheros)
    import json
    meta = json.loads(ficheros["meta.json"])
    return {
        "configurado": bool(d.get("github_token") and d.get("respaldo_repo")),
        "activo": d.get("respaldo_activo", False),
        "repo": d.get("respaldo_repo", ""),
        "hora": d.get("respaldo_hora", ""),
        "completa": d.get("respaldo_completa", False),
        "ultima": d.get("respaldo_ultima", ""),
        "al_dia": huella == d.get("respaldo_huella"),
        "huella": huella,
        "tamano_kb": round(sum(len(v) for v in ficheros.values()) / 1024, 1),
        "tablas": meta["filas"],
        "no_se_copia": [
            "El token de LaLiga: es la llave de tu cuenta y opera sin contraseña ni 2FA.",
            "El token de Telegram, la clave de las cuotas y el de GitHub.",
            "Usuarios y sesiones de Coconut.",
            "El maestro de jugadores y la temporada 24/25: se vuelven a descargar solos.",
        ],
    }


# ---------- quién manda ----------

@router.get("/mando", summary="Qué dispositivo es el que actúa")
async def mando():
    from ..dispositivo import HORAS, id_local, leer_mando, puedo_actuar
    d = await aj.leer()
    actual = await leer_mando()
    puedo, porque = await puedo_actuar()
    yo = await id_local()
    return {
        "yo": {"id": yo, "nombre": d.get("dispositivo_nombre") or ""},
        "mando": actual or None,
        "es_mio": bool(actual and actual.get("id") == yo),
        "puedo_actuar": puedo,
        "motivo": porque,
        "horas": HORAS,
        # Sin repositorio no hay sitio compartido donde poner el candado, y no tenerlo es
        # lo correcto para quien solo tiene una instalación.
        "disponible": bool(d.get("github_token") and d.get("respaldo_repo")),
    }


@router.post("/mando", summary="Este dispositivo pasa a ser el que actúa")
async def tomar_mando(forzar: bool = Query(
        False, description="Quitárselo a otro que lo tenga vivo")):
    from ..dispositivo import tomar
    try:
        return {"ok": True, "mando": await tomar(forzar=forzar)}
    except PermissionError as e:
        # 409 y no 423: no es que esté bloqueado para siempre, es que hay que decidir si
        # se le quita al otro. El mensaje dice quién lo tiene y cuánto le queda.
        raise HTTPException(409, f"{e} Usa ?forzar=true si este debe mandar.") from e
    except ErrorGitHub as e:
        raise _traducir(e) from e


@router.delete("/mando", summary="Dejar el mando libre")
async def soltar_mando():
    from ..dispositivo import soltar
    await soltar()
    return {"ok": True}
