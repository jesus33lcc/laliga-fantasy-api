"""`/ventas/*`: la política de venta de cada jugador.

Qué cobrar sola y qué traer para aprobar. El módulo `ventas.py` tiene el porqué de cada
modo y la medición que lo respalda; aquí solo está la puerta.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import ventas as V
from .accion import _resolver

router = APIRouter(prefix="/ventas", tags=["ventas"])


class Politica(BaseModel):
    modo: str = Field("prima", description=" · ".join(V.MODOS))
    prima_min: float = Field(3.0, ge=-10, le=50,
                             description="% sobre el valor de mercado, solo para modo=prima")
    no_antes_de_congelar: bool = Field(
        True, description="No cobrar hasta que la jornada en curso haya arrancado")
    relistar: bool = Field(
        True, description="Volver a listarlo si la ficha caduca sin venderse")
    hasta: dt.datetime | None = Field(
        None, description="Fecha límite; al pasar, avisa en vez de seguir esperando")
    nota: str = ""


@router.get("", summary="Las políticas de venta guardadas, y los modos que existen")
async def listar():
    """El catálogo va en la respuesta a propósito: quien consuma esto no tiene por qué
    saberse los modos de memoria, y así la interfaz los pinta sin llevarlos escritos."""
    return {
        "politicas": await V.listar(),
        "modos": {
            "avisar": "No cobra nunca. Cada oferta llega como aviso y la apruebas tú.",
            "prima": "Cobra solo si la oferta supera el valor en el % que pidas. Es la "
                     "táctica medida: esperar rinde +6,4 puntos porcentuales frente a "
                     "aceptar la primera.",
            "valor": "Cobra en cuanto la oferta llega al valor de mercado.",
            "inmediata": "Cobra la mejor oferta del sistema sin esperar. Sigue habiendo "
                         "suelo del 90 %; lo que se pierde es la prima de la paciencia.",
        },
        "por_defecto": V.POR_DEFECTO,
    }


@router.get("/{jugador}", summary="La política de un jugador (la suya o la de por defecto)")
async def ver(jugador: str):
    j = await _resolver(jugador)
    if not j.get("es_mio"):
        raise HTTPException(400, f"{j['nombre']} no es tuyo")
    return {**await V.politica_de(j["player_team_id"]), "nombre": j["nombre"],
            "valor": j.get("valor")}


@router.put("/{jugador}", summary="Fijar la política de venta de un jugador")
async def fijar(jugador: str, cuerpo: Politica):
    j = await _resolver(jugador)
    if not j.get("es_mio"):
        raise HTTPException(400, f"{j['nombre']} no es tuyo")
    try:
        pol = await V.guardar(j["player_team_id"], j["nombre"], **cuerpo.model_dump())
    except ValueError as e:
        # Un modo inventado es un 400, no un 500: el dato está mal, no el servidor.
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "jugador": j["nombre"], "politica": pol}


@router.delete("/{jugador}", summary="Quitar la política (vuelve a la de por defecto)")
async def quitar(jugador: str):
    try:
        j = await _resolver(jugador)
        ptid, nombre = j["player_team_id"], j["nombre"]
    except HTTPException:
        # Un jugador vendido ya no se encuentra en ninguna plantilla, y es justo el
        # caso en que más falta hace borrar su política. Se busca entonces en la propia
        # tabla por el nombre con que se guardó.
        pol = next((p for p in await V.listar()
                    if (p.get("nombre") or "").lower() == jugador.lower()), None)
        if not pol:
            raise
        ptid, nombre = pol["player_team_id"], pol["nombre"]
    if not await V.borrar(ptid):
        raise HTTPException(404, f"{nombre} no tenía política propia")
    return {"ok": True, "jugador": nombre, "politica": V.POR_DEFECTO}
