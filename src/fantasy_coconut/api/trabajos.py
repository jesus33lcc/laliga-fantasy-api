"""`/trabajos/*`: qué hace el worker, visto desde Coconut.

Los jobs viven en el proceso del `worker` y la API no los ve. El puente es Postgres: el
worker apunta cada ejecución y aquí se lee. Ver `trabajos.py` para el porqué.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .. import trabajos as t
from ..config import a_dict

router = APIRouter(prefix="/trabajos", tags=["trabajos"])


class Config(BaseModel):
    """Los ajustes son libres porque cada proceso declara los suyos en el catálogo.

    La validación no va aquí: va en `trabajos.guardar_config()`, que solo acepta las
    claves que ese proceso declara. Así el navegador no puede meter el ajuste de un
    proceso en otro ni inventarse campos.
    """
    model_config = {"extra": "allow"}


@router.get("/config", summary="Catálogo y configuración de cada proceso")
async def config():
    """Todo lo que la pantalla necesita para pintarse: qué hace cada proceso, para qué
    sirve, qué cuesta, qué se puede tocar y cómo está configurado ahora."""
    from ..catalogo_jobs import publico
    return {"catalogo": publico(), "config": await t.leer_config()}


@router.put("/config/{nombre}", summary="Guardar la configuración de un proceso")
async def guardar_config(nombre: str, cambios: Config):
    """Surte efecto en ≤10 min sin reiniciar nada: `job_reprogramar` relee y reprograma."""
    try:
        nueva = await t.guardar_config(nombre, a_dict(cambios))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "nombre": nombre, "config": nueva,
            "aviso": "Se aplica en menos de 10 minutos, sin reiniciar el worker."}


@router.get("", summary="Estado de los trabajos del worker")
async def listar():
    """Una fila por job: cuándo corrió, cuánto tardó, si falló y cuántas veces.

    Lo mismo que cuenta `docker compose logs`, pero sin terminal: esto se opera desde el
    móvil.
    """
    from ..catalogo_jobs import publico
    return {"trabajos": await t.resumen(),
            "lanzables": t.LANZABLES,
            "catalogo": publico(),
            "config": await t.leer_config(),
            "peticiones": await t.peticiones(10)}


@router.get("/{nombre}", summary="Historial de un trabajo")
async def detalle(nombre: str, limite: int = Query(20, le=100)):
    return {"nombre": nombre, "historial": await t.historial(nombre, limite)}


@router.post("/{nombre}/lanzar", summary="Lanzar un trabajo a mano")
async def lanzar(nombre: str):
    """Deja la petición en la tabla; el worker la recoge en ≤20 s.

    No ejecuta aquí. El job corre en el worker a propósito: si corriera dentro de la
    API, una ingesta larga bloquearía las peticiones del navegador y un job colgado
    tumbaría `/health`, que es lo que vigila `deadman.sh`.
    """
    try:
        return await t.pedir(nombre)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
