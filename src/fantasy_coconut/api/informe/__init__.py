"""`/informe/*`: endpoints de consulta, repartidos por dominio.

Este fichero monta el router del paquete y reexporta sus endpoints. El reparto es por
dominio: lo que se consulta junto vive junto.

| módulo | responsabilidad |
|---|---|
| `comun.py` | Constantes y ayudantes compartidos. No declara rutas |
| `equipo.py` | Plantilla, once, puntos y calendario, propios y de rivales |
| `ventanas.py` | Ventanas de clausulazo y blindaje: cuándo se puede atacar y ser atacado |
| `mercado.py` | Mercado, precios e histórico de lo que se paga |
| `panel.py` | El cruce (`/hoy`) y los bloques que lo alimentan |

Los sub-routers no declaran prefijo: `/informe` lo añade este fichero al juntarlos.
Si cada módulo llevara el suyo, las rutas saldrían como `/informe/informe/...` y el
síntoma sería un 404 en toda la interfaz, lejos de la causa.

`scripts/caracterizar.py --comparar` contrasta la forma de las respuestas con la foto
anterior: un endpoint que desaparece, o un campo que deja de devolverse, sale ahí.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import equipo, mercado, panel, ventanas

# Re-exportado para que el paquete se pueda usar como un módulo suelto y quien lo importa
# no tenga que saber en qué fichero cayó cada endpoint: `api/tacticas.py` pide
# `mercado_historico`, y el scheduler y los vigilantes llaman a varios de estos.
from .comun import cobertura
from .equipo import (
    clasificacion_jornada,
    en_vivo,
    enfrentamientos,
    jornada,
    jugador,
    optimo,
    validar_once,
)
from .equipo import equipo as equipo_de
from .mercado import backtest, mercado_historico, mis_ofertas, momentum
from .mercado import mercado as mercado_actual
from .panel import calendario_dificultad, hoy, premium, saldos_rivales
from .ventanas import (
    exposicion,
    ventana_blindaje,
    ventana_clausulazo,
    ventanas_liga,
)

router = APIRouter(prefix="/informe", tags=["informe"])
for _sub in (equipo, ventanas, mercado, panel):
    router.include_router(_sub.router)

# `/informe` a secas. Se registra aquí y no con un decorador en `panel.py` porque los
# sub-routers van sin prefijo: allí la ruta quedaría vacía del todo y FastAPI se niega
# ("Prefix and path cannot be both empty"). Aquí el prefijo existe, así que casa.
@router.get("", summary="El barrido completo de la rutina diaria")
async def completo():
    """Todos los bloques de la rutina diaria en una llamada.

    Devuelve `jornada`, `validacion_once`, `exposicion`, `enfrentamientos` y `mercado`.

    Vive en el índice y no en un módulo de dominio porque agrega endpoints de `equipo`,
    `ventanas` y `mercado`: colocarla en cualquiera de ellos le obligaría a importar a los
    otros dos.
    """
    return {
        "jornada": await equipo.jornada(),
        "validacion_once": await equipo.validar_once(),
        "exposicion": await ventanas.exposicion(),
        "enfrentamientos": await equipo.enfrentamientos(),
        "mercado": await mercado.mercado(),
    }


# Declara qué reexporta el paquete, y es lo que hace intencionados a ojos de `ruff` los
# imports de arriba. Sin esto cada nombre sale como `F401`, y si además van detrás de
# `completo()` suman un `E402` cada uno: de ahí que vayan delante. Con avisos fijos en un
# fichero, un `F401` nuevo y de verdad pasa desapercibido.
__all__ = [
    "backtest",
    "calendario_dificultad",
    "clasificacion_jornada",
    "cobertura",
    "completo",
    "en_vivo",
    "enfrentamientos",
    "equipo_de",
    "exposicion",
    "hoy",
    "jornada",
    "jugador",
    "mercado_actual",
    "mercado_historico",
    "mis_ofertas",
    "momentum",
    "optimo",
    "premium",
    "router",
    "saldos_rivales",
    "validar_once",
    "ventana_blindaje",
    "ventana_clausulazo",
    "ventanas_liga",
]
