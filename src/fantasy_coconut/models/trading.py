"""Trading de agentes libres: comprar para revender unos días después.

Contrato. `oportunidades(mercado, caja, plazas_libres)` devuelve los candidatos con
el importe exacto a pujar y los descartados con su motivo; `cartera()` lista las
posiciones abiertas y cuáles tocan vender; `balance()` da el retorno ya realizado.
Ninguna de las tres ejecuta compras: este módulo propone y registra.

La señal: el valor de mercado tiene inercia

El valor se recalcula cada día a las 00:15 con la oferta y demanda global (millones
de usuarios, no los de esta liga), según la puntuación de la última jornada, el volumen
de pujas, las lesiones y la titularidad prevista.

Medido sobre la serie propia (25.873 observaciones, 34 días, 836 jugadores):

| lo que hizo ayer | qué hace hoy | probabilidad de subir |
|---|---|---|
| subió >0,5 % | +2,36 % | 77,6 % |
| plano | −0,29 % | 7,6 % |
| bajó >0,5 % | −1,60 % | 2,0 % |

El jugador medio pierde −0,445 % al día (solo el 13,6 % de los días sube), así que la
señal no es "el mercado sube": es que la racha persiste.

Simulando el ciclo completo sobre esa misma muestra (comprar al valor de hoy y vender al
valor de D+k con el spread real de la oferta del sistema, +0,91 % medido en
`market/history`), comprando tras una subida >2 %:

| espera | retorno | operaciones con ganancia |
|---|---|---|
| D+3 | +11,41 % | 98,7 % |
| D+5 | +18,14 % | 97,2 % |

El límite: solo se pueden comprar agentes libres

Esas cifras son sobre todos los jugadores; comprables son solo los ~13 agentes libres
del día. En qué estado salen al mercado libre, medido sobre 104 fichas reales:
subiendo 10,6 % · plano 32,7 % · bajando 56,7 %.

- A ciegas: −3,84 %, gana el 19,2 % de las veces.
- Solo los que suben: +5,31 % en 3 días, gana el 83,3 %, con n=6.

AVISO: ese +5,31 % hay que leerlo a la baja. El momentum en que se apoya, validado contra
una temporada entera (326 días evaluados, 197.041 observaciones), da +2,474 %/día
y bate a la media en 322 de esos 326 días; esa medición vive en `models/precio.py`, que es
donde se reproduce con `walk_forward()`. La medición corta cae sobre el arranque de
liga, donde los precios se reajustan más fuerte. Esperar del orden de +2,5-3 % en 3
días, no +5 %: ese es el listón contra el que se lee `balance()`.

Con n=6 la muestra no da para automatizar la compra, y las ocasiones son ~1,5 por semana.

Lo que juega a favor (y no era obvio)

- El 93,3 % de los agentes libres no tiene ni una puja; la medición original no dejó
  anotada la muestra, así que conviene rehacerla antes de apoyarse en el número. Sin
  pujas rivales se compran al valor exacto, sin sobrepujar.
- Un fichaje queda protegido 14 días, así que una operación de trading no aumenta la
  exposición a clausulazos.

Lo que juega en contra

- Vender tarda días e inmoviliza una plaza.
- El dinero de una puja se retiene en `teamInvestment` hasta las 13:48 del día
  siguiente, y ahí no está disponible para un clausulazo.
- El tope de 24 fichas cuenta las pujas vivas (`030.01.40`).
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import bindparam, text

from ..db import Session, ddl
from ..tiempo import hace

log = logging.getLogger("coconut.models.trading")

DDL = """
CREATE TABLE IF NOT EXISTS trading_ops (
    id             bigserial PRIMARY KEY,
    player_id      integer     NOT NULL,
    nombre         text,
    abierta_en     timestamptz NOT NULL DEFAULT (now()),
    precio_compra  bigint      NOT NULL,
    valor_compra   bigint      NOT NULL,
    momentum       real,
    estado         text        NOT NULL DEFAULT 'propuesta',
    listada_en     timestamptz,
    cerrada_en     timestamptz,
    precio_venta   bigint,
    nota           text
);
CREATE INDEX IF NOT EXISTS idx_trading_abiertas ON trading_ops (estado)
    WHERE estado IN ('propuesta', 'comprada', 'listada');
"""

# ---------- parámetros. conservadores a propósito: el coste de equivocarse no es solo el ----------
# dinero, es tener una plaza ocupada y la caja retenida el día que aparece un
# ---------- clausulazo bueno. ----------
CAPITAL_MAX = 3_000_000       # nunca invertido a la vez en trading
PLAZAS_MAX = 2                # posiciones abiertas simultáneas
PLAZAS_LIBRES_MIN = 2         # nunca dejar la plantilla con menos margen que esto
MOMENTUM_MIN = 0.02           # solo subidas >2 %: con 0,5 % el margen no cubre el error
DIAS_ESPERA = 3               # vender al 3.er día: +11,4 % teórico, y no inmoviliza más


async def crear_tabla() -> None:
    async with Session() as s:
        for sentencia in ddl(DDL).strip().split(";"):
            if sentencia.strip():
                await s.execute(text(sentencia))
        await s.commit()


async def _momentum_de(ids: list[int]) -> dict[int, tuple[float, int]]:
    """Variación del último día para cada jugador: `{id: (ratio, valor_hoy)}`.

    Se compara el último valor con el anterior, no con "ayer" por fecha: si una toma
    falla, comparar contra un hueco daría 0 % y ocultaría la racha.
    """
    if not ids:
        return {}
    async with Session() as s:
        filas = (await s.execute(text("""
            SELECT player_id, date(ts) AS dia, max(value) AS v
            FROM market_values
            WHERE player_id IN :ids AND ts > :desde
            GROUP BY 1, 2 ORDER BY 1, 2""").bindparams(
                bindparam("ids", expanding=True)),
            {"ids": list(ids), "desde": hace(dias=10)})).all()
    serie: dict[int, list[int]] = {}
    for pid, _dia, v in filas:
        serie.setdefault(int(pid), []).append(int(v))
    out = {}
    for pid, xs in serie.items():
        if len(xs) >= 2 and xs[-2] > 0:
            out[pid] = ((xs[-1] - xs[-2]) / xs[-2], xs[-1])
    return out


async def oportunidades(mercado: list[dict], caja: int, plazas_libres: int) -> dict:
    """Qué agentes libres cumplen hoy, con el importe exacto a pujar.

    `mercado` es la lista cruda de `GET /league/{lg}/market`. Se filtra aquí y no en el
    endpoint para que el job y la web usen exactamente el mismo criterio.
    """
    libres = [m for m in mercado if m.get("discr") == "marketPlayerLeague"]
    ids = [int((m.get("playerMaster") or {})["id"]) for m in libres
           if (m.get("playerMaster") or {}).get("id")]
    mom = await _momentum_de(ids)

    abiertas = await cartera()
    invertido = sum(o["precio_compra"] for o in abiertas)
    margen = max(0, CAPITAL_MAX - invertido)

    candidatos, descartes = [], []
    for m in libres:
        pm = m.get("playerMaster") or {}
        pid = int(pm.get("id") or 0)
        r, valor = mom.get(pid, (None, int(pm.get("marketValue") or 0)))
        fila = {
            "player_id": pid, "nombre": pm.get("nickname") or pm.get("name"),
            "posicion": int(pm.get("positionId") or 0),
            "valor": valor, "momentum": round(r, 4) if r is not None else None,
            "market_id": str(m.get("id") or ""),
            "pujas": int(m.get("numberOfBids") or 0),
            "estado": pm.get("playerStatus"),
        }
        if r is None:
            fila["motivo"] = "sin serie de precios suficiente"
        elif r < MOMENTUM_MIN:
            fila["motivo"] = f"momentum {r*100:+.1f}% < {MOMENTUM_MIN*100:.0f}%"
        elif valor > margen:
            fila["motivo"] = (f"{valor/1e6:.2f}M pasa del capital de trading libre "
                              f"({margen/1e6:.2f}M)")
        elif valor > caja:
            fila["motivo"] = "no hay caja"
        else:
            # Se puja el VALOR exacto: el 93,3 % de los agentes libres no tiene ninguna
            # puja, así que sobrepujar solo regala margen. Y en subasta a primer precio
            # se paga lo que se ofrece.
            fila["pujar"] = valor
            candidatos.append(fila)
            continue
        descartes.append(fila)

    candidatos.sort(key=lambda d: -(d["momentum"] or 0))
    bloqueo = None
    if plazas_libres < PLAZAS_LIBRES_MIN:
        bloqueo = (f"solo quedan {plazas_libres} plazas de plantilla; el mínimo para "
                   f"operar es {PLAZAS_LIBRES_MIN}")
    elif len(abiertas) >= PLAZAS_MAX:
        bloqueo = f"ya hay {len(abiertas)} operaciones abiertas (tope {PLAZAS_MAX})"
    elif margen <= 0:
        bloqueo = f"capital de trading agotado ({CAPITAL_MAX/1e6:.1f}M)"

    return {
        "candidatos": [] if bloqueo else candidatos,
        "descartados": descartes,
        "bloqueo": bloqueo,
        "capital": {"tope": CAPITAL_MAX, "invertido": invertido, "libre": margen},
        "abiertas": len(abiertas), "plazas_libres": plazas_libres,
        "criterio": {"momentum_min": MOMENTUM_MIN, "dias_espera": DIAS_ESPERA,
                     "plazas_max": PLAZAS_MAX},
    }


async def cartera() -> list[dict]:
    """Posiciones abiertas, con los días que llevan y si toca vender."""
    async with Session() as s:
        filas = (await s.execute(text("""
            SELECT id, player_id, nombre, abierta_en, precio_compra, valor_compra,
                   momentum, estado, listada_en
            FROM trading_ops WHERE estado IN ('comprada', 'listada')
            ORDER BY abierta_en"""))).all()
    ahora = dt.datetime.now(dt.UTC)
    out = []
    for f in filas:
        dias = (ahora - f[3]).total_seconds() / 86400
        out.append({"id": f[0], "player_id": f[1], "nombre": f[2],
                    "abierta_en": f[3].isoformat(), "precio_compra": int(f[4]),
                    "valor_compra": int(f[5]), "momentum": f[6], "estado": f[7],
                    "listada_en": f[8].isoformat() if f[8] else None,
                    "dias": round(dias, 2), "toca_vender": dias >= DIAS_ESPERA})
    return out


async def balance() -> dict:
    """Retorno realizado de las operaciones cerradas: es lo que valida o refuta el backtest.

    El backtest da +5,31 % con n=6; hasta que esta tabla tenga operaciones cerradas de
    sobra, ese número es una hipótesis, no un resultado.
    """
    async with Session() as s:
        filas = (await s.execute(text("""
            SELECT precio_compra, precio_venta, nombre, abierta_en, cerrada_en
            FROM trading_ops WHERE estado = 'cerrada' AND precio_venta IS NOT NULL
            ORDER BY cerrada_en DESC"""))).all()
    ops = []
    for c, v, nom, a, cr in filas:
        ops.append({"nombre": nom, "compra": int(c), "venta": int(v),
                    "retorno": round((int(v) - int(c)) / max(int(c), 1), 4),
                    "beneficio": int(v) - int(c),
                    "dias": round((cr - a).total_seconds() / 86400, 1)})
    n = len(ops)
    return {
        "operaciones": ops, "n": n,
        "beneficio_total": sum(o["beneficio"] for o in ops),
        "retorno_medio": round(sum(o["retorno"] for o in ops) / n, 4) if n else None,
        "acierto": round(sum(1 for o in ops if o["beneficio"] > 0) / n, 3) if n else None,
        "aviso": ("Sin operaciones cerradas todavía: el +5,31 % del backtest es una "
                  "hipótesis con n=6, no un resultado." if n < 5 else
                  "Compara este retorno con el +5,31 % del backtest: si no se parece, "
                  "el modelo está midiendo otra cosa."),
    }


async def registrar(player_id: int, nombre: str, precio: int, valor: int,
                    momentum: float | None, estado: str = "comprada",
                    nota: str = "") -> int:
    async with Session() as s:
        fila = (await s.execute(text("""
            INSERT INTO trading_ops (player_id, nombre, precio_compra, valor_compra,
                                     momentum, estado, nota)
            VALUES (:p, :n, :pc, :vc, :m, :e, :nt) RETURNING id"""),
            {"p": player_id, "n": nombre, "pc": precio, "vc": valor,
             "m": momentum, "e": estado, "nt": nota})).first()
        await s.commit()
    return int(fila[0])


async def marcar(op_id: int, estado: str, precio_venta: int | None = None) -> None:
    campos = "estado = :e"
    if estado == "listada":
        campos += ", listada_en = now()"
    if estado == "cerrada":
        campos += ", cerrada_en = now(), precio_venta = :pv"
    async with Session() as s:
        await s.execute(text(f"UPDATE trading_ops SET {campos} WHERE id = :i"),
                        {"i": op_id, "e": estado, "pv": precio_venta})
        await s.commit()
