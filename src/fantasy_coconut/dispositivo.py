"""Quién manda cuando hay más de una instalación de Coconut.

Cede el control a un aparato durante un plazo, lo renueva mientras siga vivo y lo libera.
`puedo_actuar()` es la consulta que hace un proceso antes de operar.

Qué evita

Con dos instalaciones (un servidor y el móvil, o dos móviles) el riesgo principal no
es que se pisen los datos: la copia tiene una política de conflictos por tabla, aunque no
reconcilia por sí sola dos instalaciones que hayan divergido. El riesgo es que las dos
actúen: dos pujas por el mismo jugador, dos clausulazos, dos veces el mismo gasto.
LaLiga rechaza el segundo intento *idéntico*, pero no dos operaciones distintas tomadas a
la vez con la misma caja.

Dónde vive el mando, y por qué ahí

No puede ser un ajuste local: sería una preferencia de cada aparato, y un móvil olvidado
en un cajón con el interruptor puesto seguiría actuando. Tampoco puede estar en la base de
datos, porque `locks.py` excluye dentro de una sola base y en el modo sin servidor
cada instalación tiene su propio SQLite: un candado ahí es invisible para el otro aparato.
El único sitio que ven las dos es el repositorio de la copia de seguridad.

Lo que garantiza y lo que no

Es un candado de grano grueso: se reclama por horas (`HORAS`) y se consulta con una
caché de minutos (`CACHE_SEG`). Cubre el caso normal (el servidor opera y abres la app en
el móvil) y no da exclusión mutua instantánea: dentro de la ventana de la caché, dos
aparatos pueden creerse los dos con el mando.

Sin repositorio configurado no hay mando y se permite todo. Quien tiene una sola
instalación no debe encontrarse un candado que no pidió.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import socket
import uuid

from . import ajustes as aj
from .respaldo_git import Destino, ErrorGitHub, escribir_fichero, leer_fichero

log = logging.getLogger("coconut.dispositivo")

FICHERO = "dispositivo.json"

# Cuánto dura el mando sin renovarse. Se renueva en cada copia (a diario) y a mano desde
# Ajustes. Si el aparato que manda desaparece, el mando caduca solo y otro puede tomarlo
# sin que nadie tenga que ir a desbloquear nada, igual que en `locks.py`: un
# candado sin caducidad deja la casa cerrada cuando se cae quien tenía la llave.
HORAS = 36

# Cuánto se fía de lo último que leyó antes de volver a preguntar a GitHub. El mando dura
# horas, así que minutos de desfase no cambian nada, y a cambio actuar no cuesta una
# petición a GitHub cada vez.
CACHE_SEG = 300


def _ahora() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def id_local() -> str:
    """El identificador de ESTA instalación. Se crea la primera vez y no cambia.

    Va en `ajustes` y no en un fichero suelto para que viaje en la copia de seguridad:
    si restauras en el mismo aparato, sigue siendo el mismo y conserva el mando.
    """
    d = await aj.leer()
    if d.get("dispositivo_id"):
        return d["dispositivo_id"]
    nuevo = uuid.uuid4().hex[:12]
    await aj.guardar({"dispositivo_id": nuevo,
                      "dispositivo_nombre": d.get("dispositivo_nombre") or _nombre()})
    log.warning("esta instalación se identifica como %s", nuevo)
    return nuevo


def _nombre() -> str:
    """Algo que un humano reconozca al leer "lo tiene <esto>".

    El hostname no sirve dentro de Docker: es el id del contenedor, así que el mando
    queda a nombre de algo como "bfd48e02627e". Eso convierte en un jeroglífico el
    mensaje que decide si le quitas el mando a otro aparato.

    Por eso manda `COCONUT_DISPOSITIVO` si está puesta, el hostname solo cuando parece un
    nombre de verdad, y si no un texto que al menos dice qué es. En cualquier caso se puede
    cambiar desde Ajustes, que es lo único que garantiza que signifique algo.
    """
    import re

    from .entorno import var

    puesto = (var("DISPOSITIVO") or "").strip()
    if puesto:
        return puesto[:40]
    try:
        h = socket.gethostname()[:40]
    except Exception:
        h = ""
    # Doce caracteres hexadecimales seguidos es un id de contenedor, no un nombre.
    if h and not re.fullmatch(r"[0-9a-f]{12}", h):
        return h
    return "este servidor"


async def _destino() -> Destino:
    d = await aj.leer()
    return Destino(repo=d.get("respaldo_repo", ""), token=d.get("github_token", ""),
                   rama=d.get("respaldo_rama") or "main")


async def leer_mando(refrescar: bool = False) -> dict | None:
    """Quién tiene el mando ahora mismo. `None` = nadie, o no hay repositorio."""
    d = await aj.leer()
    destino = await _destino()
    if not destino.valido:
        return None

    if not refrescar:
        cache, ts = d.get("mando_cache"), d.get("mando_cache_ts")
        if cache is not None and ts:
            try:
                edad = (_ahora() - dt.datetime.fromisoformat(ts)).total_seconds()
                if edad < CACHE_SEG:
                    return cache or None
            except ValueError:
                pass

    try:
        crudo = await leer_fichero(destino, FICHERO)
    except ErrorGitHub as e:
        # Si GitHub no contesta, no se bloquea. Un candado que se cierra cuando se
        # cae la red convierte un problema de conectividad en no poder alinear, que es
        # mucho peor que el riesgo que evita.
        log.warning("no se pudo leer el mando (%s); se permite actuar", e)
        return None

    mando = json.loads(crudo) if crudo else None
    await aj.guardar({"mando_cache": mando or {}, "mando_cache_ts": _ahora().isoformat()})
    return mando


async def puedo_actuar(refrescar: bool = False) -> tuple[bool, str]:
    """`(puedo, motivo)`. El motivo se enseña tal cual cuando la respuesta es que no."""
    mando = await leer_mando(refrescar=refrescar)
    if not mando or not mando.get("id"):
        return True, ""
    if mando["id"] == await id_local():
        return True, ""
    try:
        hasta = dt.datetime.fromisoformat(mando["hasta"])
    except (KeyError, ValueError):
        # Un mando que no se entiende no puede bloquear: sería un candado sin llave.
        log.warning("el mando guardado no se entiende, se ignora: %s", mando)
        return True, ""
    if hasta <= _ahora():
        return True, ""
    quedan = (hasta - _ahora()).total_seconds() / 3600
    # `api/programar.py` reconoce este 423 por cómo empieza el texto.
    return False, (f"el mando lo tiene «{mando.get('nombre') or mando['id']}» "
                   f"durante {quedan:.1f} h más. Solo un dispositivo opera a la vez; "
                   f"puedes tomarlo desde Ajustes si este es el que debe actuar.")


async def tomar(forzar: bool = False) -> dict:
    """Este dispositivo pasa a ser el que actúa.

    Con el mando de otro vivo no se toma salvo `forzar`: quitárselo sin querer al
    servidor desde el móvil es justo el accidente que esto evita.
    """
    destino = await _destino()
    if not destino.valido:
        raise ErrorGitHub(400, "hace falta el repositorio de la copia: es el único sitio "
                               "que los dos dispositivos pueden ver")
    puedo, motivo = await puedo_actuar(refrescar=True)
    if not puedo and not forzar:
        raise PermissionError(motivo)

    d = await aj.leer()
    mando = {
        "id": await id_local(),
        "nombre": d.get("dispositivo_nombre") or _nombre(),
        "desde": _ahora().isoformat(),
        "hasta": (_ahora() + dt.timedelta(hours=HORAS)).isoformat(),
    }
    await escribir_fichero(destino, FICHERO,
                           json.dumps(mando, ensure_ascii=False, indent=2) + "\n",
                           f"mando: {mando['nombre']}")
    await aj.guardar({"mando_cache": mando, "mando_cache_ts": _ahora().isoformat()})
    log.warning("este dispositivo toma el mando hasta %s", mando["hasta"])
    return mando


async def soltar() -> None:
    """Deja el mando libre. Lo que hay que hacer antes de cambiar de teléfono."""
    destino = await _destino()
    if not destino.valido:
        return
    await escribir_fichero(destino, FICHERO, "{}\n", "mando: libre")
    await aj.guardar({"mando_cache": {}, "mando_cache_ts": _ahora().isoformat()})
    log.warning("mando soltado: cualquier dispositivo puede tomarlo")


async def renovar_si_es_mio() -> bool:
    """Alarga el mando si ya es de este dispositivo. Lo llama el trabajo de la copia.

    Renovar en la copia diaria es lo que hace que no haga falta acordarse: mientras el
    aparato que manda siga vivo y copiando, conserva el mando; en cuanto deja de hacerlo,
    caduca solo.
    """
    mando = await leer_mando(refrescar=True)
    if not mando or mando.get("id") != await id_local():
        return False
    await tomar()
    return True
