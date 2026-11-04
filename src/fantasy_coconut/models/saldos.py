"""Estimación del saldo de los rivales por contabilidad del tablón.

La API solo expone `teamMoney` del equipo propio. Pero el tablón publica cada movimiento
con su importe, así que el saldo ajeno se reconstruye:

    saldo = inicial - compras - clausulazos_pagados
                    + ventas + clausulazos_cobrados + recompensas + recompensa_diaria

El `inicial` se despeja con el saldo propio, que es el único exacto que da la API. El
reglamento da 100M más una compensación variable a quien recibe una plantilla floja, así
que cada manager empezó con una cifra distinta y aplicar un solo inicial a los cuatro
tiene ese límite de origen.

Es una cota superior, y condicionada a dos cosas: que el tablón esté completo para el
periodo contabilizado, y que el despeje sea el bueno (si cae fuera de rango se usa el del
reglamento, que es una suposición). Cumplidas, el saldo real de un rival es igual o menor
que el estimado, porque subir una cláusula no sale en el tablón y ese gasto no se resta
nunca. Por eso se devuelven la confianza y el desglose: es un orden de magnitud.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass

from sqlalchemy import text

from ..db import Session

log = logging.getLogger("coconut.saldos")

COMPRA, VENTA = "market_buy", "market_sale"

# Contar solo compras y ventas al sistema deja saldos negativos: faltan los otros dos
# flujos de dinero del tablón.
#
#   · `clausulazo` (tipo 1): el que paga es `manager_id`; el que COBRA es `user2Id`,
#     que va dentro de `raw`. Son dos apuntes de un mismo evento.
#   · `recompensa` (tipo 6): ingreso por jornada, y a lo largo de una temporada suma
#     lo bastante como para descuadrar a todo el mundo si no se cuenta.
#
# Se aceptan los dos nombres de cada tipo: los eventos ingeridos antes de identificarlo
# quedan guardados como `tipo_N`.
_MOVIMIENTOS = text("""
    WITH ev AS (
        SELECT manager_id, kind, amount, raw FROM league_events WHERE manager_id IS NOT NULL
    ),
    -- Lo que cobra el DUEÑO ANTERIOR en un clausulazo: sale del evento del comprador.
    cobros_clausula AS (
        SELECT CAST((raw->>'user2Id') AS INTEGER) AS manager_id, SUM(amount) AS cobrado
        FROM ev WHERE kind IN ('clausulazo', 'tipo_1') AND raw->>'user2Id' IS NOT NULL
        GROUP BY 1
    ),
    propios AS (
        SELECT manager_id,
               COALESCE(SUM(amount) FILTER (WHERE kind = :compra), 0) AS compras,
               COALESCE(SUM(amount) FILTER (WHERE kind = :venta),   0) AS ventas,
               COALESCE(SUM(amount) FILTER (WHERE kind IN ('clausulazo','tipo_1')), 0) AS clausulas_pagadas,
               -- Todos los ingresos del tablón, no solo la recompensa de jornada:
               -- también «11 ideal», «ajuste de venta», «actualización de puntos»,
               -- «rectificación en el cálculo» y «otro ajuste» (tipos 12 y 20-24).
               COALESCE(SUM(amount) FILTER (WHERE kind IN (
                   'recompensa','tipo_6','recompensa_ideal','tipo_12',
                   'otra_recompensa_20','tipo_20','otra_recompensa_21','tipo_21',
                   'otra_recompensa_22','tipo_22','otra_recompensa_23','tipo_23',
                   'otra_recompensa_24','tipo_24')), 0) AS recompensas,
               COUNT(*) FILTER (WHERE kind IN (:compra, :venta, 'clausulazo', 'tipo_1')) AS n
        FROM ev GROUP BY manager_id
    )
    SELECT p.manager_id,
           p.compras + p.clausulas_pagadas                                   AS compras,
           p.ventas + p.recompensas + COALESCE(c.cobrado, 0)                 AS ventas,
           p.n
    FROM propios p
    LEFT JOIN cobros_clausula c ON c.manager_id = p.manager_id
""")

_MANAGERS = text("""
    SELECT id, name, is_me, balance_known, team_value FROM managers
""")

_GUARDAR = text("""
    UPDATE managers SET balance_est = :saldo, updated_at = now() WHERE id = :id
""")


@dataclass
class Saldo:
    manager_id: int
    nombre: str
    es_mio: bool
    compras: int
    ventas: int
    movimientos: int
    estimado: int | None       # el saldo tal cual sale de la contabilidad
    real: int | None
    crudo: int | None = None   # se conserva por compatibilidad: es igual a `estimado`
    valor_plantilla: int | None = None

    @property
    def en_deuda(self) -> bool:
        return self.estimado is not None and self.estimado < 0

    @property
    def margen_de_deuda(self) -> int:
        """Cuánto puede deber: el 20 % del valor de su equipo (regla `030.01.32`)."""
        return int((self.valor_plantilla or 0) * 0.20)

    @property
    def incompleto(self) -> bool:
        """Poco fiable solo si la deuda pasa del límite legal.

        Un saldo negativo no es imposible: `030.01.32` permite pujar en deuda hasta el
        20 % del valor del equipo. Por debajo de eso el número es bueno.
        """
        return self.estimado is not None and self.estimado < -self.margen_de_deuda

    @property
    def confianza(self) -> str:
        if self.real is not None:
            return "exacto"
        return "baja" if self.incompleto else "media"


# El reglamento oficial: «cada manager recibe un equipo completo de 14 jugadores y 100
# millones de saldo» (docs/REGLAS.md). Hay una compensación variable para quien
# recibe una plantilla por debajo de la media, así que 100M es el suelo, no una certeza.
INICIAL_REGLAMENTO = 100_000_000
# Primer evento del tablón: la creación de la liga.
ARRANQUE_LIGA = _dt.date(2026, 7, 20)
RECOMPENSA_DIARIA = 100_000


def _recompensa_diaria_acumulada() -> int:
    """Cota superior de lo cobrado por la recompensa diaria desde que arrancó la liga."""
    dias = max(0, (_dt.date.today() - ARRANQUE_LIGA).days)
    return dias * RECOMPENSA_DIARIA


_GASTO_OCULTO = text("""
    SELECT COALESCE(sum(importe), 0) FROM decisions
    WHERE ejecutada AND accion IN ('subir_clausula', 'subir-clausula')
""")

_RECOMPENSAS_COBRADAS = text("""
    SELECT count(*) FROM decisions
    WHERE ejecutada AND accion IN ('recompensa_diaria', 'recompensa')
""")


async def _recompensa_diaria_propia() -> int:
    """Lo que se ha cobrado de recompensa diaria, no lo que se podría haber cobrado.

    Para el saldo propio hay dato real y usar la cota superior (un cobro por día desde que
    arrancó la liga) resta dinero que nunca entró: el proceso que la cobra no existió desde
    el principio. Para los rivales no hay forma de saberlo y ahí sí se usa la cota.
    """
    try:
        async with Session() as s:
            return int((await s.execute(_RECOMPENSAS_COBRADAS)).scalar() or 0) * RECOMPENSA_DIARIA
    except Exception:
        return _recompensa_diaria_acumulada()


async def _gastos_fuera_del_tablon() -> int:
    """Lo gastado en subir cláusulas, que el tablón no publica.

    Es el único gasto propio grande que no deja evento, y sin devolverlo al despeje el
    presupuesto inicial sale corto por esa misma cantidad. Solo se conocen los propios:
    los de los rivales no los ve nadie, y de ahí que su caja estimada sea una cota
    superior y nunca una cifra exacta.
    """
    try:
        async with Session() as s:
            return int((await s.execute(_GASTO_OCULTO)).scalar() or 0)
    except Exception as e:                       # tabla aún sin crear en una instalación nueva
        log.warning("no se pudieron leer las subidas de cláusula (%s): el inicial saldrá "
                    "corto por esa cantidad", type(e).__name__)
        return 0


async def estimar() -> tuple[list[Saldo], int | None]:
    """Devuelve `(list[Saldo], presupuesto_inicial_deducido)`.

    El primer elemento es una lista de dataclasses `Saldo`, no de dicts. Tratarla con
    `.get()` lanza `AttributeError`, y quien lo envuelva en un `except` se queda con
    valores por defecto que parecen razonables y no lo son.
    """
    async with Session() as s:
        movs = {r[0]: (int(r[1]), int(r[2]), int(r[3]))
                for r in await s.execute(_MOVIMIENTOS,
                                         {"compra": COMPRA, "venta": VENTA})}
        managers = [dict(r._mapping) for r in await s.execute(_MANAGERS)]

    # 1) Presupuesto inicial, despejado con el saldo propio, que es el único exacto:
    #
    #     inicial = saldo_real + compras - ventas + subidas de cláusula
    #               - recompensa diaria acumulada
    #
    # Hay que devolverle los dos conceptos que mueven dinero y no salen en el tablón, o el
    # inicial absorbe los gastos propios y luego se los resta a los rivales. Las subidas de
    # cláusula salen de `decisions`, que es donde quedan las propias; las ajenas no las ve
    # nadie, y de ahí que el estimador rival sea una cota superior.
    inicial, detalle = INICIAL_REGLAMENTO, "del reglamento (sin saldo propio conocido)"
    for m in managers:
        if not (m["is_me"] and m["balance_known"] is not None):
            continue
        c, v, _ = movs.get(m["id"], (0, 0, 0))
        oculto = await _gastos_fuera_del_tablon()
        despejado = int(m["balance_known"]) + c - v + oculto - await _recompensa_diaria_propia()
        # Una guarda, no un adorno: si el despeje sale absurdo (base recién creada, tablón
        # a medio ingerir) es preferible el número del reglamento a una cifra inventada
        # que se propaga a los cuatro managers.
        if 20_000_000 <= despejado <= 500_000_000:
            inicial = despejado
            detalle = (f"despejado con mi saldo real (+{oculto / 1e6:.1f}M de subidas de "
                       f"cláusula propias, que el tablón no publica)")
        else:
            detalle = (f"del reglamento: el despeje dio {despejado / 1e6:.1f}M, "
                       f"fuera del rango creíble")
        log.info("inicial: %.1fM %s", inicial / 1e6, detalle)
        break

    # 2) Aplicarlo a todos. Aquí está el límite del método: el reglamento compensa con
    # saldo extra a quien recibe una plantilla por debajo de la media, así que los cuatro
    # no empezaron con lo mismo y un único inicial no puede cuadrarles a todos.
    salida: list[Saldo] = []
    async with Session() as s:
        for m in managers:
            c, v, n = movs.get(m["id"], (0, 0, 0))
            # La recompensa diaria tampoco sale en el tablón. Se estima por los días
            # transcurridos, asumiendo que la cobran: es una cota SUPERIOR, porque quien
            # no la cobre tendrá menos.
            est = (inicial - c + v + _recompensa_diaria_acumulada()
                   if inicial is not None else None)
            # No se sanea a 0: un negativo es deuda, que el reglamento permite hasta el
            # 20 % del valor del equipo. Redondearla a cero borra lo más útil que da este
            # modelo, que es saber que un rival está sin caja y va a tener que vender.
            sal = Saldo(
                manager_id=m["id"], nombre=m["name"], es_mio=bool(m["is_me"]),
                compras=c, ventas=v, movimientos=n, estimado=est,
                real=m["balance_known"], crudo=est,
                valor_plantilla=m.get("team_value"),
            )
            if sal.en_deuda:
                log.info("%s está en deuda (%.2fM de un margen de %.1fM): tendrá que "
                         "vender", m["name"], (est or 0) / 1e6, sal.margen_de_deuda / 1e6)
            if sal.incompleto:
                log.warning("%s debe %.1fM, más del 20%% de su plantilla: aquí sí falta "
                            "algo por explicar", m["name"], -(est or 0) / 1e6)
            salida.append(sal)
            if est is not None:
                await s.execute(_GUARDAR, {"saldo": est, "id": m["id"]})
        await s.commit()

    salida.sort(key=lambda x: -(x.estimado or 0))
    return salida, inicial


async def presupuesto_rival_maximo() -> int:
    """Lo que puede pagar el rival más rico. Es la vara de medir mi exposición."""
    saldos, _ = await estimar()
    rivales = [s.estimado or 0 for s in saldos if not s.es_mio]
    return max(rivales) if rivales else 0
