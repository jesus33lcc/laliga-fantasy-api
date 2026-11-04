"""Vigilancia de cláusulas: mi exposición y los objetivos reales.

Responde en SQL, sobre el último snapshot, qué jugador mío sale barato de robar y qué
jugador rival se puede pagar a buen precio.

`clause_lock_until` es la protección de 14 días tras la compra, no un bloqueo de subida de
cláusula: la fecha es `fecha_de_compra + 14,00 días` exactos. Mientras esté en el futuro
ese jugador no se puede clausular, ni yo a él ni él a mí, así que sin filtrarlo la lista
de objetivos incluye intocables.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from ..db import Session
from ..tiempo import dentro_de

log = logging.getLogger("coconut.vigilancia")

ULTIMO = "(SELECT max(ts) FROM squad_snapshots)"

_MI_EXPOSICION = text(f"""
    SELECT p.name AS jugador,
           -- Hace falta para medir la caída del once con `blindaje.anotar_perdida`, que
           -- cruza por id. Sin él la exposición solo se puede ordenar por ratio, y el
           -- ratio no sabe si el jugador juega.
           s.player_id,
           s.buyout_clause AS clausula,
           mv.value AS valor,
           CAST(s.buyout_clause AS FLOAT) / NULLIF(mv.value, 0) AS ratio,
           -- `bloqueada` es true mientras corren sus 14 días, o sea cuando NO se puede
           -- clausular. El nombre invita a leerlo al revés.
           (s.clause_lock_until > now()) AS bloqueada,
           s.clause_lock_until AS protegido_hasta
    FROM squad_snapshots s
    JOIN players p   ON p.id = s.player_id
    JOIN managers m  ON m.id = s.manager_id
    LEFT JOIN v_latest_values mv ON mv.player_id = s.player_id
    WHERE s.ts = {ULTIMO} AND m.is_me
      AND s.buyout_clause <= :presupuesto_rival
    -- Desempate por nombre: el ratio 1,00 se repite mucho, y sin él el orden de la
    -- lista lo decide el motor.
    ORDER BY CAST(s.buyout_clause AS FLOAT) / NULLIF(mv.value, 0), p.name
    LIMIT :limite
""")

_OBJETIVOS = text(f"""
    SELECT m.name AS rival, p.name AS jugador, p.id AS player_id,
           -- El id que pide la ruta de pago de cláusula es este, no `p.id`. Sin traerlo,
           -- el piloto no tiene con qué pagarla. Ojo: esta consulta es un f-string, así
           -- que unas llaves aquí dentro se interpolan igual que en el SQL.
           s.player_team_id,
           s.buyout_clause AS clausula,
           mv.value AS valor,
           CAST(s.buyout_clause AS FLOAT) / NULLIF(mv.value, 0) AS ratio,
           s.clause_lock_until AS protegido_hasta,
           -- Señales de calidad: sin esto la decisión es solo aritmética de precios.
           p.status, p.position,
           CAST((p.raw->>'lastSeasonPoints') AS INTEGER) AS puntos_anteriores,
           (SELECT pl.probability FROM probable_lineups pl
             WHERE pl.player_id = p.id ORDER BY pl.ts DESC LIMIT 1) AS prob_titular
    FROM squad_snapshots s
    JOIN players p   ON p.id = s.player_id
    JOIN managers m  ON m.id = s.manager_id
    LEFT JOIN v_latest_values mv ON mv.player_id = s.player_id
    WHERE s.ts = {ULTIMO} AND NOT m.is_me
      AND s.buyout_clause <= :saldo
      AND mv.value IS NOT NULL
      AND (s.clause_lock_until IS NULL OR s.clause_lock_until <= now())
      AND COALESCE(s.shielded, false) = false
      -- Solo jugadores de campo: la posición 5 son entrenadores, que no entran en el
      -- once (el PUT de alineación ni siquiera acepta `coach`), así que clausular uno es
      -- pagar por una ficha que no puede puntuar. El mercado los saca a menudo.
      AND p.position BETWEEN 1 AND 4
    ORDER BY CAST(s.buyout_clause AS FLOAT) / NULLIF(mv.value, 0), s.buyout_clause DESC
    LIMIT :limite
""")

# Los que hoy están protegidos pero se desbloquean pronto. Su fecha de desbloqueo es
# pública: el resto de la liga la ve igual.
_PROXIMOS = text(f"""
    SELECT m.name AS rival, p.name AS jugador,
           s.buyout_clause AS clausula, mv.value AS valor,
           CAST(s.buyout_clause AS FLOAT) / NULLIF(mv.value, 0) AS ratio,
           s.clause_lock_until AS se_libera
    FROM squad_snapshots s
    JOIN players p   ON p.id = s.player_id
    JOIN managers m  ON m.id = s.manager_id
    LEFT JOIN v_latest_values mv ON mv.player_id = s.player_id
    WHERE s.ts = {ULTIMO} AND NOT m.is_me
      AND s.clause_lock_until > now()
      AND s.clause_lock_until < :hasta
      AND mv.value IS NOT NULL
    -- Los comprados a la vez comparten la fecha exacta: sin desempate, el orden cambia
    -- entre Postgres y SQLite con los mismos datos.
    ORDER BY s.clause_lock_until, p.name
    LIMIT :limite
""")

# Saldo de verdad disponible: lo comprometido en ofertas pendientes ya no es tuyo aunque
# `teamMoney` lo siga contando. Sin este resto, el piloto propone dos compras con el mismo
# dinero y la segunda revienta en la API.
_MI_SALDO = text("""
    SELECT GREATEST(COALESCE(balance_known, 0) - COALESCE(balance_committed, 0), 0)
    FROM managers WHERE is_me LIMIT 1
""")


async def mi_saldo() -> int:
    """Caja propia según el último snapshot, nunca negativa.

    El `GREATEST(..., 0)` de la consulta solo vale para decidir gasto, que es para lo que
    se pregunta aquí: con la caja en rojo la respuesta útil para `puede_gastar` es "no hay
    para nada". El valor CON SIGNO, que es el que delata la jornada anulada por saldo
    negativo, sale de la API en `informe/comun._saldo_real()`, y es el que mira el panel.

    Sin fila propia en `managers` (instalación nueva, antes del primer `ingest_league`)
    esto daría 0 igual que estando sin dinero, y con ese 0 `objetivos()` sale vacía y
    parece que no hay a quién clausular. Por eso se distingue en el log.
    """
    async with Session() as s:
        saldo = (await s.execute(_MI_SALDO)).scalar()
    if saldo is None:
        log.warning("no hay manager propio en `managers`: el saldo sale 0, "
                    "que aquí no significa estar a cero")
        return 0
    return int(saldo)


async def blindados_ahora() -> set[str] | None:
    """Los nombres de mis jugadores blindados AHORA MISMO, leídos de la API.

    El blindaje dura 24 h y vive solo en la API: uno puesto hace un rato todavía no está
    en `squad_snapshots`, que va hasta 30 min por detrás. Sin cruzarlo, un aviso de
    exposición señala un peligro ya resuelto, y un aviso falso en el canal de urgencias
    enseña a ignorarlo.

    Devuelve `None` si la API no contesta, que no es lo mismo que "no hay ninguno": quien
    llama se queda entonces con la foto de la base, que avisa de más pero no se calla con
    alguien expuesto de verdad.

    Vive aquí y no en cada avisador para que no puedan discrepar: dos sitios avisan de
    exposición (el panel y el vigilante de Telegram) y los dos cruzan por aquí.
    """
    from ..clients.auth import tokens
    from ..clients.fantasy import FantasyAPI
    from ..clients.via_api import cliente_lectura
    from ..config import settings
    try:
        async with cliente_lectura() as http:
            propia = await FantasyAPI(http).plantilla(
                await tokens.bearer(), settings.fantasy_league_id,
                str(settings.fantasy_team_id))
        return {(p.get("playerMaster") or {}).get("nickname")
                for p in (propia.get("players") or []) if p.get("isShielded")}
    except Exception:
        return None


async def mi_exposicion(presupuesto_rival: int, limite: int = 10) -> list[dict]:
    """Jugadores míos que un rival con ese presupuesto podría pagar.

    No filtra los blindados: el blindaje dura 24 h y vive en la API, no en el snapshot.
    Quien avise a partir de esto cruza con `blindados_ahora()`.
    """
    async with Session() as s:
        filas = await s.execute(_MI_EXPOSICION,
                                {"presupuesto_rival": presupuesto_rival, "limite": limite})
        return [dict(f._mapping) for f in filas]


async def objetivos(saldo: int | None = None, limite: int = 10) -> list[dict]:
    """Rivales clausulables ahora: excluye protegidos, blindados y entrenadores."""
    saldo = saldo if saldo is not None else await mi_saldo()
    async with Session() as s:
        filas = await s.execute(_OBJETIVOS, {"saldo": saldo, "limite": limite})
        return [dict(f._mapping) for f in filas]


async def proximas_liberaciones(dias: int = 4, limite: int = 10) -> list[dict]:
    """Rivales que salen de su protección en los próximos días."""
    async with Session() as s:
        filas = await s.execute(_PROXIMOS,
                                {"hasta": dentro_de(dias=dias), "limite": limite})
        return [dict(f._mapping) for f in filas]
