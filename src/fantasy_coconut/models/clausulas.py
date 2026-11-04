"""Índice de vulnerabilidad de cláusula, medido en coste por punto esperado.

En una liga pequeña el clausulazo es el mecanismo dominante, así que interesan dos cosas:
a quién puedes robar barato por punto esperado y a quién te pueden robar a ti.

    coste_por_punto = cláusula / xPts_restantes_temporada

Las dos filtran por presupuesto y ninguna descarta a los protegidos: exponen `protegido` y
decide quien llama.

El BLINDAJE no lo expone ninguna de las dos, y no es un olvido: vive en la API y dura 24 h,
así que el snapshot no lo tiene fresco. Quien avise a partir de esto cruza con
`vigilancia.blindados_ahora()`. Para elegir a quién blindar está `models/blindaje.py`, que
sí descarta lo que nadie puede pagar.
"""
from sqlalchemy import text

from ..db import Session

# Dos detalles de SQL que valen para Postgres Y para SQLite, y que si se "simplifican"
# dejan de dar error y pasan a dar un número falso:
#
#   · `DISTINCT ON (col)` es de Postgres. El equivalente estándar es
#     `row_number() OVER (PARTITION BY col ORDER BY …) = 1`.
#   · El coste lleva dos casts anidados, y cada uno tapa un fallo distinto:
#       - el interno `FLOAT` fuerza división real. Sin él, en SQLite
#         `buyout_clause / xpts` es división ENTERA y el coste por punto sale 0.
#       - el externo `NUMERIC` es para `round`, porque Postgres no tiene
#         `round(double precision, int)`, solo `round(numeric, int)`.
SQL_OBJETIVOS = text("""
WITH x AS (
  SELECT player_id, xpts FROM (
    SELECT player_id, value AS xpts,
           row_number() OVER (PARTITION BY player_id ORDER BY ts DESC) AS rn
    FROM predictions
    WHERE model = 'xpts_season_v1')
  WHERE rn = 1
)
SELECT t.player_id, t.name, t.owner, t.buyout_clause, t.market_value,
       -- `protegido` es true cuando está dentro de sus 14 días, o sea cuando NO se puede
       -- clausular. Aquí solo se expone; nadie filtra por él.
       t.clause_ratio, t.protegido,
       x.xpts,
       round(CAST(CAST(t.buyout_clause AS FLOAT) / NULLIF(x.xpts, 0) / 1e6 AS NUMERIC), 3)
           AS coste_por_punto_m
FROM v_clausula_targets t
LEFT JOIN x ON x.player_id = t.player_id
WHERE t.buyout_clause <= :saldo
-- Sin el desempate por `player_id`, el orden lo decide el motor: casi todos los objetivos
-- tienen `coste_por_punto_m` a NULL porque falta la predicción de puntos, y entonces
-- Postgres y SQLite devuelven jugadores distintos en el top-10 con los mismos datos.
--
-- Y el alias va en minúsculas a propósito: Postgres pasa a minúsculas los identificadores
-- sin comillas y SQLite conserva las mayúsculas, así que `coste_por_punto_M` llegaría a
-- Python con dos nombres distintos según el motor.
ORDER BY coste_por_punto_m ASC NULLS LAST, t.player_id
LIMIT :lim
""")

SQL_EXPOSICION = text("""
SELECT p.name, s.buyout_clause, v.value AS market_value,
       round(CAST(CAST(s.buyout_clause AS FLOAT) / NULLIF(v.value,0) AS NUMERIC), 2) AS ratio,
       -- Mismo flag que en `SQL_OBJETIVOS`: sin él, esta consulta no da forma de saber
       -- quién está dentro de sus 14 días, y la lista mete intocables como si fueran
       -- alcanzables. `v_latest_squads` sí trae la fecha.
       (s.clause_lock_until IS NOT NULL AND s.clause_lock_until > now()) AS protegido
FROM v_latest_squads s
JOIN managers m ON m.id = s.manager_id AND m.is_me
JOIN players  p ON p.id = s.player_id
JOIN v_latest_values v ON v.player_id = p.id
WHERE s.buyout_clause <= :saldo_rival_max
-- Desempate por nombre: muchos jugadores tienen ratio exactamente 1,00 (cláusula igual
-- al valor), y sin esto el orden entre ellos depende del motor.
ORDER BY ratio ASC, p.name
""")


async def objetivos_baratos(saldo: int, limite: int = 15):
    """Jugadores ajenos que caben en `saldo`, del más barato por punto esperado al más caro."""
    async with Session() as s:
        rows = await s.execute(SQL_OBJETIVOS, {"saldo": saldo, "lim": limite})
        return [dict(r._mapping) for r in rows]


async def exposicion_propia(saldo_rival_max: int):
    """Cuáles de los míos están al alcance del rival más rico, del más apetecible al menos.

    Apetecible es el ratio cláusula/valor, no la cláusula: lo que invita a robar es pagar
    poco por lo que vale, no que sea barato en absoluto.

    Trae `protegido` pero no filtra por él, ni sabe nada del blindaje. Ver el docstring
    del módulo.
    """
    async with Session() as s:
        rows = await s.execute(SQL_EXPOSICION, {"saldo_rival_max": saldo_rival_max})
        return [dict(r._mapping) for r in rows]
