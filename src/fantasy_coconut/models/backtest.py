"""Registrar lo que se predice y comprobar después si se acertó.

Por qué hace falta

Con 38 jornadas la varianza es enorme: una temporada entera cabe dentro del margen de la
suerte. Sin un registro de lo que se predijo antes de saber el resultado, no hay forma de
distinguir un criterio que funciona de una racha.

La regla

Toda predicción se guarda antes de saber el resultado, con las variables que la
sostienen. Después, `liquidar()` rellena el `actual` cuando el dato ya existe. Guardar la
predicción una vez conocido el resultado invalida la medición, así que el `ts` es el del
momento de decidir, nunca el de la comprobación.

`features` guarda el contexto en JSON para poder responder *por qué* se decidió algo meses
después, que es lo que permite distinguir un modelo malo de la mala suerte.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text

from ..db import Session, json_param

log = logging.getLogger("coconut.models.backtest")

# Horizontes. El nombre entra en la clave de deduplicación, así que se dejan explícitos.
H_PRECIO_1D = "precio_1d"
H_PUNTOS_JORNADA = "puntos_jornada"


async def registrar(model: str, horizon: str, filas: list[dict]) -> int:
    """Guarda predicciones. `filas`: `{player_id, value, lo?, hi?, features?}`.

    No deduplica a propósito: si un modelo predice dos veces el mismo día es un dato sobre
    el modelo, y borrarlo escondería el problema.
    """
    if not filas:
        return 0
    async with Session() as s:
        await s.execute(text("""
            INSERT INTO predictions (model, player_id, horizon, value, lo, hi, features)
            VALUES (:m, :p, :h, :v, :lo, :hi, {F})""".replace("{F}", json_param(":f"))),
            [{"m": model, "h": horizon, "p": f.get("player_id"),
              "v": float(f.get("value") or 0), "lo": f.get("lo"), "hi": f.get("hi"),
              "f": json.dumps(f.get("features") or {}, default=str)} for f in filas])
        await s.commit()
    log.info("backtest: %d predicciones de %s/%s", len(filas), model, horizon)
    return len(filas)


async def liquidar_precio() -> dict:
    """Rellena el `actual` de las predicciones de precio a 1 día.

    El real se saca de la propia serie: el valor del día siguiente al de la predicción.
    Solo se liquidan las que ya tienen ese día ingerido; el resto esperan.
    """
    async with Session() as s:
        res = await s.execute(text("""
            WITH d AS (
                SELECT player_id, date(ts) AS f, AVG(value) AS v
                FROM market_values WHERE value > 0 GROUP BY 1, 2
            ),
            pend AS (
                SELECT p.id, p.player_id, date(p.ts) AS dia
                FROM predictions p
                WHERE p.actual IS NULL AND p.horizon = :h AND p.player_id IS NOT NULL
            )
            UPDATE predictions SET actual = calc.real
            FROM (
                SELECT pend.id, (dm.v - dh.v) / dh.v * 100 AS real
                FROM pend
                JOIN d dh ON dh.player_id = pend.player_id AND dh.f = pend.dia
                JOIN d dm ON dm.player_id = pend.player_id AND dm.f = pend.dia + 1
                WHERE dh.v > 0
            ) AS calc
            WHERE predictions.id = calc.id
            RETURNING predictions.id"""), {"h": H_PRECIO_1D})
        n = len(res.all())
        await s.commit()
    log.info("backtest: %d predicciones de precio liquidadas", n)
    return {"liquidadas": n}


async def nota(model: str | None = None) -> list[dict]:
    """La nota de cada modelo: acierto de signo y error medio, solo sobre lo liquidado.

    `n` importa tanto como el acierto. Un 100 % con n=3 no dice nada, y un acierto
    perfecto es una alarma sobre la métrica antes que un resultado: conviene comprobar
    qué saca la referencia tonta (la inercia) con los mismos datos.
    """
    async with Session() as s:
        filas = (await s.execute(text("""
            SELECT model, horizon, count(*) AS n,
                   avg(CASE WHEN (value > 0) = (actual > 0) THEN 1.0 ELSE 0.0 END) AS signo,
                   avg(abs(value - actual)) AS err,
                   avg(actual) AS real_medio,
                   date(min(ts)) AS desde
            FROM predictions
            -- DOS cosas, y la segunda solo se ve en SQLite:
            --  · Sin el CAST, asyncpg no puede inferir el tipo del parámetro y falla
            --    con «could not determine data type».
            --  · **Nunca se escribe el nombre de un parámetro dentro de un comentario
            --    SQL.** SQLAlchemy los sustituye TAMBIÉN ahí, así que cuenta uno de más
            --    mientras el motor ignora el del comentario: «Incorrect number of
            --    bindings supplied».
            WHERE actual IS NOT NULL
              AND (CAST(:m AS text) IS NULL OR model = CAST(:m AS text))
            GROUP BY model, horizon ORDER BY n DESC"""), {"m": model})).all()
    return [{
        "model": r[0], "horizon": r[1], "n": r[2],
        "acierto_signo": round(float(r[3]), 3) if r[3] is not None else None,
        "error_medio": round(float(r[4]), 3) if r[4] is not None else None,
        "movimiento_real_medio": round(float(r[5]), 3) if r[5] is not None else None,
        "desde": str(r[6]),
        "fiable": (r[2] or 0) >= 100,
    } for r in filas]


async def registrar_momentum_de_hoy() -> dict:
    """Guarda la predicción diaria de precio. Va en el scheduler, después de la ingesta.

    Se apunta el ranking entero de los que suben, no solo los comprables: así se puede
    medir la señal aunque ese día no se compre nada, que es casi siempre.

    Una por día, y la guarda importa. Cuelga de `job_players`, que corre 4×/día: sin
    ella se apuntarían cuatro copias casi idénticas de la misma predicción, el valor real
    del día siguiente liquidaría las cuatro con el mismo número, y `n` saldría ×4 sin una
    sola observación nueva. Un backtest con la muestra inflada no da error: da una
    fiabilidad falsa, que es justo el fallo contra el que existe esta tabla.

    La guarda vive en la BD y no en memoria, así que un reinicio del worker no la burla.
    """
    from . import precio  # import perezoso: evita un ciclo con models/precio.py

    async with Session() as s:
        ya = (await s.execute(text("""
            SELECT count(*) FROM predictions
            WHERE model = 'momentum' AND horizon = :h AND date(ts) = current_date"""),
            {"h": H_PRECIO_1D})).scalar_one()
    if ya:
        return {"registradas": 0, "motivo": f"ya hay {ya} predicciones de hoy"}

    ranking = await precio.momentum(minimo_pct=0.5)
    if not ranking:
        return {"registradas": 0, "motivo": "ningún jugador sube hoy"}

    filas = [{
        "player_id": m["player_id"],
        # Se predice que mañana repite la subida de hoy: es la regla que ganó el
        # backtest. Si algún día un modelo la bate, se cambia esta línea y se compara.
        "value": m["sube_1d_pct"],
        "features": {"sube_1d": m["sube_1d_pct"], "sube_3d": m["sube_3d_pct"],
                     "sube_7d": m["sube_7d_pct"], "valor": m["valor"],
                     "pos": m["posicion"]},
    } for m in ranking[:60]]
    n = await registrar("momentum", H_PRECIO_1D, filas)
    return {"registradas": n}
