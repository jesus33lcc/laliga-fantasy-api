"""Ingesta de los datos privados de la liga: managers, plantillas y tablón.

De aquí salen la cláusula de cada jugador (`buyoutClause`) y hasta cuándo está protegido
del clausulazo (`buyoutClauseLockedEndTime` = su fecha de compra + 14 días).

Coste: 1 agregado (`/leagues/{lg}/teams`) + 1 plantilla por manager + 1 saldo (solo el
equipo propio) + 1 por página del tablón, hasta `paginas`, parando en la primera vacía.
El agregado trae además `fixturePoints`, con el que se detecta que una jornada no está
puntuando.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text

from ..alerts import EMERGENCIA, enviar
from ..clients.fantasy import FantasyAPI
from ..config import settings
from ..db import Session, json_param
from ..tipos_actividad import nombre as _nombre_tipo

log = logging.getLogger("coconut.ingest.league")

UPSERT_MANAGER = text("""
    INSERT INTO managers (id, name, is_me, balance_known, team_value, points, updated_at)
    VALUES (:id, :name, :is_me, :balance, :team_value, :points, now())
    ON CONFLICT (id) DO UPDATE SET
        name          = EXCLUDED.name,
        is_me         = EXCLUDED.is_me,
        balance_known = COALESCE(EXCLUDED.balance_known, managers.balance_known),
        team_value    = EXCLUDED.team_value,
        points        = EXCLUDED.points,
        updated_at    = now()
""")

INSERT_SQUAD = text("""
    INSERT INTO squad_snapshots
        (ts, manager_id, player_id, player_team_id, buyout_clause, clause_lock_until,
         shielded, purchase_price)
    VALUES (:ts, :manager_id, :player_id, :player_team_id, :clause, :lock_until,
        :shielded,
        -- La API no expone `purchasePrice`: se recupera del tablón, que sí publica
        -- el importe de cada compra.
        --
        -- Las DOS formas de adquirir, no solo `market_buy`: un clausulazo también es una
        -- compra y su importe es el precio pagado. Sin el `clausulazo` aquí, una ficha
        -- fichada por cláusula nace con el precio a NULL en todos los snapshots, y sin
        -- precio de compra no se distingue una cláusula subida a mano de una compra cara
        -- con el valor ya caído, que es lo que hace falta para medir la caja rival.
        --
        -- El filtro por `manager_id` ya se queda con el lado del comprador: en un
        -- clausulazo paga `manager_id` y cobra `counterparty`.
        (SELECT e.amount FROM league_events e
          WHERE e.kind IN ('market_buy', 'clausulazo') AND e.player_id = :player_id
            AND e.manager_id = :manager_id
          ORDER BY e.ts DESC LIMIT 1))
    ON CONFLICT (ts, manager_id, player_id) DO NOTHING
""")

# La clave de dedupe lleva columnas que pueden ser NULL, y dos NULL no colisionan en
# ninguno de los dos motores: sin `NULLS NOT DISTINCT` en Postgres, o el `coalesce` del
# índice en SQLite, el tablón se duplicaría entero en cada sondeo.
INSERT_EVENT = text("""
    INSERT INTO league_events (ts, kind, manager_id, player_id, amount, raw)
    VALUES (:ts, :kind, :manager_id, :player_id, :amount, {RAW})
    ON CONFLICT DO NOTHING
""".replace("{RAW}", json_param(":raw")))

# El evento se guarda entero en `raw`, así que un tipo que hoy no se interpreta se puede
# reinterpretar hacia atrás sin perder nada. El que más información da es el 4, blindar:
# cuesta un anuncio y es público, así que señala qué jugador teme perder cada rival.


def _num(v: Any) -> int | None:
    if v in (None, "", "null"):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _fecha(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


async def _guardar_managers(equipos: list[dict], yo: str) -> list[tuple[int, str]]:
    """Los managers de la liga. Devuelve `(manager_id, team_id)` de cada uno."""
    managers: list[tuple[int, str]] = []
    async with Session() as s:
        for eq in (equipos or []):
            mid = _num(eq.get("managerId"))
            tid = str(eq.get("id") or "")
            if mid is None or not tid:
                continue
            await s.execute(UPSERT_MANAGER, {
                "id": mid,
                "name": (eq.get("manager") or {}).get("managerName") or f"manager-{mid}",
                "is_me": tid == yo,
                # La API solo expone `teamMoney` del equipo propio. El saldo rival se
                # estima con la contabilidad del tablón: lo hace `models/saldos.py` y lo
                # guarda en la columna `balance_est`.
                "balance": None,
                "team_value": _num(eq.get("teamValue")),
                "points": _num(eq.get("teamPoints")) or 0,
            })
            managers.append((mid, tid))
        await s.commit()
    return managers


async def _guardar_saldo(s, api: FantasyAPI, token: str, mid: int, tid: str,
                         plantilla: dict) -> None:
    """El saldo, que solo llega en la plantilla propia.

    `teamInvestment` es el dinero ya comprometido en ofertas pendientes y no se descuenta
    de `teamMoney`. Sin guardarlo, el sistema cree tener más caja de la que puede gastar y
    promete dos compras con el mismo dinero. Viene en otro endpoint, pero solo hay que
    pedirlo una vez por ronda.
    """
    comprometido = 0
    try:
        money = await api.saldo(token, str(tid))
        if isinstance(money, dict):
            comprometido = _num(money.get("teamInvestment")) or 0
    except Exception:
        log.warning("no se pudo leer teamInvestment de %s", tid)
    await s.execute(
        text("UPDATE managers SET balance_known=:b, balance_committed=:c, "
             "updated_at=now() WHERE id=:id"),
        {"b": _num(plantilla["teamMoney"]), "c": comprometido, "id": mid},
    )


async def _guardar_estado(s, pid: int, jugador: dict) -> tuple[str, str] | None:
    """El `playerStatus` y la DEMARCACIÓN de la plantilla mandan sobre los del maestro.

    Las dos fuentes discrepan: el maestro puede dar `ok` a un jugador que la plantilla da
    `injured`. Como `job_players` ingiere el del maestro, sin esto la base diría `ok` y
    `mejor_once()` alinearía a un lesionado. La plantilla es la que ve la app.

    Con la posición pasa lo mismo y cuesta más caro. El maestro se sirve con
    `Cache-Control: max-age=28800`, así que un cambio de demarcación tarda hasta 8 h en
    llegar. Mientras tanto el modelo alinea al jugador en su línea vieja, LaLiga lo
    descarta al guardar el once y queda un once de diez: jornada anulada. Devuelve
    `(viejo, nuevo)` cuando la posición cambia, para que el llamante pueda avisar.
    """
    maestro = jugador.get("playerMaster") or {}
    estado = maestro.get("playerStatus")
    if estado:
        await s.execute(
            text("UPDATE players SET status=:e, updated_at=now() "
                 "WHERE id=:i AND status IS DISTINCT FROM :e"),
            {"e": estado, "i": pid})

    pos = maestro.get("positionId")
    if pos in (None, ""):
        return None
    pos = int(pos)
    anterior = (await s.execute(
        text("SELECT position FROM players WHERE id=:i"), {"i": pid})).scalar()
    if anterior is None or int(anterior) == pos:
        return None
    await s.execute(
        text("UPDATE players SET position=:p, updated_at=now() WHERE id=:i"),
        {"p": pos, "i": pid})
    return (_POS.get(int(anterior), str(anterior)), _POS.get(pos, str(pos)))


_POS = {1: "POR", 2: "DEF", 3: "MED", 4: "DEL"}


async def _guardar_plantillas(api: FantasyAPI, token: str, liga: str,
                              managers: list[tuple[int, str]]) -> int:
    """Una petición por manager. Devuelve cuántas fichas se han guardado.

    Todas las filas llevan el mismo `ts`, y es obligatorio: las consultas de plantilla
    cogen el último snapshot con un `max(ts)` global. Un manager escrito con otro `ts`
    hace desaparecer a los demás de todas ellas sin dar error. Lo vigila el invariante
    "managers fuera del último snapshot" de `salud.py`.

    Por eso las cuatro van con `ttl=0`. Con el defecto de 900 s un clausulazo envenena la
    caché: `api/accion.py` lee la plantilla del rival para mirar la protección ANTES de
    pagar, y una ingesta dentro de esos 15 min mezcla esa copia con la propia ya fresca.
    El jugador aparece en dos plantillas bajo el mismo `ts`, el invariante de `salud.py`
    se pone rojo y `politica.datos_fiables()` para el piloto entero. La ventana es de
    minutos.

    No cuesta ni una petición de más en el ciclo normal: el job corre cada 30 min y la
    caché dura 15, así que nunca hay copia viva que reutilizar.
    """
    ts = datetime.now().astimezone()
    filas = 0
    cambios: list[tuple] = []
    async with Session() as s:
        for mid, tid in managers:
            plantilla = await api.plantilla(token, liga, tid, ttl=0)
            if isinstance(plantilla, dict) and plantilla.get("teamMoney") is not None:
                await _guardar_saldo(s, api, token, mid, tid, plantilla)
            jugadores = (plantilla.get("players") if isinstance(plantilla, dict)
                         else plantilla)
            for j in jugadores or []:
                pid = _num((j.get("playerMaster") or {}).get("id"))
                if pid is None:
                    continue
                await s.execute(INSERT_SQUAD, {
                    "ts": ts,
                    "manager_id": _num(j.get("managerId")) or mid,
                    "player_id": pid,
                    # El id que usa la alineación no es el del jugador: es el de su
                    # ficha dentro de tu equipo.
                    "player_team_id": str(j.get("playerTeamId") or "") or None,
                    "clause": _num(j.get("buyoutClause")),
                    "lock_until": _fecha(j.get("buyoutClauseLockedEndTime")),
                    "shielded": bool(j.get("isShielded")),
                })
                filas += 1
                cambio = await _guardar_estado(s, pid, j)
                if cambio:
                    cambios.append((mid, (j.get("playerMaster") or {}).get("nickname")
                                    or str(pid), *cambio))
        await s.commit()
    await _avisar_cambios_de_posicion(cambios)
    return filas


async def _avisar_cambios_de_posicion(cambios: list[tuple]) -> None:
    """Un cambio de demarcación en la plantilla PROPIA se avisa como emergencia.

    El once guardado tiene al jugador en su línea vieja, así que en cuanto algo refresque
    la alineación (un fichaje basta) LaLiga lo descarta y quedan diez, y eso anula la
    jornada. Solo se arregla recolocándolo a mano. Los de otros managers no se avisan: son
    información, no una tarea.
    """
    if not cambios:
        return
    async with Session() as s:
        mio = (await s.execute(
            text("SELECT id FROM managers WHERE is_me LIMIT 1"))).scalar()
    if mio is None:
        # Sin fila `is_me` no se puede saber de quién es cada cambio. Callar sería lo
        # peor: el único caso para el que existe esta función se iría al log de ajenos.
        log.error("cambio(s) de demarcación y no hay manager `is_me`: %s",
                  ", ".join(f"{n} {v}->{x}" for _, n, v, x in cambios))
        await enviar("🔀 <b>Cambio de demarcación</b> y no se sabe de quién: "  # sin-rastro-ok: aviso que lee una persona
                     + ", ".join(f"{n} {v}→{x}" for _, n, v, x in cambios)
                     + "\nRevisa el once a mano.",
                     nivel=EMERGENCIA, categoria="once_roto", clave="posicion:sin-is-me")
        return
    propios = [c for c in cambios if c[0] == mio]
    for _, nombre, viejo, nuevo in propios:
        await enviar(f"🔀 <b>{nombre} cambia de demarcación</b>: {viejo} → {nuevo}\n"  # sin-rastro-ok: aviso que lee una persona
                     f"Hay que recolocarlo en el once a mano: LaLiga lo descarta de su "
                     f"línea vieja y un once de diez anula la jornada.",
                     nivel=EMERGENCIA, categoria="once_roto",
                     clave=f"posicion:{nombre}:{nuevo}")
    if propios:
        log.error("cambio de demarcación PROPIO: %s",
                  ", ".join(f"{n} {v}->{x}" for _, n, v, x in propios))
    ajenos = [c for c in cambios if c[0] != mio]
    if ajenos:
        log.info("cambios de demarcación ajenos: %s",
                 ", ".join(f"{n} {v}->{x}" for _, n, v, x in ajenos))


async def _leer_tablon(api: FantasyAPI, token: str, liga: str,
                       paginas: int) -> list[dict[str, Any]]:
    """El tablón entero, que está paginado y con páginas de tamaños distintos.

    Se recorren hasta que una venga vacía. Quedarse en el índice 0 deja fuera casi todo
    el histórico: el saldo rival se reconstruye sumando desde el principio, así que perder
    páginas antiguas falsea la contabilidad.
    """
    actividad: list[dict[str, Any]] = []
    for pagina in range(paginas):
        pg = await api.actividad(token, liga, pagina)
        if isinstance(pg, dict):
            pg = pg.get("data") or []
        if not pg:
            break
        actividad.extend(pg)
    return actividad


async def _guardar_eventos(actividad: list[dict[str, Any]]) -> int:
    """Cada evento del tablón, con su tipo traducido y el original en `raw`.

    Los 33 tipos vienen de `GET {API}/v5/activity-types`, generados en
    `tipos_actividad.py`. Traducirlos todos importa porque entre ellos hay avisos que de
    otro modo pasan desapercibidos como `tipo_N`: el 7 declara que la jornada no ha
    puntuado por once incompleto, y el 8 que no ha puntuado por saldo negativo.
    """
    eventos = 0
    async with Session() as s:
        for e in actividad or []:
            cuando = _fecha(e.get("createdAt"))
            if not cuando:
                continue
            await s.execute(INSERT_EVENT, {
                "ts": cuando,
                "kind": _nombre_tipo(int(e.get("activityTypeId") or 0)),
                "manager_id": _num(e.get("user1Id")),
                "player_id": _num(e.get("playerMasterId")),
                "amount": _num(e.get("amount")),
                "raw": json.dumps(e, ensure_ascii=False),
            })
            eventos += 1
        await s.commit()
    return eventos


async def ingest_league(api: FantasyAPI, token: str, liga: str | None = None,
                        paginas: int = 8) -> dict[str, int]:
    """Clasificación, plantillas y tablón de la liga, en tres fases."""
    liga = liga or settings.fantasy_league_id
    if not liga:
        raise ValueError("falta FANTASY_LEAGUE_ID en el .env")
    yo = str(settings.fantasy_team_id or "")

    # `/leagues/{lg}/teams` sustituye a `/standing` y trae más: `fixturePoints` (puntos de
    # la jornada EN CURSO), `position`/`previousPosition` y las cuatro plantillas de golpe.
    #
    # Pero NO trae `isShielded`, así que las plantillas individuales se siguen pidiendo:
    # sin el blindaje, la vigilancia daría a todo el mundo por desprotegido y el piloto
    # podría intentar un clausulazo contra alguien blindado (400 `030.01.62`).
    equipos = await api.http.get(f"/v1/competition/1/leagues/{liga}/teams",
                                 ttl=300, auth=token)

    # `fixturePoints` a 0 con jugadores que ya han puntuado significa que la jornada NO
    # está contando: pasa con el once incompleto. Se avisa desde `vigilar()`.
    fixture = {str(t.get("id")): t.get("fixturePoints") for t in (equipos or [])}

    managers = await _guardar_managers(equipos, yo)
    filas = await _guardar_plantillas(api, token, liga, managers)
    eventos = await _guardar_eventos(await _leer_tablon(api, token, liga, paginas))

    res = {"managers": len(managers), "jugadores_en_plantillas": filas,
           "eventos": eventos, "fixture_points": fixture.get(yo)}
    log.info("liga %s: %s", liga, res)
    return res
