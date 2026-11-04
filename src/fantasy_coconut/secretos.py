"""Escribir un fichero con un secreto dentro, sin dejarlo expuesto ni a medias.

Dos problemas, y los dos se ven solo mirando el orden de las operaciones:

- Permisos. `open(ruta, "w")` crea el fichero con lo que dicte el umask, que por
  defecto es `0644`: legible por cualquier usuario de la máquina. Hacer `chmod` DESPUÉS
  de escribir deja una ventana en la que el secreto ya está en disco y todavía es público.
  Aquí el fichero nace con `0600`.
- Escritura a medias. Un corte a mitad de `write()` deja un JSON truncado, y lo que se
  guarda aquí (un refresh token, un `code_verifier`) sin él obliga a repetir el login. Se
  escribe en un temporal y se mueve con `os.replace`, que es atómico.
"""

from __future__ import annotations

import json
import os
from typing import Any

MODO = 0o600


def escribir_json(ruta: str, datos: Any) -> None:
    """Guarda `datos` como JSON en `ruta`, solo legible por su dueño."""
    os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
    tmp = ruta + ".tmp"
    # `os.open` con el modo puesto: el fichero nunca llega a existir con permisos amplios.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, MODO)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(datos, f)
    except BaseException:
        # Si algo falla, no se deja un temporal con el secreto rondando.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Hace falta porque `O_CREAT` no cambia el modo de un fichero que ya existe: un
    # temporal dejado por una ejecución anterior con otro umask conservaría sus permisos.
    # Nunca da acceso a nadie más: `0600` no tiene bits de grupo ni de otros.
    os.chmod(tmp, MODO)
    os.replace(tmp, ruta)
