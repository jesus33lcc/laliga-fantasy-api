"""`/bloqueo/*`: el candado del once.

Por qué existe

Basta con que un proceso venda a un titular después de fijar el once para que, al
congelarse la jornada, el equipo tenga 10 jugadores, y un once incompleto anula la
jornada entera.

`once_fijado.py` evita que el modelo *recalcule* encima de una decisión humana, pero no
impide que algo rompa el once por otro lado. Esto sí: mientras el candado está puesto,

  · `/accion/alineacion` responde 423 y no toca nada,
  · `job_alineacion` no recalcula,
  · y vender a un titular del once bloqueado se rechaza, que es exactamente lo
    que deja el once corto.

El flujo

    DELETE /bloqueo/once  ->  abrir el candado
    (se edita el once)
    POST   /bloqueo/once  ->  cerrarlo, guardando quién está dentro

Al cerrarlo se apunta la lista de `playerTeamId` titulares. Eso es lo que permite
decir "no puedes vender a este, está en el once bloqueado" sin tener que releer nada.

Qué no bloquea

Comprar, pujar, blindar o subir cláusula. Ninguna de esas cosas quita a nadie del
once. El candado protege la alineación, no es un modo pánico.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Body, HTTPException
from sqlalchemy import text

from ..db import Session, ddl

log = logging.getLogger("coconut.api.bloqueo")
router = APIRouter(prefix="/bloqueo", tags=["bloqueo"])

DDL = """
CREATE TABLE IF NOT EXISTS bloqueos (
    recurso   text PRIMARY KEY,
    motivo    text NOT NULL DEFAULT '',
    datos     jsonb NOT NULL DEFAULT '{}',
    creado    timestamptz NOT NULL DEFAULT now()
)
"""


async def crear_tabla() -> None:
    async with Session() as s:
        await s.execute(text(ddl(DDL)))
        await s.commit()


async def estado(recurso: str = "once") -> dict | None:
    """`None` si está abierto. Nunca lanza: un fallo al consultar el candado no puede
    impedir alinear, porque no alinear cuesta más que alinear de más."""
    try:
        async with Session() as s:
            f = (await s.execute(
                text("SELECT recurso, motivo, datos, creado FROM bloqueos WHERE recurso=:r"),
                {"r": recurso})).first()
        return dict(f._mapping) if f else None
    except Exception:
        log.warning("no se pudo leer el candado; se asume abierto", exc_info=True)
        return None


async def exigir_abierto(recurso: str = "once") -> None:
    """Corta la acción si el candado está echado. 423 Locked es el código exacto."""
    b = await estado(recurso)
    if b:
        raise HTTPException(
            423, f"el {recurso} está BLOQUEADO desde {b['creado']:%d-%b %H:%M} "
                 f"({b['motivo'] or 'sin motivo'}). Ábrelo con DELETE /bloqueo/{recurso}.")


async def titulares_bloqueados() -> set[str]:
    """Los `playerTeamId` que no se pueden vender ahora mismo."""
    b = await estado("once")
    return set((b or {}).get("datos", {}).get("titulares", []))


@router.get("", summary="¿Hay algún candado puesto?")
async def ver():
    b = await estado("once")
    return {
        "once": {
            "bloqueado": bool(b),
            "motivo": (b or {}).get("motivo"),
            "desde": (b or {}).get("creado"),
            "titulares_protegidos": len((b or {}).get("datos", {}).get("titulares", [])),
        }
    }


@router.post("/once", summary="Echar el candado al once")
async def bloquear(motivo: str = Body("fijado a mano", embed=True)):
    """Guarda quién está en el once ahora. Si luego alguien intenta vender a uno de
    ellos, se rechaza con su nombre: no con un error genérico."""
    from ..config import settings
    from .deps import api, bearer, cliente, protegido

    tok = await bearer()
    # `FantasyAPI` no tiene método de alineación: esa ruta la sirve `puente.py` a pelo.
    # `ttl=0` a propósito: bloquear un once cacheado bloquearía el once de hace un rato.
    lineup = await protegido(cliente().get(
        api()._p(f"/teams/{settings.fantasy_team_id}/lineup"), ttl=0, auth=tok))
    f = lineup.get("formation") or {}
    titulares = []
    for clave in ("goalkeeper", "defender", "midfield", "striker"):
        bloque = f.get(clave) or []
        for p in (bloque if isinstance(bloque, list) else [bloque]):
            if (ptid := str(p.get("playerTeamId") or "")):
                titulares.append(ptid)

    if len(titulares) != 11:
        raise HTTPException(
            409, f"el once tiene {len(titulares)} jugadores, no 11. No se bloquea un once "
                 f"roto: eso congelaría el fallo en vez de evitarlo.")

    datos = {"titulares": titulares, "guardado": datetime.now(UTC).isoformat()}
    async with Session() as s:
        await s.execute(text("""
            INSERT INTO bloqueos (recurso, motivo, datos) VALUES ('once', :m, :d)
            ON CONFLICT (recurso) DO UPDATE SET motivo=:m, datos=:d, creado=now()"""),
            {"m": motivo, "d": json.dumps(datos)})
        await s.commit()
    log.warning("ONCE BLOQUEADO (%s): %d titulares protegidos", motivo, len(titulares))
    return {"ok": True, "bloqueado": True, "titulares": len(titulares), "motivo": motivo}


@router.delete("/once", summary="Abrir el candado del once")
async def desbloquear():
    async with Session() as s:
        r = await s.execute(text("DELETE FROM bloqueos WHERE recurso='once' RETURNING recurso"))
        await s.commit()
        habia = bool(r.first())
    log.warning("once DESBLOQUEADO" if habia else "el once ya estaba abierto")
    return {"ok": True, "bloqueado": False, "estaba_bloqueado": habia}
