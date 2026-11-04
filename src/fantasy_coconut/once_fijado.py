"""Once fijado a mano: cuando la decisión humana debe ganar al modelo.

Por qué existe

A veces se fija un once con información externa que el bot no tiene: una previa de
prensa, una rueda de prensa, una lesión que todavía no se ha publicado. Con eso puede
entrar un suplente en lugar de quien el modelo tenía elegido.

Pero `job_alineacion` corre varias veces al día y `alineacion_guardia` cada hora, y
todos llaman a `mejor_once()`: sin este mecanismo, la siguiente pasada sobrescribe
esa decisión sin avisar y guardar un once a mano no sirve de nada.

Cómo funciona

El once fijado vale para una jornada concreta. Mientras esa sea la próxima, el bot lo
respeta y no recalcula. En cuanto la jornada avanza, el fichero caduca solo y el modelo
vuelve a mandar: así un once fijado y olvidado no arrastra una alineación vieja durante
semanas, que sería peor que no tener este mecanismo.

Importante: el bot sigue guardando el once en cada pasada. Lo que cambia es de dónde
sale el payload, no que deje de escribirse. Si esto fallara, se cae al modelo, nunca a
"no alinear".
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .config import settings

log = logging.getLogger("coconut.once_fijado")

_RUTA = os.path.join(os.path.dirname(settings.token_cache) or "/data", "once_fijado.json")


def fijar(payload: dict[str, Any], jornada: int, motivo: str) -> None:
    """Fija un once para una jornada. Sobrescribe el anterior."""
    datos = {"payload": payload, "jornada": int(jornada), "motivo": motivo}
    tmp = _RUTA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _RUTA)
    log.info("once fijado para la J%s: %s", jornada, motivo)


def leer(jornada_objetivo: int) -> dict[str, Any] | None:
    """Devuelve el once fijado si es para esta jornada; si no, None.

    Un once de una jornada pasada se ignora y se borra: mejor que el modelo vuelva a
    mandar que arrastrar una decisión vieja sin que nadie se entere.
    """
    try:
        with open(_RUTA, encoding="utf-8") as f:
            datos = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        log.exception("no se pudo leer %s; se ignora", _RUTA)
        return None

    if int(datos.get("jornada", -1)) != int(jornada_objetivo):
        log.info("el once fijado era para la J%s y ahora toca la J%s: caducado, lo borro",
                 datos.get("jornada"), jornada_objetivo)
        soltar()
        return None
    return datos


def soltar() -> None:
    """Quita el once fijado; el modelo vuelve a decidir."""
    try:
        os.remove(_RUTA)
        log.info("once fijado retirado; vuelve a decidir el modelo")
    except FileNotFoundError:
        pass
