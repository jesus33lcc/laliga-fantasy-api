"""Onces probables desde futbolfantasy.com.

Responde a lo que el precio no dice: si va a jugar. Un titular de 100 puntos rinde más
que un suplente de 200. De cada jugador se saca `data-probabilidad`, que la casa publica
ya cuantificada, más lesión, jerarquía y situación.

La ruta es `/laliga/equipos/<slug>` y es HTML servido, sin JavaScript;
`/laliga/alineaciones-probables` da 404. Su `robots.txt` está vacío, o sea que permite el
acceso. Cada página pesa ~1,2 MB y viene en gzip: sin `Accept-Encoding` serían ~24 MB.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger("coconut.onces")

BASE = "https://www.futbolfantasy.com/laliga/equipos"

EQUIPOS = [
    "alaves", "athletic", "atletico", "barcelona", "betis", "celta", "deportivo",
    "elche", "espanyol", "getafe", "levante", "malaga", "osasuna", "racing",
    "rayo-vallecano", "real-madrid", "real-sociedad", "sevilla", "valencia", "villarreal",
]

# Cada jugador es un <div class="jugador_<ID> ..."> seguido de sus atributos data-*.
_BLOQUE = re.compile(r'<div class="jugador_(\d+)[^"]*"((?:\s+data-[\w-]+="[^"]*")+)',
                     re.IGNORECASE)
_ATRIBUTO = re.compile(r'data-([\w-]+)="([^"]*)"')

# Los de balón parado (`data-bpc/bpp/bpfc/bpfd`) no se leen: valen 1000 para todos los
# jugadores de la plantilla, porteros incluidos, o sea que son constantes del maquetado.
# Antes de usar un atributo nuevo, comprobar que varía entre jugadores.


@dataclass
class Jugador:
    ff_id: str
    nombre: str
    equipo: str
    probabilidad: float | None   # 0..1
    posicion: str | None
    lesionado: bool
    sancionado: bool
    apercibido: bool
    jerarquia: int | None      # 0..50: peso en el equipo
    situacion: int | None      # 0..3
    estado: int | None         # 0 / 40 / 80
    edad: int | None
    valor: int | None
    delta_valor: int | None
    rival: str | None
    en_casa: bool | None

    @property
    def disponible(self) -> bool:
        return not (self.lesionado or self.sancionado)


def _pct(v: str | None) -> float | None:
    if not v:
        return None
    m = re.search(r"(\d+)", v)
    return int(m.group(1)) / 100 if m else None


def _int(v: str | None) -> int | None:
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def _bandera(v: str | None) -> bool:
    """Los flags vienen como '0'/'1' o '-1' cuando no aplica."""
    n = _int(v)
    return n is not None and n > 0


def parsear(html: str, equipo: str) -> list[Jugador]:
    vistos: dict[str, Jugador] = {}
    for ff_id, attrs in _BLOQUE.findall(html):
        d = {k.lower(): v for k, v in _ATRIBUTO.findall(attrs)}
        # El mismo jugador aparece en varios div (campo, lista…): se fusionan.
        previo = vistos.get(ff_id)
        nombre = d.get("nombre") or (previo.nombre if previo else "")
        prob = _pct(d.get("probabilidad"))
        if previo and prob is None:
            prob = previo.probabilidad
        if not nombre:
            continue
        vistos[ff_id] = Jugador(
            ff_id=ff_id,
            nombre=nombre.replace("-", " ").strip(),
            equipo=equipo,
            probabilidad=prob,
            posicion=d.get("posicionlaligafantasy") or (previo.posicion if previo else None),
            lesionado=_bandera(d.get("lesion")) or (previo.lesionado if previo else False),
            sancionado=_bandera(d.get("sancionado")) or (previo.sancionado if previo else False),
            apercibido=_bandera(d.get("apercibido")) or (previo.apercibido if previo else False),
            jerarquia=_int(d.get("jerarquia")) if d.get("jerarquia") else (
                previo.jerarquia if previo else None),
            situacion=_int(d.get("situacion")) if d.get("situacion") else (
                previo.situacion if previo else None),
            estado=_int(d.get("estado")) if d.get("estado") else (
                previo.estado if previo else None),
            edad=_int(d.get("edad")) if d.get("edad") else (previo.edad if previo else None),
            valor=_int(d.get("valor-laliga-fantasy")) or (previo.valor if previo else None),
            delta_valor=_int(d.get("valor-diff-laliga-fantasy")) or (
                previo.delta_valor if previo else None),
            rival=d.get("rival") or (previo.rival if previo else None),
            en_casa=(d.get("locvis") == " ") if d.get("locvis") else (
                previo.en_casa if previo else None),
        )
    return list(vistos.values())


async def descargar(equipos: list[str] | None = None,
                    pausa: float = 2.0) -> list[Jugador]:
    """Un barrido completo. Secuencial y con pausa: no hay prisa y el sitio es ajeno."""
    equipos = equipos or EQUIPOS
    salida: list[Jugador] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=40, write=10, pool=30),
        headers={
            "User-Agent": "fantasy-coconut/0.1 (uso privado)",
            "Accept-Encoding": "gzip, deflate",  # sin esto son 24 MB por barrido
        },
        follow_redirects=True,
    ) as c:
        import asyncio
        for i, eq in enumerate(equipos):
            try:
                r = await c.get(f"{BASE}/{eq}")
                if r.status_code != 200:
                    log.warning("%s -> HTTP %d", eq, r.status_code)
                    continue
                js = parsear(r.text, eq)
                salida.extend(js)
                log.info("%-16s %d jugadores", eq, len(js))
            except Exception:
                log.exception("fallo con %s", eq)
            if i < len(equipos) - 1:
                await asyncio.sleep(pausa)
    return salida


# ---------- persistencia ----------

async def _jornada_objetivo() -> int:
    """La jornada que se está preparando: la siguiente si la actual está en juego.

    Misma convención que `job_alineacion` y `once_fijado`. Si falla, cae a la última que
    haya en la tabla: es mejor guardar con una jornada aproximada que no guardar.
    """
    try:
        from ..clients.fantasy import FantasyAPI
        from ..clients.via_api import cliente_lectura
        async with cliente_lectura() as http:
            w = await FantasyAPI(http).jornada_actual()
        n = w.get("nextWeek") if w.get("isLive") else w.get("weekNumber")
        if n:
            return int(n)
    except Exception:
        log.warning("no se pudo leer la jornada actual; se usa la última conocida",
                    exc_info=True)
    # Los imports van aquí, no en quien llama. Este es el camino de respaldo y solo se
    # recorre cuando `week/current` falla: si los nombres los importara otra función,
    # daría `NameError` justo en el momento en que tiene que funcionar.
    from sqlalchemy import text as _text

    from ..db import Session as _Session
    async with _Session() as s:
        return int((await s.execute(_text(
            "SELECT COALESCE(max(jornada), 1) FROM probable_lineups"))).scalar() or 1)


async def ingest_onces(jornada: int | None = None) -> dict[str, int]:
    """Barrido completo -> `probable_lineups`. Cuesta 20 peticiones a futbolfantasy.

    Solo se guarda lo que se ha podido emparejar con un jugador de LaLiga: una
    probabilidad atribuida al jugador equivocado es peor que no tenerla.
    """
    from sqlalchemy import text as _text

    from ..db import Session as _Session
    from .emparejar import emparejar as _emparejar

    ff = await descargar()
    async with _Session() as s:
        laliga = [dict(r._mapping) for r in await s.execute(
            _text("SELECT id, name, team_id FROM players"))]
    if jornada is None:
        # La jornada la manda LaLiga, no la tabla propia. Sacarla de
        # `MAX(jornada) FROM probable_lineups` no avanza nunca, porque la ingesta escribe
        # en la misma jornada que lee: se queda clavada en la primera y todas las
        # probabilidades se guardan con esa etiqueta, pisándose entre ellas.
        jornada = await _jornada_objetivo()

    emp, huerfanos = _emparejar(laliga, ff)
    por_nombre = {e.nombre_ff: e.player_id for e in emp}

    guardados = 0
    async with _Session() as s:
        for j in ff:
            pid = por_nombre.get(j.nombre)
            if pid is None or j.probabilidad is None:
                continue
            await s.execute(_text("""
                INSERT INTO probable_lineups (jornada, team_id, player_id,
                                              probability, source, ts)
                VALUES (:j, :t, :p, :prob, 'futbolfantasy', now())
                ON CONFLICT (player_id, jornada, source) DO UPDATE
                    SET probability = EXCLUDED.probability, ts = now()
            """), {"j": jornada, "t": 0, "p": pid, "prob": j.probabilidad})

            # Y además al histórico, que no pisa. `probable_lineups` tiene
            # `PRIMARY KEY (player_id, jornada, source)` con UPSERT, así que guarda el
            # último valor y solo ese: vale para alinear, pero deja sin datos cualquier
            # medición hacia atrás. Con `ts` en la clave cada toma se conserva, unas
            # 365.000 filas por temporada.
            await s.execute(_text("""
                INSERT INTO probable_lineups_historico
                    (jornada, player_id, probability, source, ts)
                VALUES (:j, :p, :prob, 'futbolfantasy', now())
                ON CONFLICT (player_id, jornada, source, ts) DO NOTHING
            """), {"j": jornada, "p": pid, "prob": j.probabilidad})
            guardados += 1
        await s.commit()

    res = {"descargados": len(ff), "emparejados": len(emp),
           "guardados": guardados, "sin_casar": len(huerfanos)}
    log.info("onces jornada %s: %s", jornada, res)
    return res
