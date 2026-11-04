"""Emergencia: detecta posiciones sin cobertura y propone refuerzos dentro de los límites.

`revisar_plantilla()` devuelve los huecos por posición, marcando cuáles son críticos;
`posiciones_debiles()` señala las que están cubiertas de ficha pero no de puntos;
`plan_refuerzo()` y `plan_emergencia()` proponen qué fichar, mirando primero el mercado y
luego las cláusulas. Ninguna ejecuta nada.

Dos situaciones distintas que conviene no confundir:

  - Falta un jugador de esa posición y no se puede formar un once legal. La alineación
    queda incompleta y eso anula la jornada entera (evento de tablón tipo 7), no solo
    esa plaza. Es el caso `critico` de `Hueco`.
  - El jugador está pero no va a puntuar (lesionado, sancionado o suplente fijo): la
    alineación es válida y solo esa plaza suma 0. Lo cubre `posiciones_debiles()` con
    `UMBRAL_TITULAR`, porque contar fichas no basta para dar el hueco por tapado.

Es un modo aparte del piloto normal porque la lógica se invierte: el piloto busca
valor y descarta lo caro; en emergencia el coste de no fichar es la jornada completa, y
supera al de pagar de más. Los límites de gasto siguen aplicando, relajados: los topes
ordinarios se sustituyen por un colchón mínimo (ver `politica.py`).

Mínimos para poder alinear, tomados del menor de cada línea entre las siete formaciones:
    1 portero · 3 defensas · 3 medios · 1 delantero
Por debajo de cualquiera de esos cuatro no hay once legal, y un once incompleto anula la
jornada entera.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text

from ..db import Session

log = logging.getLogger("coconut.emergencia")

PORTERO, DEFENSA, MEDIO, DELANTERO = 1, 2, 3, 4
NOMBRES = {PORTERO: "portero", DEFENSA: "defensa", MEDIO: "medio", DELANTERO: "delantero"}

# Suelo por posición para que exista un once válido.
# Los mínimos salen de `alineacion.FORMACIONES`: el menor de cada línea entre las siete.
# Con 2 medios no hay once legal (la formación más corta de medios es 3), y un once
# incompleto anula la jornada entera.
MINIMOS = {PORTERO: 1, DEFENSA: 3, MEDIO: 3, DELANTERO: 1}
# Colchón recomendado: con lo justo, una lesión o un clausulazo te deja fuera.
HOLGURA = {PORTERO: 2, DEFENSA: 5, MEDIO: 4, DELANTERO: 3}

# Por debajo de esta probabilidad de ser titular, un jugador ocupa plaza pero puntúa 0.
# Tener un portero suplente y tener cero porteros dan los mismos puntos: la diferencia es
# que con uno se puede guardar alineación. Por eso el hueco no se da por tapado con
# contar fichas: ver `posiciones_debiles()`.
UMBRAL_TITULAR = 0.5


@dataclass
class Hueco:
    posicion: int
    tengo: int
    minimo: int
    critico: bool          # no se puede alinear

    @property
    def faltan(self) -> int:
        return max(self.minimo - self.tengo, 0)

    def __str__(self) -> str:
        etiqueta = "CRÍTICO" if self.critico else "justo"
        return (f"{NOMBRES.get(self.posicion, '?')}: tengo {self.tengo}, "
                f"mínimo {self.minimo} ({etiqueta})")


_MI_PLANTILLA = text("""
    SELECT p.position, count(*) FILTER (WHERE p.status = 'ok') AS sanos, count(*) AS total
    FROM squad_snapshots s
    JOIN players p  ON p.id = s.player_id
    JOIN managers m ON m.id = s.manager_id
    WHERE s.ts = (SELECT max(ts) FROM squad_snapshots) AND m.is_me
    GROUP BY p.position
""")

# Jugadores de OTROS managers clausulables ahora mismo (sin protección ni blindaje), del
# más barato al más caro. El mercado no entra aquí: eso es `_MERCADO_POS`. Y la
# titularidad se pondera después, en `puntuar()`, no en el `ORDER BY`.
_CANDIDATOS = text("""
    SELECT p.id, p.name, m.name AS dueno,
           s.buyout_clause AS coste,
           v.value AS valor,
           CAST((p.raw->>'lastSeasonPoints') AS INTEGER) AS puntos,
           (SELECT pl.probability FROM probable_lineups pl
             WHERE pl.player_id = p.id ORDER BY pl.ts DESC LIMIT 1) AS prob_titular
    FROM squad_snapshots s
    JOIN players p  ON p.id = s.player_id
    JOIN managers m ON m.id = s.manager_id
    LEFT JOIN v_latest_values v ON v.player_id = p.id
    WHERE s.ts = (SELECT max(ts) FROM squad_snapshots)
      AND NOT m.is_me
      AND p.position = :pos
      AND p.status = 'ok'
      AND s.buyout_clause <= :presupuesto
      AND (s.clause_lock_until IS NULL OR s.clause_lock_until <= now())
      AND COALESCE(s.shielded, false) = false
    ORDER BY s.buyout_clause
""")


async def revisar_plantilla() -> list[Hueco]:
    """¿Puedo alinear? Devuelve solo las posiciones donde voy corto."""
    async with Session() as s:
        filas = {int(r[0]): int(r[1]) for r in await s.execute(_MI_PLANTILLA)
                 if r[0] is not None}

    huecos: list[Hueco] = []
    for pos, minimo in MINIMOS.items():
        tengo = filas.get(pos, 0)
        if tengo < HOLGURA[pos]:
            huecos.append(Hueco(pos, tengo, minimo, critico=tengo < minimo))
    return huecos


_MEJOR_POR_POSICION = text("""
    SELECT p.position,
           MAX((SELECT pl.probability FROM probable_lineups pl
                 WHERE pl.player_id = p.id ORDER BY pl.ts DESC LIMIT 1)) AS mejor_prob
    FROM squad_snapshots s
    JOIN players p  ON p.id = s.player_id
    JOIN managers m ON m.id = s.manager_id
    WHERE s.ts = (SELECT max(ts) FROM squad_snapshots) AND m.is_me AND p.status = 'ok'
    GROUP BY p.position
""")


async def posiciones_debiles() -> list[tuple[int, float]]:
    """Posiciones cubiertas de número pero no de puntos.

    Un hueco se cierra fichando a cualquiera; una debilidad, no. Si el mejor portero que
    tengo sale titular el 10% de las jornadas, la plantilla está "completa" y sumando
    cero por esa plaza todas las semanas. Sin esto el sistema se da por satisfecho en
    cuanto el hueco desaparece y deja de buscar justo cuando debería seguir.

    Devuelve `(posición, mejor probabilidad de titularidad)` de las que están por debajo
    del umbral, incluidas las que no tienen a nadie con dato (probabilidad 0).
    """
    async with Session() as s:
        filas = await s.execute(_MEJOR_POR_POSICION)
        actuales = {int(r[0]): float(r[1] or 0.0) for r in filas if r[0] is not None}

    return sorted(
        ((pos, actuales.get(pos, 0.0)) for pos in MINIMOS
         if actuales.get(pos, 0.0) < UMBRAL_TITULAR),
        key=lambda t: t[1],
    )


async def candidatos(posicion: int, presupuesto: int, limite: int = 8) -> list[dict]:
    """Quién puede tapar el hueco, del más barato al más caro."""
    async with Session() as s:
        filas = await s.execute(_CANDIDATOS, {"pos": posicion, "presupuesto": presupuesto})
        return [dict(f._mapping) for f in filas][:limite]


_TITULARES_LIBRES = text("""
    SELECT o.market_id, o.player_id, p.name, o.sale_price AS precio,
           GREATEST(o.sale_price, COALESCE(v.value, 0)) AS suelo,
           o.expires_at, o.num_bids AS competencia,
           CAST((p.raw->>'lastSeasonPoints') AS INTEGER) AS puntos,
           (SELECT pl.probability FROM probable_lineups pl
             WHERE pl.player_id = p.id ORDER BY pl.ts DESC LIMIT 1) AS prob_titular
    FROM market_offers o
    JOIN players p ON p.id = o.player_id
    LEFT JOIN v_latest_values v ON v.player_id = p.id
    WHERE p.position = :pos
      AND o.tipo = 'marketPlayerLeague'
      AND (o.expires_at IS NULL OR o.expires_at > now())
      AND p.status = 'ok'
      AND GREATEST(o.sale_price, COALESCE(v.value, 0)) <= :presupuesto
      AND (SELECT pl.probability FROM probable_lineups pl
            WHERE pl.player_id = p.id ORDER BY pl.ts DESC LIMIT 1) >= :umbral
    ORDER BY suelo
""")


async def plan_refuerzo(presupuesto: int) -> list[dict]:
    """Titulares de verdad en el mercado libre para las posiciones flojas.

    Solo mira agentes libres, y es deliberado: para una debilidad no hay prisa, y las dos
    alternativas son peores. El clausulazo le entrega la caja al rival al que se la pagas,
    y ofertar por el jugador de otro le anuncia qué te falta. El mercado libre no financia
    a nadie y no revela nada hasta que la subasta se resuelve.
    """
    plan: list[dict] = []
    for pos, prob_actual in await posiciones_debiles():
        async with Session() as s:
            filas = [dict(f._mapping) for f in await s.execute(
                _TITULARES_LIBRES,
                {"pos": pos, "presupuesto": presupuesto, "umbral": UMBRAL_TITULAR})]
        if not filas:
            continue
        for f in filas:
            f["nota"] = puntuar(f)
        mejor = max(filas, key=lambda f: f["nota"])
        plan.append({
            "posicion": pos,
            "prob_actual": prob_actual,
            "candidato": mejor,
            "importe": importe_puja(mejor["precio"], urgente=False,
                                    suelo=mejor.get("suelo") or 0),
            "alternativas": len(filas),
        })
    return plan


def puntuar(c: dict) -> float:
    """Para una emergencia manda que JUEGUE; el rendimiento es el segundo criterio."""
    prob = c.get("prob_titular")
    prob = float(prob) if prob is not None else 0.4   # sin dato, se asume mediocre
    puntos = float(c.get("puntos") or 0) / 200.0
    return prob * (0.4 + 0.6 * min(puntos, 1.0))


# --------------------------------------------------------------------------------
# El mercado: la vía normal para tapar un hueco
# --------------------------------------------------------------------------------

# Dos cosas que este SELECT tiene que respetar:
#
#  · El suelo de compra no es `sale_price`, es el mayor entre el precio de salida y el
#    valor de mercado: la API rechaza cualquier oferta por debajo del valor. Filtrar
#    por `sale_price <= presupuesto` promete fichajes que luego no se pueden pagar.
#  · Un `marketPlayerTeam` no es una compra, es una petición: el dueño puede decir
#    que no. Para tapar un hueco crítico van primero los agentes libres, que resuelven
#    solos, aunque salgan más caros. Un portero "barato" que el rival no te vende vale 0.
_MERCADO_POS = text("""
    SELECT o.market_id, o.player_id, o.player_team_id, p.name, o.sale_price AS precio,
           o.tipo, o.expires_at, p.status,
           GREATEST(o.sale_price, COALESCE(v.value, 0)) AS suelo,
           (o.tipo = 'marketPlayerLeague')               AS garantizado,
           CASE WHEN o.tipo = 'marketPlayerLeague'
                THEN o.num_bids ELSE o.num_offers END    AS competencia,
           CAST((p.raw->>'lastSeasonPoints') AS INTEGER) AS puntos,
           (SELECT pl.probability FROM probable_lineups pl
             WHERE pl.player_id = p.id ORDER BY pl.ts DESC LIMIT 1) AS prob_titular
    FROM market_offers o
    JOIN players p ON p.id = o.player_id
    LEFT JOIN v_latest_values v ON v.player_id = p.id
    WHERE p.position = :pos
      AND GREATEST(o.sale_price, COALESCE(v.value, 0)) <= :presupuesto
      AND (o.expires_at IS NULL OR o.expires_at > now())
      AND p.status <> 'out_of_league'
    ORDER BY garantizado DESC, suelo
""")


async def del_mercado(posicion: int, presupuesto: int) -> list[dict]:
    async with Session() as s:
        filas = await s.execute(_MERCADO_POS,
                                {"pos": posicion, "presupuesto": presupuesto})
        return [dict(f._mapping) for f in filas]


def importe_puja(precio: int, urgente: bool, suelo: int = 0) -> int:
    """Cuánto pujar u ofertar: precio por el margen, nunca por debajo del suelo.

    Con `urgente` se paga una prima del 15 % para no perder la subasta por un margen
    pequeño, porque el hueco crítico cuesta la jornada entera; sin urgencia basta con
    rozar el precio de salida (2 %).

    `suelo` es el mínimo que la API acepta (el valor de mercado). Va por encima del
    margen: da igual lo que salga del cálculo, por debajo del suelo la oferta se
    rechaza con un 400 y el hueco sigue ahí.
    """
    margen = 1.15 if urgente else 1.02
    return max(int(precio * margen), int(suelo * 1.01) if suelo else 0)


async def plan_emergencia(presupuesto: int) -> list[dict]:
    """Qué fichar por cada hueco crítico, mirando primero el mercado y luego las cláusulas.

    El mercado va antes a propósito: sale más barato, no revela intención frente a un
    rival y no consume la ventana de clausulazo, que solo está abierta a ratos
    (`030.01.17`) y no se recupera hasta la jornada siguiente.
    """
    plan: list[dict] = []
    for h in await revisar_plantilla():
        if not h.critico:
            continue
        opciones = await del_mercado(h.posicion, presupuesto)
        via = "mercado"
        if not opciones:
            opciones = await candidatos(h.posicion, presupuesto, limite=25)
            via = "clausulazo"
            for o in opciones:
                o["precio"] = o["coste"]
        if not opciones:
            plan.append({"hueco": h, "via": None,
                         "motivo": "no hay ningún candidato asequible"})
            continue

        for o in opciones:
            o["nota"] = puntuar(o)
        mejor = max(opciones, key=lambda o: o["nota"])
        # Entre notas parecidas, el más barato: el dinero sobrante tapa el siguiente hueco.
        parecidos = [o for o in opciones if o["nota"] >= mejor["nota"] * 0.9]

        # Entre candidatos de nota parecida, el agente libre gana al que vende otro
        # manager aunque cueste más: el primero lo resuelve la subasta sola y el segundo
        # depende de que el dueño diga que sí.
        garantizados = [o for o in parecidos if o.get("garantizado")]
        if not garantizados:
            # Solo quedan fichas de otros managers. Se propone igual, marcada con
            # `solo_hay_de_rival`, para que quien decida sepa que la operación puede
            # rechazarse y que la oferta enseña en qué posición estás corto.
            log.warning("hueco de %s: solo hay fichas de otros managers; la oferta puede "
                        "ser rechazada", h)
        elegido = min(garantizados or parecidos,
                      key=lambda o: o.get("suelo") or o["precio"])

        plan.append({
            "hueco": h,
            "via": via,
            "candidato": elegido,
            "importe": importe_puja(elegido["precio"], urgente=True,
                                    suelo=elegido.get("suelo") or 0) if via == "mercado"
                       else elegido["precio"],
            # Le dice al ejecutor qué endpoint tocar: pujar() o ofertar().
            "garantizado": bool(elegido.get("garantizado")) if via == "mercado" else None,
            "alternativas": len(opciones),
            "solo_hay_de_rival": via == "mercado" and not garantizados,
        })
    return plan
