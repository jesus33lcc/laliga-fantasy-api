"""Cuándo abre una jornada. Un solo sitio, porque el cálculo obvio está mal.

Por qué existe

Calcular la apertura como `min(matchDate)` del calendario está mal: el calendario de una
jornada puede traer partidos aplazados de jornadas anteriores, con fechas ya pasadas.

El síntoma no es un error, es una fecha en el pasado: la apertura sale vencida, cualquier
cuenta atrás se vuelve negativa y las guardas que dependen de ella no llegan a
dispararse. Lo usan la guardia del once, la ventana de clausulazo y la de blindaje, así
que el cálculo vive aquí y no repetido en tres sitios.

La regla

Una jornada que aún no ha empezado no puede abrir en el pasado. Así que la apertura es
el primer partido futuro, no el primero a secas. Y cuando la API publica
`openingWeekDate`, ese manda: es un hecho, no una deducción.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from typing import Any

log = logging.getLogger("coconut.jornada")


def _fecha(v: Any) -> dt.datetime | None:
    if isinstance(v, dt.datetime):
        return v
    try:
        return dt.datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def apertura(partidos: Iterable[dict], *, ahora: dt.datetime | None = None,
             etiqueta: str = "") -> dt.datetime | None:
    """Cuándo arranca la jornada, ignorando los partidos ya jugados.

    `partidos` es lo que devuelve `GET /calendar?weekNumber=N`. Devuelve `None` si no
    queda ningún partido por jugar: que para una jornada futura significa que su
    calendario todavía no está publicado.
    """
    ahora = ahora or dt.datetime.now(dt.UTC)
    fechas = [f for f in (_fecha(p.get("matchDate")) for p in (partidos or [])) if f]
    if not fechas:
        return None
    futuras = [f for f in fechas if f > ahora]
    if not futuras:
        return None
    if len(futuras) < len(fechas):
        log.info("%s: %d partido(s) del calendario ya se jugaron (aplazados); "
                 "se ignoran para calcular la apertura",
                 etiqueta or "jornada", len(fechas) - len(futuras))
    return min(futuras)


def apertura_de(semana: dict, partidos: Iterable[dict],
                *, ahora: dt.datetime | None = None,
                etiqueta: str = "") -> dt.datetime | None:
    """Igual, pero prefiriendo `openingWeekDate` cuando la API lo publica.

    `week/current` solo trae el `openingWeekDate` de la jornada actual. Si la actual
    ya está viva (`isLive`), la que interesa preparar es la siguiente, y para esa hay que
    deducirlo del calendario: que es cuando muerde lo de los aplazados.
    """
    if semana and not semana.get("isLive") and semana.get("openingWeekDate"):
        f = _fecha(semana["openingWeekDate"])
        if f:
            return f
    return apertura(partidos, ahora=ahora, etiqueta=etiqueta)
