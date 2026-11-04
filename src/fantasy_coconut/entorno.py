"""Lectura de las variables de entorno del proyecto.

Todas llevan el prefijo `COCONUT_` y aquí se leen sin él: `var("MODO")` mira
`COCONUT_MODO`. Resolver el prefijo en un solo sitio evita que cada módulo lo escriba a
mano y se equivoque en uno.
"""

from __future__ import annotations

import os

PREFIJO = "COCONUT_"


def var(nombre: str, defecto: str | None = None) -> str | None:
    """Lee `COCONUT_<nombre>`. El nombre va sin prefijo."""
    return os.getenv(PREFIJO + nombre.upper().removeprefix(PREFIJO), defecto)


def bandera(nombre: str, defecto: bool = False) -> bool:
    """Igual, pero para interruptores. Acepta `1`, `true`, `si`, `sí`, `yes`, `on`."""
    v = var(nombre)
    if v is None:
        return defecto
    return v.strip().lower() in ("1", "true", "si", "sí", "yes", "on")
