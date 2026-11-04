"""Calendario de dificultad: rating ofensivo y defensivo por equipo, y cuánto pesa el rival.

Contrato. `fuerzas()` devuelve por equipo un rating ofensivo y otro defensivo,
suavizados hacia la media de la liga y con su `n`; `dificultad()` traduce un cruce
concreto a un nivel 1-5 dependiente de la posición; `prob_porteria_cero()` da una
probabilidad aproximada y no calibrada.

De dónde sale la idea

Es un planteamiento estándar en fantasy (FPL, Dixon-Coles y derivados): un jugador no
vale lo mismo según el rival.

- Un defensa o portero contra un equipo que marca mucho tiene menos probabilidad de
  portería a cero, que es una parte grande de sus puntos posibles.
- Un atacante contra una defensa sólida tiene menos ocasiones.
- Y todo eso hay que multiplicarlo por la probabilidad de jugar: un jugador con 6
  puntos esperados que sale titular el 60 % de las veces vale 3,6, no 6.

Por qué no se usa un Dixon-Coles

Dixon-Coles modela goles con una Poisson por equipo, y para eso hacen falta los
marcadores. `GET /calendar` los declara en `localScore`/`visitorScore` y llegan a `None`
incluso con la jornada jugada, así que no hay Poisson que ajustar. Es lo que se observa,
no una propiedad garantizada: si algún día llegan, la decisión se puede revisar.

Lo que sí hay, en una sola petición (el maestro que ya se ingiere), es `weekPoints`:
los puntos de cada jugador en cada jornada. Y ahí está la señal, porque el sistema de
puntuación de LaLiga Fantasy ya incorpora los goles:

- Los puntos de porteros y defensas suben con la portería a cero y bajan al encajar:
  sirven de rating defensivo del equipo.
- Los de medios y delanteros suben al marcar y asistir: rating ofensivo.

O sea que no es un modelo de goles sino un modelo de lo que puntúa, que es lo que se
quiere predecir.

Límites

- Con pocas jornadas esto ordena, no mide. Cada rating lleva su `n` y se suaviza hacia
  la media (`K_SUAVIZADO`): sin eso, un equipo con un partido bueno sale como el mejor de
  la liga.
- Esto no entra en la alineación, y es una decisión medida. Multiplicar la media
  previa de cada jugador por la dificultad del rival empeora la correlación con los
  puntos reales: 0,4111 frente a 0,4266 de la media sola, y un factor aleatorio de control
  da 0,4359 (601 casos, 2 jornadas). La explicación es que el efecto del rival ya está
  dentro de la media del propio jugador. Sirve para leerlo y para decidir a mano un
  caso extremo, no para alinear.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text

from ..db import ES_SQLITE, Session

log = logging.getLogger("coconut.models.dificultad")

# Cuántas jornadas de "equipo medio" se suman a cada rating. Con pocas jornadas reales,
# un K alto evita que un solo partido bueno dispare a un equipo al primer puesto.
K_SUAVIZADO = 6.0

DEFENSIVOS = (1, 2)      # portero y defensa
OFENSIVOS = (3, 4)       # medio y delantero


@dataclass
class Fuerza:
    team_id: int
    ataque: float          # puntos medios de sus medios+delanteros por jornada
    defensa: float         # puntos medios de sus porteros+defensas por jornada
    n_ataque: int
    n_defensa: int

    @property
    def fiable(self) -> bool:
        """Falso con menos de 20 observaciones por lado: el rating no es interpretable."""
        return min(self.n_ataque, self.n_defensa) >= 20


# Esta es la única consulta del proyecto con dos versiones, y por un motivo concreto:
# desplegar un array JSON en filas es una función que devuelve filas, y ahí cada motor
# tiene la suya (`jsonb_array_elements` frente a `json_each`) sin forma neutra de
# escribirlo. Todo lo demás del JSON (`->>`, `->`, `jsonb_array_length`, `jsonb_typeof`)
# sí es común, bien porque SQLite lo trae o porque se registra en `sqlite_compat`.
#
# La diferencia práctica: en Postgres el alias `w` es el elemento; en SQLite `json_each`
# devuelve una fila con columnas y el elemento está en `w.value`.
_SQL_PUNTOS_PG = """
SELECT p.team_id, p.position, CAST((w->>'weekNumber') AS INTEGER) AS jornada,
       CAST((w->>'points') AS INTEGER) AS puntos
FROM players p, jsonb_array_elements(p.raw->'weekPoints') AS w
WHERE p.team_id IS NOT NULL
  AND jsonb_typeof(p.raw->'weekPoints') = 'array'
"""

_SQL_PUNTOS_SQLITE = """
SELECT p.team_id, p.position,
       CAST((w.value->>'weekNumber') AS INTEGER) AS jornada,
       CAST((w.value->>'points') AS INTEGER) AS puntos
FROM players p, json_each(p.raw, '$.weekPoints') AS w
WHERE p.team_id IS NOT NULL
  AND json_type(p.raw, '$.weekPoints') = 'array'
"""

SQL_PUNTOS = _SQL_PUNTOS_SQLITE if ES_SQLITE else _SQL_PUNTOS_PG


async def fuerzas() -> dict[int, Fuerza]:
    """Rating ofensivo y defensivo de cada equipo, suavizado hacia la media.

    Se cuenta solo a quien jugó (puntos distintos de cero): incluir a los suplentes
    que no salieron hundiría a los equipos con plantilla ancha, que es justo lo contrario
    de lo que se quiere medir.
    """
    async with Session() as s:
        filas = (await s.execute(text(SQL_PUNTOS))).all()

    ata: dict[int, list[int]] = {}
    defe: dict[int, list[int]] = {}
    for team_id, pos, _jornada, puntos in filas:
        if puntos == 0:
            continue
        destino = ata if pos in OFENSIVOS else defe if pos in DEFENSIVOS else None
        if destino is not None:
            destino.setdefault(int(team_id), []).append(int(puntos))

    todos_a = [p for v in ata.values() for p in v]
    todos_d = [p for v in defe.values() for p in v]
    media_a = sum(todos_a) / len(todos_a) if todos_a else 0.0
    media_d = sum(todos_d) / len(todos_d) if todos_d else 0.0

    def suave(vals: list[int], media: float) -> float:
        # Media bayesiana simple: los pocos datos tiran hacia el equipo promedio.
        return (sum(vals) + media * K_SUAVIZADO) / (len(vals) + K_SUAVIZADO)

    out: dict[int, Fuerza] = {}
    for team_id in set(ata) | set(defe):
        va, vd = ata.get(team_id, []), defe.get(team_id, [])
        out[team_id] = Fuerza(team_id=team_id,
                              ataque=round(suave(va, media_a), 3),
                              defensa=round(suave(vd, media_d), 3),
                              n_ataque=len(va), n_defensa=len(vd))
    log.info("fuerzas de %d equipos · media ataque %.2f, defensa %.2f",
             len(out), media_a, media_d)
    return out


def dificultad(posicion: int, rival: Fuerza | None,
               fuerzas_todas: dict[int, Fuerza]) -> dict:
    """Cómo de duro es ese rival PARA ESA POSICIÓN, de 1 (fácil) a 5 (durísimo).

    La dificultad no es una propiedad del rival, sino del cruce. Para un defensa
    lo que importa es el ataque del rival; para un delantero, su defensa. Un
    equipo puede ser blando para lo uno y durísimo para lo otro, y tratarlo con un solo
    número (como hacen los FDR de colores) pierde justo esa distinción.
    """
    if rival is None or not fuerzas_todas:
        return {"nivel": 3, "etiqueta": "sin datos", "valor": None}

    contra = rival.ataque if posicion in DEFENSIVOS else rival.defensa
    valores = sorted((f.ataque if posicion in DEFENSIVOS else f.defensa)
                     for f in fuerzas_todas.values())
    if not valores:
        return {"nivel": 3, "etiqueta": "sin datos", "valor": None}

    # Percentil del rival dentro de la liga, repartido en cinco niveles como el FDR clásico.
    pos_rel = sum(1 for v in valores if v < contra) / len(valores)
    nivel = min(5, max(1, int(pos_rel * 5) + 1))
    etiquetas = {1: "muy fácil", 2: "fácil", 3: "normal", 4: "difícil", 5: "durísimo"}
    return {"nivel": nivel, "etiqueta": etiquetas[nivel], "valor": round(contra, 2),
            "fiable": rival.fiable}


def prob_porteria_cero(rival: Fuerza | None, fuerzas_todas: dict[int, Fuerza]) -> float | None:
    """Probabilidad aproximada de que el equipo LOCAL de esa ficha deje la portería a cero.

    Es una aproximación, no una probabilidad calibrada. Lo correcto sería sacarla
    de las cuotas de apuestas (`P(under 0.5 goles del rival)`), que es la fuente pública
    mejor calibrada que existe y sigue pendiente. Esto se apoya en el ataque del rival
    frente a la liga, así que sirve para ordenar porteros y defensas entre sí, no para
    apostar sobre el número.

    Se ancla en la tasa real del fútbol (alrededor del 30 % de los partidos acaban con
    portería a cero) y se mueve ±15 puntos según lo bueno que sea el ataque contrario.
    """
    if rival is None or not fuerzas_todas:
        return None
    ataques = sorted(f.ataque for f in fuerzas_todas.values())
    if not ataques:
        return None
    pos_rel = sum(1 for v in ataques if v < rival.ataque) / len(ataques)
    # pos_rel 0 (ataque más flojo) ~0,45 · pos_rel 1 (el mejor ataque) ~0,15
    return round(0.45 - 0.30 * pos_rel, 3)
