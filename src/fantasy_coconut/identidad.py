"""Qué liga y qué equipo son los tuyos. Se descubren al conectar la cuenta.

`cargar()` rellena `settings.fantasy_league_id` y `settings.fantasy_team_id` al arrancar,
`descubrir()` los averigua preguntando a LaLiga y `guardar()` los persiste en `ajustes`.

El agujero que cierra

En el servidor esos dos valores vienen del `.env`, puestos a mano al instalar. En un móvil
no hay `.env`: quedan vacíos y `/estado` no sabe qué liga mirar, así que la cuenta se
conecta bien y la app sigue diciendo "falta conectar tu cuenta".

Por qué se escribe sobre `settings`

`settings` es un objeto mutable y quien lo consulta lee el atributo en el momento de
usarlo, así que basta con rellenarlo al arrancar para que todos los puntos de lectura vean
el valor bueno. La alternativa (que cada uno consulte otra fuente) es un cambio mecánico
repartido por muchos ficheros, con una ocasión de olvido en cada sitio.

Precedencia: manda el entorno. Si el `.env` trae liga y equipo, `cargar()` no los pisa.
El autodescubrimiento es para quien no los tiene, no para corregir a quien sí.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from .config import settings
from .db import Session, json_param

log = logging.getLogger("coconut.identidad")

CLAVE = "identidad"


async def cargar() -> dict[str, str]:
    """Lee la identidad guardada y la mete en `settings`. Se llama al arrancar."""
    # Lo que venga del entorno manda: en el servidor está en el `.env` y es correcto.
    if settings.fantasy_league_id and settings.fantasy_team_id:
        return {"liga": settings.fantasy_league_id, "equipo": settings.fantasy_team_id,
                "origen": "entorno"}
    try:
        async with Session() as s:
            fila = (await s.execute(
                text("SELECT valor FROM ajustes WHERE clave = :k"), {"k": CLAVE})).scalar()
    except Exception:
        log.warning("no se pudo leer la identidad guardada", exc_info=True)
        return {}
    if not fila:
        return {}
    d = json.loads(fila) if isinstance(fila, str) else fila
    if d.get("liga") and d.get("equipo"):
        settings.fantasy_league_id = str(d["liga"])
        settings.fantasy_team_id = str(d["equipo"])
        log.info("identidad cargada: liga %s · equipo %s", d["liga"], d["equipo"])
        return {**d, "origen": "guardada"}
    return {}


async def guardar(liga: str, equipo: str, nombre_liga: str = "",
                  nombre_equipo: str = "") -> None:
    d = {"liga": str(liga), "equipo": str(equipo),
         "nombre_liga": nombre_liga, "nombre_equipo": nombre_equipo}
    async with Session() as s:
        await s.execute(text(f"""
            INSERT INTO ajustes (clave, valor) VALUES (:k, {json_param(':v')})
            ON CONFLICT (clave) DO UPDATE
               SET valor = {json_param(':v')}, actualizado = now()"""),
            {"k": CLAVE, "v": json.dumps(d)})
        await s.commit()
    settings.fantasy_league_id = d["liga"]
    settings.fantasy_team_id = d["equipo"]
    log.warning("identidad guardada: %s (%s) · equipo %s (%s)",
                nombre_liga or liga, liga, nombre_equipo or equipo, equipo)


async def descubrir(cli, token: str) -> dict[str, Any]:
    """Pregunta a LaLiga cuál es tu liga y cuál es tu equipo dentro de ella.

    Tres peticiones, y cada una hace falta:
      1. `/v4/user/me` tu id de usuario. Sin él no se puede saber cuál de los
         equipos de la liga es el tuyo: todos tienen el mismo aspecto.
      2. `/leagues` tus ligas.
      3. `/leagues/{id}/standing` los equipos, con el `manager` de cada uno.

    Devuelve `{ok, liga, equipo, ligas:[...]}`. Si hay varias ligas no elige por ti:
    devuelve la lista para que la decidas. Elegir a ciegas sería empezar a operar sobre
    una liga que no es la que te interesa.
    """
    yo = await cli.usuario(token)
    mi_id = str(yo.get("id") or yo.get("userId") or "")
    if not mi_id:
        return {"ok": False, "motivo": "LaLiga no devolvió tu id de usuario"}

    ligas = await cli.ligas(token)
    if isinstance(ligas, dict):
        ligas = ligas.get("leagues") or ligas.get("data") or []
    if not ligas:
        return {"ok": False, "motivo": "tu cuenta no tiene ninguna liga privada"}

    resumen = [{"id": str(liga_.get("id")), "nombre": liga_.get("name") or "(sin nombre)"}
               for liga_ in ligas]
    if len(ligas) > 1:
        return {"ok": False, "motivo": "tienes varias ligas: elige una",
                "ligas": resumen}

    liga = resumen[0]
    equipos = await cli.clasificacion(token, liga["id"])
    if isinstance(equipos, dict):
        equipos = equipos.get("data") or equipos.get("teams") or []

    # El equipo y el manager viven dentro de `team`, no en la raíz de la fila: `/standing`
    # trae `{livePoints, points, position, previousPosition, team}` y es `team` quien lleva
    # `{id, manager, managerId, teamMoney, teamValue}`. Leerlos en la raíz devuelve None
    # sin dar error. `manager` se acepta como objeto o como id suelto: equivocarse aquí
    # deja al usuario operando sobre el equipo de otro.
    mio = None
    for fila in equipos:
        eq = fila.get("team") or fila          # por si algún día viene en la raíz
        m = eq.get("manager")
        ident = str((m or {}).get("id") if isinstance(m, dict) else (m or "")) \
            or str(eq.get("managerId") or "")
        if ident and ident == mi_id:
            mio = eq
            break
    if mio is None:
        return {"ok": False, "motivo": "no encuentro tu equipo en esa liga",
                "ligas": resumen}

    m = mio.get("manager") if isinstance(mio.get("manager"), dict) else {}
    nombre_eq = mio.get("name") or m.get("managerName") or ""
    await guardar(liga["id"], str(mio.get("id")), liga["nombre"], nombre_eq)
    return {"ok": True, "liga": liga,
            "equipo": {"id": str(mio.get("id")), "nombre": nombre_eq}}
