"""Modelo de precio: ordena por momentum y, aparte, estima la variación del día siguiente.

Lo que usa producción es `momentum()`, el ranking por variación del último día, y
`oportunidades()`, que lo cruza con los agentes libres comprables hoy. El modelo lineal
(`ajustar`, `evaluar`, `walk_forward`) no lo llama nadie: se lanza a mano y está aquí
porque es lo que permitió medir que no bate a la inercia.

El valor se recalcula a diario con un algoritmo determinista que refleja a la comunidad
global, no a los cuatro de esta liga, así que acertar el precio da presupuesto sin
depender del resultado deportivo. Son mínimos cuadrados a mano sobre 8 variables, con una
matriz normal de 9x9 (las 8 más el sesgo): sin pandas ni scikit-learn ni lightgbm, que no
publica rueda para `linux-aarch64` y ninguna de las tres cabe en un móvil.

Lo que se mide está en `evaluar()`. El corte de validación es temporal, nunca aleatorio:
entrenar con datos posteriores al día que se predice da un acierto irreal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import text

from ..db import Session

log = logging.getLogger("coconut.models.precio")

# Columnas del modelo. Todas se calculan mirando solo hacia atrás.
VARIABLES = ["ret1", "ret3", "ret7", "acel", "pos_rango", "vol", "dias_subiendo", "log_valor"]

SQL_SERIE = """
SELECT player_id, date(ts) AS fecha, CAST(round(AVG(value)) AS INTEGER) AS valor
FROM market_values
WHERE value > 0
GROUP BY player_id, date(ts)
ORDER BY player_id, fecha
"""


@dataclass
class Muestra:
    player_id: int
    fecha: object
    x: list[float]
    y: float                       # retorno del día siguiente, en tanto por uno
    valor: int = 0


@dataclass
class Modelo:
    coef: list[float] = field(default_factory=list)
    sesgo: float = 0.0

    def predecir(self, x: list[float]) -> float:
        # `strict=True` y no por gusto: si los coeficientes y las variables llegaran
        # desalineados, `zip` se pararía en el más corto y la predicción saldría usando
        # menos variables, con un número perfectamente plausible. Sin `strict` no hay
        # error: hay una respuesta falsa.
        return self.sesgo + sum(c * v for c, v in zip(self.coef, x, strict=True))


def _resolver(A: list[list[float]], b: list[float]) -> list[float]:
    """Eliminación gaussiana con pivoteo parcial. Devuelve ceros si es singular.

    Sin pivoteo, una columna casi constante hace que la diagonal se acerque a cero y el
    resultado se va a infinito. Con 8 variables el coste es irrelevante.
    """
    n = len(b)
    M = [[*fila[:], b[i]] for i, fila in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            log.warning("matriz singular en la columna %d: el modelo sale plano", col)
            return [0.0] * n
        M[col], M[piv] = M[piv], M[col]
        for r in range(n):
            if r == col:
                continue
            f = M[r][col] / M[col][col]
            for c in range(col, n + 1):
                M[r][c] -= f * M[col][c]
    return [M[i][n] / M[i][i] for i in range(n)]


def ajustar(muestras: list[Muestra], ridge: float = 1e-6) -> Modelo:
    """Mínimos cuadrados con regularización ridge. Devuelve coeficientes y sesgo.

    El `ridge` es necesario: sin él, dos variables muy correlacionadas (`ret1` y `acel` lo
    están) dejan la matriz casi singular y los coeficientes se disparan.
    """
    k = len(VARIABLES)
    A = [[0.0] * (k + 1) for _ in range(k + 1)]
    b = [0.0] * (k + 1)
    for m in muestras:
        fila = [*m.x, 1.0]                    # la última columna es el sesgo
        for i in range(k + 1):
            for j in range(k + 1):
                A[i][j] += fila[i] * fila[j]
            b[i] += fila[i] * m.y
    for i in range(k):
        A[i][i] += ridge * len(muestras)
    sol = _resolver(A, b)
    return Modelo(coef=sol[:k], sesgo=sol[k])


def _features(serie: list[tuple]) -> list[Muestra]:
    """Convierte la serie de un jugador en muestras.

    Hacen falta 9 días: 8 de historia más el siguiente, que es lo que se predice. La
    ventana de `pos_rango`, `vol` y `dias_subiendo` es de 14.
    """
    out = []
    for i in range(7, len(serie) - 1):
        v = [float(s[1]) for s in serie[max(0, i - 13):i + 1]]
        hoy = v[-1]
        if hoy <= 0:
            continue
        def ret(n: int, _hoy: float = hoy, _v: list[float] = v) -> float:
            """Retorno a n días. Los valores van por defecto para que quede claro que se
            fijan EN esta vuelta del bucle y no se leen del entorno al llamar."""
            return (_hoy - _v[-1 - n]) / _v[-1 - n] if len(_v) > n and _v[-1 - n] > 0 else 0.0
        alto, bajo = max(v), min(v)
        # Días seguidos subiendo: la forma más simple de capturar la inercia.
        seguidos = 0
        for j in range(len(v) - 1, 0, -1):
            if v[j] > v[j - 1]:
                seguidos += 1
            else:
                break
        difs = [abs(v[j] - v[j - 1]) / v[j - 1] for j in range(1, len(v)) if v[j - 1] > 0]
        x = [
            ret(1), ret(3), ret(7),
            ret(1) - ret(3) / 3,                              # aceleración
            (hoy - bajo) / (alto - bajo) if alto > bajo else 0.5,
            sum(difs) / len(difs) if difs else 0.0,           # volatilidad media
            min(seguidos, 7) / 7,
            (len(str(int(hoy))) - 6) / 3,                     # tamaño, en órdenes de magnitud
        ]
        manana = float(serie[i + 1][1])
        out.append(Muestra(serie[i][0], serie[i][2], x, (manana - hoy) / hoy, int(hoy)))
    return out


async def cargar_muestras() -> list[Muestra]:
    async with Session() as s:
        filas = (await s.execute(text(SQL_SERIE))).all()
    porjug: dict[int, list] = {}
    for pid, fecha, valor in filas:
        porjug.setdefault(pid, []).append((pid, valor, fecha))
    muestras = []
    for serie in porjug.values():
        if len(serie) >= 9:
            muestras.extend(_features(serie))
    log.info("muestras: %d de %d jugadores", len(muestras), len(porjug))
    return muestras


def evaluar(modelo: Modelo, prueba: list[Muestra], top: int = 50) -> dict:
    """Métricas del modelo medidas siempre contra un modelo de referencia (línea base).

    La línea base es la inercia: predecir con la variación del día anterior, sin ajustar
    nada. Sin ella las métricas no son interpretables.

    Por qué el acierto de signo a secas engaña. Medirlo en el top-50 por |predicción|
    da 1,000, y la línea base da 1,000 también. Dos motivos, los dos reales:

    - El 38,6 % de los días el valor no cambia. Un empate no es "bajada", pero contarlo
      como `y > 0 falso` regala aciertos a cualquiera que prediga bajar.
    - El top por |predicción| selecciona rachas monótonas. Un jugador puede subir cinco días
      seguidos entre el 25 % y el 33 % diario: ahí acertar el signo no distingue modelos.

    Por eso se miden tres cosas:

    1. Acierto solo sobre los días que se movieron (los empates se apartan y se cuentan).
    2. Ventaja sobre la línea base. Si sale <= 0, el modelo no aporta sobre la inercia.
    3. Retorno de cartera: cuánto rinde comprar el top-N frente al jugador medio. Es la
       única métrica expresada en euros.
    """
    if not prueba:
        return {"error": "sin datos de prueba"}

    conpred = sorted(((modelo.predecir(m.x), m) for m in prueba), key=lambda t: -abs(t[0]))
    sel = conpred[:top]

    # Los empates se apartan: no informan del signo y solo sirven para inflar la nota.
    movidos = [(p, m) for p, m in sel if m.y != 0]
    tot_mov = [m for m in prueba if m.y != 0]

    def frac(n, d):
        return round(n / d, 3) if d else None

    ac_mod = frac(sum(1 for p, m in movidos if (p > 0) == (m.y > 0)), len(movidos))
    ac_ine = frac(sum(1 for _, m in movidos if (m.x[0] > 0) == (m.y > 0)), len(movidos))

# ---------- la cartera: lo que de verdad se compraría ----------
    # Ordenar por |predicción| sirve para medir el signo, pero no es una cartera:
    # mezcla los que van a subir con los que van a desplomarse. Para "qué compro hoy" el
    # orden es por predicción a secas, de mayor a menor.
    compra = sorted(prueba, key=lambda m: -modelo.predecir(m.x))[:top]
    # Línea base de la cartera: comprar lo que más subió el día anterior, sin modelo.
    compra_inercia = sorted(prueba, key=lambda m: -m.x[0])[:top]

    def med(xs: list) -> float:
        """Retorno medio de un grupo, en porcentaje. 0 si el grupo viene vacío."""
        return sum(m.y for m in xs) / len(xs) * 100 if xs else 0.0
    ret_cartera, ret_inercia = med(compra), med(compra_inercia)
    ret_medio = med(prueba)
    return {
        "n_prueba": len(prueba),
        "pct_sin_cambio": round(100 * (1 - len(tot_mov) / len(prueba)), 1),
        "top": len(sel),
        "top_con_movimiento": len(movidos),
        "acierto_modelo": ac_mod,
        "acierto_inercia": ac_ine,
        "ventaja_sobre_inercia": (round(ac_mod - ac_ine, 3)
                                  if ac_mod is not None and ac_ine is not None else None),
        "base_suben": frac(sum(1 for m in tot_mov if m.y > 0), len(tot_mov)),
        "retorno_cartera_pct": round(ret_cartera, 3),
        "retorno_cartera_inercia_pct": round(ret_inercia, 3),
        "retorno_medio_pct": round(ret_medio, 3),
        "ventaja_cartera_pct": round(ret_cartera - ret_medio, 3),
        # La cifra que decide si el modelo aporta algo sobre la línea base.
        "cartera_vs_inercia_pct": round(ret_cartera - ret_inercia, 3),
    }


async def walk_forward(dias_prueba: int = 8, top: int = 50) -> dict:
    """Validación walk-forward: reentrena cada día y predice solo el siguiente.

    Devuelve, por día evaluado, la ventaja sobre la línea base y el retorno de cartera,
    más el agregado de cuántos días la ventaja sale positiva.

    Un único corte train/test puede salir bien por casualidad: si el
    corte cae en una semana tranquila el resultado no dice nada. Aquí se repite el
    experimento día a día (entrenando siempre solo con el pasado) y se mira si la
    ventaja se sostiene o si fue de un día suelto.
    """
    muestras = await cargar_muestras()
    fechas = sorted({m.fecha for m in muestras})
    if len(fechas) < dias_prueba + 4:
        return {"error": f"solo {len(fechas)} días de serie"}

    dias = []
    for f in fechas[-dias_prueba:]:
        entreno = [m for m in muestras if m.fecha < f]
        prueba = [m for m in muestras if m.fecha == f]
        if len(entreno) < 300 or not prueba:
            continue
        r = evaluar(ajustar(entreno), prueba, top)
        r["fecha"] = str(f)
        dias.append(r)

    utiles = [d for d in dias if d.get("ventaja_sobre_inercia") is not None]
    gana = sum(1 for d in utiles if d["ventaja_sobre_inercia"] > 0)
    gana_cartera = sum(1 for d in dias if d["ventaja_cartera_pct"] > 0)
    gana_vs_inercia = sum(1 for d in dias if d["cartera_vs_inercia_pct"] > 0)
    return {
        "dias_evaluados": len(dias),
        "dias_que_bate_a_la_inercia": f"{gana}/{len(utiles)}",
        "dias_con_cartera_mejor_que_la_media": f"{gana_cartera}/{len(dias)}",
        "dias_con_cartera_mejor_que_la_inercia": f"{gana_vs_inercia}/{len(dias)}",
        "ventaja_media_cartera_vs_inercia_pct": round(
            sum(d["cartera_vs_inercia_pct"] for d in dias) / len(dias), 3) if dias else None,
        "ventaja_media_sobre_inercia": (
            round(sum(d["ventaja_sobre_inercia"] for d in utiles) / len(utiles), 3)
            if utiles else None),
        "ventaja_media_cartera_pct": round(
            sum(d["ventaja_cartera_pct"] for d in dias) / len(dias), 3) if dias else None,
        "detalle": dias,
    }



#  MOMENTUM: la señal que sí bate a la línea base

#
# Medido con walk-forward, reentrenando cada día y prediciendo solo el siguiente:
#
#   · El modelo de 8 variables no bate a la línea base: la ventaja media es negativa,
#     o sea que no añade nada sobre "compra lo que está subiendo".
#   · La señal que ambos capturan sí es real. Validada sobre una temporada completa
#     (326 días evaluados, 197.041 observaciones, 1.189 jugadores): la cartera del
#     top-50 por subida del día anterior rinde +2,47 %/día sobre el jugador medio y
#     bate a la media en 322 de esos 326 días.
#   · Sobrevive en todos los tramos de precio, no solo en los jugadores baratos.
#   · Y persiste varios días, así que no hace falta acertar el momento exacto de vender.
#
# Avisos antes de operar con esto:
#   · Una medición corta sobre el arranque de liga da el doble de ventaja: ahí los precios
#     se reajustan más fuerte y el número sale inflado. El bueno para planificar es el de
#     la temporada entera. `walk_forward()` vuelve a medirlo en un minuto.
#   · Aquí solo entran los agentes libres, porque su subasta resuelve sola: una ficha de
#     otro manager depende de que su dueño acepte. Son un puñado de fichas al día, no el
#     maestro entero, y salen ~1,5 ocasiones por semana.
#   · Fichar tiene latencia: la subasta resuelve a las 13:48 del día siguiente, así
#     que un jugador cuyo partido sea antes de esa hora ya no sirve.
#   · Al vender, LaLiga paga entre el valor y ±10 %, no un porcentaje fijo por debajo.

SQL_MOMENTUM = """
-- `round()` por dentro NO sobra: `CAST(x AS INTEGER)` **redondea en Postgres y
-- TRUNCA en SQLite** (1394849,5 -> 1.394.850 frente a 1.394.849). Sobre la media
-- diaria de la serie de precios eso desplaza un euro en la mitad de las filas, sin
-- dar ningún error. Con `round()` los dos motores hacen lo mismo.
WITH d AS (
    SELECT player_id, date(ts) AS f, CAST(round(AVG(value)) AS INTEGER) AS v
    FROM market_values WHERE value > 0 GROUP BY 1, 2
),
u AS (
    SELECT player_id, f, v,
           ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY f DESC) AS rn
    FROM d
),
piv AS (
    SELECT player_id,
           MAX(v) FILTER (WHERE rn = 1) AS hoy,
           MAX(v) FILTER (WHERE rn = 2) AS ayer,
           MAX(v) FILTER (WHERE rn = 4) AS hace3,
           MAX(v) FILTER (WHERE rn = 8) AS hace7
    FROM u WHERE rn <= 8 GROUP BY player_id
)
SELECT p.id, p.nickname, p.position, p.status, piv.hoy, piv.ayer, piv.hace3, piv.hace7
FROM piv JOIN players p ON p.id = piv.player_id
-- La señal es `hoy` contra `ayer`, y con esos dos basta. Exigir además `hace3`
-- dejaba fuera a quien lleva menos de 4 días de serie, sin salir ni como
-- descartado: justo los recién llegados al mercado, que es donde el momentum más
-- interesa. Los retornos a 3 y 7 días salen `None` para ellos, que es lo honesto.
WHERE piv.hoy > 0 AND piv.ayer > 0
"""


async def momentum(minimo_pct: float = 0.0) -> list[dict]:
    """Ranking por variación de precio del último día, filtrado por `minimo_pct`.

    Es la señal que bate a la línea base; el modelo lineal de este fichero no (ver la
    cabecera). Devuelve también la subida a 3 y 7 días para distinguir un pico suelto de
    una tendencia sostenida, que es la que persiste.
    """
    async with Session() as s:
        filas = (await s.execute(text(SQL_MOMENTUM))).all()

    out = []
    for pid, nick, pos, estado, hoy, ayer, hace3, hace7 in filas:
        r1 = (hoy - ayer) / ayer * 100
        if r1 < minimo_pct:
            continue
        out.append({
            "player_id": pid, "nombre": nick, "posicion": pos, "status": estado,
            "valor": int(hoy),
            "sube_1d_pct": round(r1, 2),
            "sube_3d_pct": round((hoy - hace3) / hace3 * 100, 2) if hace3 else None,
            "sube_7d_pct": round((hoy - hace7) / hace7 * 100, 2) if hace7 else None,
            "euros_1d": int(hoy - ayer),
        })
    out.sort(key=lambda d: -d["sube_1d_pct"])
    return out


async def oportunidades(top: int = 15) -> dict:
    """Momentum cruzado con el mercado: los `top` mejores que hoy se pueden comprar.

    Sin este cruce el ranking no es accionable, porque la mayoría de los que más suben no
    están a la venta. Solo agentes libres (`marketPlayerLeague`), porque la subasta los
    resuelve sola: una ficha de otro manager depende de que su dueño acepte, así que
    salir aquí no significaría poder comprarla. El filtro de estado es lista blanca: pasan
    los `ok` y los que no traen estado, así que también caen los `doubtful` y cualquier
    estado nuevo que saque LaLiga.
    """
    # Sin umbral: interesa ver todos los agentes libres con su momentum, aunque sea
    # negativo. "Hay 13 y ninguno sube" es información; una lista vacía parece una avería.
    mom = {m["player_id"]: m for m in await momentum(minimo_pct=-1e9)}
    async with Session() as s:
        # La columna es `tipo` (no `discr`, que es como se llama en la API), y la tabla
        # es un upsert por `market_id` sin histórico: las fichas caducadas se quedan.
        # Sin el filtro por `expires_at` se proponen compras de un mercado de hace días.
        mercado = (await s.execute(text("""
            SELECT player_id, sale_price, expires_at, tipo FROM market_offers
            WHERE expires_at > now() AND status = 'on_sale'"""))).all()

    comprables, descartados = [], 0
    for pid, precio_venta, expira, tipo in mercado:
        m = mom.get(pid)
        if not m:
            continue
        if tipo != "marketPlayerLeague":
            descartados += 1
            continue
        if m["status"] not in ("ok", None):
            continue
        comprables.append({**m, "precio_salida": int(precio_venta or 0),
                           "expira": str(expira) if expira else None})
    comprables.sort(key=lambda d: -d["sube_1d_pct"])

    return {
        "comprables": comprables[:top],
        "en_mercado_pero_de_rival": descartados,
        "mejores_del_dia_no_comprables": [
            m for m in list(mom.values())[:top] if m["player_id"] not in
            {c["player_id"] for c in comprables}][:5],
        "nota": ("La ventaja medida es +2,5 %/día sobre la media, y persiste varios días. "
                 "Ojo: la subasta resuelve a las 13:48 del día siguiente."),
    }
