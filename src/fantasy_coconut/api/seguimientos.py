"""`/seguimiento/*`: vigilancias de larga duración, desde Coconut.

A diferencia de `/programar` (que dispara una acción a una hora), esto crea algo que
se queda mirando hasta completarse y que sobrevive a un reinicio. Ver
`seguimiento.py` para el modelo.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import seguimiento as seg
from .. import vigilantes as _v

router = APIRouter(prefix="/seguimiento", tags=["seguimiento"])


class Nuevo(BaseModel):
    tipo: str = Field(..., description="venta · puja · clausulazo_al_expirar · mi_exposicion")
    params: dict = Field(default_factory=dict)
    revisar_cada: int = Field(300, ge=20, le=86400, description="segundos entre comprobaciones")
    caduca_en_horas: float | None = Field(72, description="null = sin límite")
    nota: str = ""


@router.get("", summary="Vigilancias activas y su historial")
async def listar(estado: str | None = Query(None), limite: int = Query(50, le=200)):
    return {"tipos": sorted(seg.VIGILANTES), "seguimientos": await seg.listar(estado, limite)}


@router.post("", summary="Crear una vigilancia")
async def crear(n: Nuevo):
    try:
        return await seg.crear(n.tipo, n.params, revisar_cada=n.revisar_cada,
                               caduca_en_horas=n.caduca_en_horas, nota=n.nota)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{sid}", summary="Cancelar una vigilancia")
async def cancelar(sid: str):
    if not await seg.cancelar(sid):
        raise HTTPException(404, "no existe o ya no está activa")
    return {"ok": True, "id": sid}


@router.get("/sugerencias", summary="Qué vigilancias tendría sentido crear hoy")
async def sugerencias():
    return {"sin_blindar": await _v.sugerir_desde_plantilla()}
