"""`/mercado/buscar` y `/mercado/avisos`: filtrar el mercado y que avise solo.

Para qué

El mercado se renueva cada día a las 13:48 con una baraja nueva. Mirarlo a mano es
justo lo que se deja de hacer a los tres días, y ahí es donde se pierden los fichajes.

Aquí se guarda una búsqueda una vez ("delanteros que no sean míos con más de 15
puntos") y el sistema la revisa en cada renovación y avisa por Telegram solo si algo
la cumple. Si no hay nada, silencio: un aviso que llega todos los días deja de leerse.

Qué se puede filtrar

Posición, puntos acumulados, precio, cuántas pujas lleva y si es agente libre. El filtro
`libre` separa los dos mercados: un agente libre lo cierra sola la subasta, y una ficha de
otro manager depende de que su dueño acepte la oferta.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..config import a_dict, settings
from ..db import Session
from .deps import api, bearer, protegido

log = logging.getLogger("coconut.api.buscar")
router = APIRouter(prefix="/mercado", tags=["mercado"])

POS = {1: "POR", 2: "DEF", 3: "MED", 4: "DEL", 5: "ENT"}
POS_ID = {v: k for k, v in POS.items()}


class Filtro(BaseModel):
    nombre: str = Field(..., min_length=1, max_length=60, description="Para reconocerlo en el aviso")
    posicion: str | None = Field(None, description="POR · DEF · MED · DEL · ENT")
    min_puntos: int = Field(0, ge=0, description="Puntos acumulados en la temporada")
    max_precio: int | None = Field(None, ge=0, description="En euros")
    solo_libres: bool = Field(
        True, description="Solo agentes libres, que se compran con /accion/pujar. Con "
                          "`false` entran también las fichas de otro manager, que van "
                          "por /accion/ofertar y dependen de que él acepte")
    max_pujas: int | None = Field(None, ge=0, description="Descarta lo que ya tiene competencia")
    activo: bool = True


async def _mercado_crudo() -> Any:
    """El mercado, venga de donde venga.

    `deps.cliente()` solo existe dentro del proceso de la API (se abre en su
    `lifespan`). Este módulo también lo usa `job_mercado_avisos`, que corre en el
    *worker*: allí hay que abrir un cliente propio, que con el puente activo sigue
    saliendo por la API igualmente.
    """
    tok = await bearer()
    try:
        return await protegido(api().mercado(tok, settings.fantasy_league_id))
    except HTTPException as e:
        if e.status_code != 503:
            raise
    from ..clients.fantasy import FantasyAPI
    from ..clients.via_api import cliente_lectura
    async with cliente_lectura() as http:
        return await FantasyAPI(http).mercado(tok, settings.fantasy_league_id)


async def _fichas() -> list[dict[str, Any]]:
    """El mercado, normalizado. Una ficha de rival y una de agente libre no se leen
    igual: el contador de competencia se llama distinto en cada una."""
    crudo = await _mercado_crudo()
    out = []
    for f in (crudo if isinstance(crudo, list) else crudo.get("items", [])):
        pm = f.get("playerMaster") or {}
        libre = f.get("discr") == "marketPlayerLeague"
        out.append({
            "nombre": pm.get("nickname"),
            "posicion": POS.get(int(pm.get("positionId") or 0), "?"),
            "valor": int(pm.get("marketValue") or 0),
            "precio": int(f.get("salePrice") or 0),
            "puntos": int(pm.get("points") or 0),
            "media": pm.get("averagePoints"),
            "libre": libre,
            # `numberOfBids` en agentes libres, `numberOfOffers` en fichas de rival: leer
            # siempre el primero deja las de rival con "0 pujas", o sea con pinta de
            # chollo sin competencia cuando ya tienen ofertas encima.
            "competencia": int((f.get("numberOfBids") if libre else f.get("numberOfOffers")) or 0),
            "expira": f.get("expirationDate"),
            "market_id": f.get("id"),
            "estado": pm.get("playerStatus"),
        })
    return out


def _cumple(ficha: dict, f: dict) -> bool:
    if f.get("solo_libres", True) and not ficha["libre"]:
        return False
    if f.get("posicion") and ficha["posicion"] != f["posicion"]:
        return False
    if ficha["puntos"] < int(f.get("min_puntos") or 0):
        return False
    if f.get("max_precio") is not None and ficha["precio"] > int(f["max_precio"]):
        return False
    if f.get("max_pujas") is not None and ficha["competencia"] > int(f["max_pujas"]):
        return False
    # Un `out_of_league` no puede puntuar: no es un fichaje, es tirar el dinero.
    return ficha["estado"] != "out_of_league"


@router.get("/buscar", summary="Filtrar el mercado de ahora mismo")
async def buscar(
    posicion: str | None = Query(None, description="POR · DEF · MED · DEL · ENT"),
    min_puntos: int = Query(0, ge=0),
    max_precio: int | None = Query(None, ge=0),
    solo_libres: bool = Query(True),
    max_pujas: int | None = Query(None, ge=0),
):
    if posicion and posicion.upper() not in POS_ID:
        raise HTTPException(400, f"posición desconocida: {posicion}. Válidas: {list(POS_ID)}")
    f = {"posicion": posicion.upper() if posicion else None, "min_puntos": min_puntos,
         "max_precio": max_precio, "solo_libres": solo_libres, "max_pujas": max_pujas}
    todas = await _fichas()
    hallazgos = [x for x in todas if _cumple(x, f)]
    hallazgos.sort(key=lambda x: (-x["puntos"], x["precio"]))
    return {"filtro": f, "en_mercado": len(todas), "encontrados": len(hallazgos),
            "fichas": hallazgos}


# ---------- avisos guardados ----------

async def _leer_filtros() -> list[dict]:
    try:
        async with Session() as s:
            v = (await s.execute(
                text("SELECT valor FROM ajustes WHERE clave='filtros_mercado'"))).scalar()
        return (v if isinstance(v, list) else json.loads(v)) if v else []
    except Exception:
        log.warning("no se pudieron leer los filtros de mercado", exc_info=True)
        return []


async def _guardar_filtros(lista: list[dict]) -> None:
    async with Session() as s:
        await s.execute(text("""
            INSERT INTO ajustes (clave, valor) VALUES ('filtros_mercado', :v)
            ON CONFLICT (clave) DO UPDATE SET valor=:v, actualizado=now()"""),
            {"v": json.dumps(lista)})
        await s.commit()


@router.get("/avisos", summary="Búsquedas guardadas que se revisan en cada mercado")
async def ver_avisos():
    return await _leer_filtros()


@router.post("/avisos", summary="Guardar una búsqueda para que avise sola")
async def crear_aviso(f: Filtro):
    if f.posicion and f.posicion.upper() not in POS_ID:
        raise HTTPException(400, f"posición desconocida: {f.posicion}")
    lista = await _leer_filtros()
    if any(x["nombre"].lower() == f.nombre.lower() for x in lista):
        raise HTTPException(409, f"ya hay un aviso llamado {f.nombre!r}")
    d = a_dict(f)
    if d["posicion"]:
        d["posicion"] = d["posicion"].upper()
    lista.append(d)
    await _guardar_filtros(lista)
    log.info("aviso de mercado guardado: %s", f.nombre)
    return {"ok": True, "avisos": lista}


@router.delete("/avisos/{nombre}", summary="Quitar una búsqueda guardada")
async def borrar_aviso(nombre: str):
    lista = await _leer_filtros()
    nueva = [x for x in lista if x["nombre"].lower() != nombre.lower()]
    if len(nueva) == len(lista):
        raise HTTPException(404, f"no hay ningún aviso llamado {nombre!r}")
    await _guardar_filtros(nueva)
    return {"ok": True, "avisos": nueva}


async def revisar_avisos() -> dict:
    """Evalúa los filtros guardados contra el mercado actual y avisa solo si hay algo.

    Lo llama `job_mercado_avisos` justo después de la renovación de las 13:48. El
    silencio cuando no hay nada es deliberado: un bot que escribe cada día se acaba
    silenciando, y el día que aparece el portero bueno no lo lees.
    """
    from ..alerts import enviar

    filtros = [f for f in await _leer_filtros() if f.get("activo", True)]
    if not filtros:
        return {"filtros": 0, "avisados": 0}

    fichas = await _fichas()
    avisados = 0
    for f in filtros:
        hallazgos = sorted([x for x in fichas if _cumple(x, f)],
                           key=lambda x: (-x["puntos"], x["precio"]))[:6]
        if not hallazgos:
            continue
        lineas = [f"🔎 <b>{f['nombre']}</b> · {len(hallazgos)} en el mercado"]  # sin-rastro-ok: lo lee el usuario en Telegram
        for x in hallazgos:
            lineas.append(f"   {x['posicion']} <b>{x['nombre']}</b> · {x['puntos']} pts · "
                          f"{x['precio']/1e6:.2f}M" +
                          (f" · {x['competencia']} pujas" if x["competencia"] else ""))
        # La clave lleva a quién se avisa: si mañana sale la misma gente, no se repite.
        clave = "mercado:" + f["nombre"] + ":" + ",".join(x["nombre"] or "?" for x in hallazgos)
        if await enviar("\n".join(lineas), clave=clave, silencio=20 * 3600,
                        categoria="mercado_filtro"):
            avisados += 1
    log.info("avisos de mercado: %d filtros, %d avisados", len(filtros), avisados)
    return {"filtros": len(filtros), "avisados": avisados}
