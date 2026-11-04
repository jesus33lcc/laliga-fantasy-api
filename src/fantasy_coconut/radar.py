"""Radar: detectores de oportunidad y de peligro.

Cada detector devuelve avisos que el piloto convierte en propuestas. La regla es la misma
para todos: solo se avisa de lo accionable. Un aviso que no lleva a una decisión es
ruido, y el ruido hace que dejes de leer los avisos buenos.

Lo que vigila, y por qué cada uno merece existir:

  Protección que caduca: cuando uno de tus jugadores sale de sus 14 días de
      protección, pasa a ser robable. Es el único momento predecible de vulnerabilidad
      y casi nadie lo mira: se puede blindar antes de que llegue, no después.
  Sniper de mercado: ofertas a punto de expirar, sin pujas y por debajo de su
      valor. La ventana es de horas y quien mira la app cuando se acuerda las pierde.
  Lastre: jugadores tuyos que no van a jugar (lesionados largos, 0% de
      titularidad). Ocupan plaza y dinero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text

from .db import Session
from .tiempo import ahora, dentro_de, horas_entre

log = logging.getLogger("coconut.radar")


@dataclass
class Aviso:
    tipo: str
    titulo: str
    detalle: str
    urgencia: int              # 1 informativo · 2 conviene · 3 urgente
    accion: str | None = None  # qué proponer, si hay algo que ejecutar
    importe: int = 0
    player_id: int | None = None
    market_id: str | None = None
    clave: str = ""

    def __str__(self) -> str:
        icono = {1: "ℹ️", 2: "⚠️", 3: "🚨"}.get(self.urgencia, "•")
        return f"{icono} {self.titulo}: {self.detalle}"


# --- 1) Mis jugadores que dejan de estar protegidos ---------------------------------
_PROTECCION_CADUCA = text("""
    SELECT p.id, p.name, s.buyout_clause AS clausula, v.value AS valor,
           -- Las horas que faltan se calculan en Python (`horas_entre`):
           -- `EXTRACT(epoch FROM …)` es de Postgres y no tiene equivalente en SQLite.
           s.clause_lock_until AS se_expone
    FROM squad_snapshots s
    JOIN players p  ON p.id = s.player_id
    JOIN managers m ON m.id = s.manager_id
    LEFT JOIN v_latest_values v ON v.player_id = s.player_id
    WHERE s.ts = (SELECT max(ts) FROM squad_snapshots)
      AND m.is_me
      AND s.clause_lock_until > now()
      AND s.clause_lock_until < :hasta
      AND v.value >= :valor_minimo
      AND s.buyout_clause <= :alcance
    ORDER BY s.clause_lock_until
""")


async def proteccion_caducando(presupuesto_rival: int, dias: int = 5,
                               valor_minimo: int = 10_000_000) -> list[Aviso]:
    """Jugadores míos valiosos a punto de quedar expuestos y al alcance de un rival."""
    async with Session() as s:
        # El margen del 20 % se calcula aquí: asyncpg no multiplica un bigint por un
        # float sin un cast explícito, y en Python queda más legible.
        filas = await s.execute(_PROTECCION_CADUCA, {
            "hasta": dentro_de(dias=dias), "valor_minimo": valor_minimo,
            "alcance": int(presupuesto_rival * 1.2),
        })
        avisos = []
        for r in filas:
            pid, nombre, clausula, valor, cuando = r
            horas = horas_entre(cuando, ahora()) or 0
            ratio = (clausula / valor) if valor else 0
            avisos.append(Aviso(
                tipo="proteccion_caduca",
                titulo=f"{nombre} queda expuesto en {horas:.0f} h",
                detalle=(f"cláusula {clausula/1e6:.1f}M sobre un valor de "
                         f"{valor/1e6:.1f}M (×{ratio:.2f}) · el rival más rico tiene "
                         f"~{presupuesto_rival/1e6:.0f}M · se destapa el "
                         f"{cuando:%d-%b %H:%M}"),
                urgencia=3 if ratio < 1.3 else 2,
                accion="subir_clausula",
                player_id=pid,
                clave=f"expone:{pid}:{cuando:%Y%m%d%H}",
            ))
        return avisos


# --- 2) Chollos del mercado a punto de expirar --------------------------------------
_SNIPER = text("""
    SELECT o.market_id, o.player_id, p.name, o.sale_price AS precio, v.value AS valor,
           o.num_bids AS pujas, o.expires_at,
           CAST((p.raw->>'lastSeasonPoints') AS INTEGER) AS puntos,
           (SELECT pl.probability FROM probable_lineups pl
             WHERE pl.player_id = p.id ORDER BY pl.ts DESC LIMIT 1) AS prob
    FROM market_offers o
    JOIN players p ON p.id = o.player_id
    LEFT JOIN v_latest_values v ON v.player_id = o.player_id
    WHERE o.expires_at > now()
      AND o.expires_at < :hasta
      AND o.sale_price <= :presupuesto
      AND p.status = 'ok'
      AND v.value IS NOT NULL
      AND o.sale_price <= v.value * :ratio_max
    ORDER BY CAST(o.sale_price AS FLOAT) / NULLIF(v.value, 0)
""")


async def chollos_expirando(presupuesto: int, horas: int = 20,
                            ratio_max: float = 1.0) -> list[Aviso]:
    """Ofertas que se acaban pronto, aún baratas y sin apenas competencia."""
    async with Session() as s:
        filas = await s.execute(_SNIPER, {"hasta": dentro_de(horas=horas),
                                          "presupuesto": presupuesto,
                                          "ratio_max": ratio_max})
        avisos = []
        for r in filas:
            mid, pid, nombre, precio, valor, pujas, expira, puntos, prob = r
            horas_rest = horas_entre(expira, ahora()) or 0
            # Sin titularidad y sin histórico no hay forma de saber si vale la pena.
            if prob is None and not puntos:
                continue
            ratio = precio / valor if valor else 1
            avisos.append(Aviso(
                tipo="chollo_mercado",
                titulo=f"{nombre} a {precio/1e6:.1f}M (vale {valor/1e6:.1f}M)",
                detalle=(f"×{ratio:.2f} · {pujas} pujas · expira en {horas_rest:.0f} h "
                         f"({expira:%d-%b %H:%M}) · "
                         f"{puntos or '?'} pts · "
                         f"{int(prob*100) if prob else '?'}% titular"),
                urgencia=3 if horas_rest < 6 else 2,
                accion="pujar",
                importe=int(precio * 1.03),   # justo por encima, sin regalar dinero
                player_id=pid,
                market_id=str(mid),
                clave=f"chollo:{mid}",
            ))
        return avisos


# --- 3) Lastre en mi plantilla -------------------------------------------------------
_LASTRE = text("""
    SELECT p.id, p.name, v.value AS valor, p.status,
           (SELECT pl.probability FROM probable_lineups pl
             WHERE pl.player_id = p.id ORDER BY pl.ts DESC LIMIT 1) AS prob
    FROM squad_snapshots s
    JOIN players p  ON p.id = s.player_id
    JOIN managers m ON m.id = s.manager_id
    LEFT JOIN v_latest_values v ON v.player_id = s.player_id
    WHERE s.ts = (SELECT max(ts) FROM squad_snapshots) AND m.is_me
      AND (p.status IN ('injured', 'suspended', 'out_of_league')
           OR (SELECT pl.probability FROM probable_lineups pl
                WHERE pl.player_id = p.id ORDER BY pl.ts DESC LIMIT 1) < 0.2)
""")


async def lastre() -> list[Aviso]:
    """Jugadores que no van a jugar: ocupan plaza y no dan puntos."""
    async with Session() as s:
        filas = await s.execute(_LASTRE)
        return [
            Aviso(tipo="lastre",
                  titulo=f"{r[1]} no va a jugar",
                  detalle=(f"estado '{r[3]}'"
                           + (f" · {int(r[4]*100)}% titular" if r[4] is not None else "")
                           + (f" · vale {r[2]/1e6:.1f}M" if r[2] else "")),
                  urgencia=1,
                  accion="vender",
                  player_id=r[0],
                  clave=f"lastre:{r[0]}")
            for r in filas
        ]


async def barrido(presupuesto: int, presupuesto_rival: int) -> list[Aviso]:
    """Todos los detectores, ordenados por urgencia."""
    avisos: list[Aviso] = []
    for detector in (
        proteccion_caducando(presupuesto_rival),
        chollos_expirando(presupuesto),
        lastre(),
    ):
        try:
            avisos.extend(await detector)
        except Exception:
            log.exception("un detector del radar falló")
    avisos.sort(key=lambda a: -a.urgencia)
    return avisos
