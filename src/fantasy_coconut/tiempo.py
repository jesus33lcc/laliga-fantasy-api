"""Fechas de corte para el SQL. Sustituyen al `interval` de Postgres.

Por qué existe

`WHERE ts > now() - interval '24 hours'` es sintaxis de Postgres. SQLite no la
entiende, y no hay forma de escribir esa resta que valga en los dos motores: cada uno
tiene sus funciones de fecha (`datetime(x, '-1 day')` frente a `x - interval '1 day'`)
y ninguna es común.

La salida es calcular el corte en Python y pasarlo como parámetro:

    from .tiempo import hace
    await s.execute(text("… WHERE ts > :desde"), {"desde": hace(horas=24)})

Además de ser portable, se lee mejor: la consulta dice *desde cuándo*, y el cuánto está
donde se decide.

Siempre con zona horaria. Una fecha *naive* comparada con una que sí la tiene
lanza `TypeError` al restarlas en Python: pero en SQL no da error: se compara como
texto contra un valor en UTC y el filtro sale desplazado las horas que haya de
diferencia, sin que nada lo diga.
"""

from __future__ import annotations

import datetime as dt

UTC = dt.UTC


def ahora() -> dt.datetime:
    """El instante actual, con zona horaria. UTC, igual que lo que se guarda."""
    return dt.datetime.now(UTC)


def hace(**cuanto: float) -> dt.datetime:
    """Un instante del pasado: `hace(horas=24)`, `hace(dias=7)`, `hace(segundos=300)`.

    Los nombres van en español para que la consulta se lea igual que el resto del
    proyecto; por dentro son los de `timedelta`.
    """
    return ahora() - _delta(**cuanto)


def dentro_de(**cuanto: float) -> dt.datetime:
    """Un instante del futuro: `dentro_de(dias=15)`."""
    return ahora() + _delta(**cuanto)


_NOMBRES = {"segundos": "seconds", "minutos": "minutes", "horas": "hours",
            "dias": "days", "días": "days", "semanas": "weeks"}


def _delta(**cuanto: float) -> dt.timedelta:
    if not cuanto:
        raise TypeError("hace()/dentro_de() necesitan una unidad: horas, dias, …")
    desconocidas = set(cuanto) - set(_NOMBRES)
    if desconocidas:
        raise TypeError(f"unidad(es) no reconocida(s): {sorted(desconocidas)}. "
                        f"Válidas: {sorted(_NOMBRES)}")
    return dt.timedelta(**{_NOMBRES[k]: v for k, v in cuanto.items()})


def horas_entre(despues: dt.datetime | str | None,
                antes: dt.datetime | str | None) -> float | None:
    """Horas entre dos instantes. Sustituye a `EXTRACT(epoch FROM (a-b))/3600`.

    Acepta texto porque un agregado de SQLite (`max(ts)`) devuelve la fecha como
    cadena, no como `datetime`: ver la trampa explicada en `sqlite_compat`.
    """
    from .sqlite_compat import fecha
    a, b = fecha(despues), fecha(antes)
    if a is None or b is None:
        return None
    return (a - b).total_seconds() / 3600
