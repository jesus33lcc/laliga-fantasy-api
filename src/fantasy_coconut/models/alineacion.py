"""Selecciona la formación que maximiza la suma de puntos estimados y ordena los suplentes.

No mueve dinero, y un once incompleto anula la jornada entera (evento de tablón tipo 7),
así que el coste de no hacerlo es la jornada completa.

A cada jugador se le estima lo que va a puntuar (titularidad por rendimiento esperado),
se prueban todas las formaciones permitidas y gana la de mayor suma. Mejor significa aquí
máxima suma estimada, no mejor resultado garantizado. El resto va al banquillo con el
mismo orden.

La dificultad del rival no entra: se midió y empeora la predicción (los números están en
`models/dificultad.py`).

Formaciones (defensas, medios, delanteros; el portero es siempre 1):
    5-4-1 · 5-3-2 · 4-5-1 · 4-4-2 · 4-3-3 · 3-5-2 · 3-4-3
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import text

from ..db import Session

log = logging.getLogger("coconut.alineacion")

PORTERO, DEFENSA, MEDIO, DELANTERO = 1, 2, 3, 4

FORMACIONES = [
    (5, 4, 1), (5, 3, 2), (4, 5, 1), (4, 4, 2), (4, 3, 3), (3, 5, 2), (3, 4, 3),
]

# Sin dato de titularidad se asume mediocre: es más seguro dejar fuera a un incógnito
# que sentar a uno del que sí consta que juega.
PROB_POR_DEFECTO = 0.4

# Puntos de una temporada estimados desde el valor de mercado, para cuando
# `lastSeasonPoints` viene vacío: fichajes de fuera, ascendidos y canteranos, que son 22 de
# los 59 jugadores con dueño. Tratar ese hueco como un 0 los manda al banquillo por detrás
# de cualquier veterano en declive, por muy titulares que sean.
#
# La relación no es lineal, y por eso no vale un «pts por millón» único. Sobre los 424
# jugadores que tienen los dos datos, la media de puntos por millón va de 44,2 por debajo
# de 5M a 1,7 por encima de 100M: la misma recta que acierta en la banda de 5-20M
# sobreestima ocho veces en la de arriba.
#
# Exponente y constante ajustados sobre esos 424, y el tope es el máximo que se ha
# observado nunca en una temporada completa.
PREVIA_K = 43.0
PREVIA_EXP = 0.31
PREVIA_TOPE = 258.0


def previa_por_valor(valor: int | None) -> float:
    """Puntos de temporada estimados desde el valor de mercado.

    Medido contra los 424 jugadores con los dos datos, error absoluto medio:

        media constante (referencia)   49,6
        `valor_en_millones * 14,1`    106,0   <- pierde contra la referencia
        esta función                   37,4

    El lineal fallaba por el extremo: a un jugador de 193M le atribuía 2.722 puntos de
    temporada, diez veces el máximo que existe, y su esperado salía en 31 puntos por
    jornada. Con seis jugadores así en una plantilla, el once estimado de ese manager
    doblaba el real.
    """
    if not valor:
        return 0.0
    return min(PREVIA_K * (valor / 1e6) ** PREVIA_EXP, PREVIA_TOPE)
# Jornadas 'virtuales' de la referencia previa: con K=4, dos jornadas jugadas pesan 1/3.
K_SUAVIZADO = 4.0


@dataclass
class Jugador:
    player_id: int
    player_team_id: str | None
    nombre: str
    posicion: int
    estado: str | None
    puntos_previos: int | None
    prob_titular: float | None
    valor: int | None = None
    # Rendimiento de la temporada EN curso.
    puntos_temporada: int = 0
    jornadas: int = 0
    # Jornadas ya disputadas por la liga: denominador de la tasa de participación.
    jornadas_liga: int = 0

    @property
    def puntos_referencia(self) -> float:
        """Puntos esperados en una temporada completa.

        Mezcla la temporada pasada con el ritmo de esta, con un peso que crece según las
        jornadas jugadas (media bayesiana, K=4): con 2 jornadas el dato nuevo pesa 1/3,
        con 8 pesa 2/3. Las dos señales por separado predicen peor que la mezcla, y K=4
        está en el óptimo medido (K=6 da +0,0011, que es ruido).

        Prudente a propósito: mirar solo `lastSeasonPoints` decide con datos de hace un
        año y deja fuera a los fichajes que están rindiendo, pero un jugador con 0 puntos
        en dos jornadas puede hacer 6 en la tercera, así que sobrerreaccionar a dos
        partidos también se paga.
        """
        previa = (float(self.puntos_previos) if self.puntos_previos
                  else previa_por_valor(self.valor))
        if not self.jornadas:
            return previa
        # Ritmo actual llevado a temporada completa (38 jornadas).
        actual = (self.puntos_temporada / self.jornadas) * 38.0
        peso = self.jornadas / (self.jornadas + K_SUAVIZADO)
        return peso * actual + (1 - peso) * previa

    @property
    def esperado(self) -> float:
        """Puntos esperados, en unidades relativas (no son puntos reales de fantasy)."""
        if (self.estado or "").lower() in ("injured", "suspended", "out_of_league"):
            return 0.0
        if self.prob_titular is not None:
            # Hay dato de la fuente: manda, incluso si es 0. Un 0 no es "no se sabe", es
            # el peor tramo medido (32,8 % puntúa, frente al 93,3 % de los de 0,8), y
            # pisarlo con la participación empeora la predicción: media x prob correla
            # +0,4310 con los puntos reales frente a +0,4245 pisándolo, sobre las
            # jornadas 4-11, con un control aleatorio en +0,3125. Lo mide
            # `scripts/medir_prob_titular.py`.
            prob = self.prob_titular
        else:
            # Sin dato: `prob_titular` mide SER titular, no jugar, y a quien la fuente no
            # cubra el defecto lo mandaría al banquillo sin motivo. Se corrige con la
            # participación real: si ha puntuado en todas las jornadas disputadas, su
            # probabilidad de jugar es 1 por mucho que salga del banquillo.
            prob = PROB_POR_DEFECTO
            if self.jornadas_liga:
                prob = max(prob, self.jornadas / self.jornadas_liga)
        base = self.puntos_referencia / 38.0          # media por jornada
        if (self.estado or "").lower() == "doubtful":
            prob *= 0.5
        return prob * base

    def __str__(self) -> str:
        p = f"{int(self.prob_titular*100)}%" if self.prob_titular is not None else "?"
        return f"{self.nombre} ({p}, {self.puntos_previos or '?'}pts → {self.esperado:.2f})"


@dataclass
class Once:
    formacion: tuple[int, int, int]
    portero: Jugador | None
    defensas: list[Jugador] = field(default_factory=list)
    medios: list[Jugador] = field(default_factory=list)
    delanteros: list[Jugador] = field(default_factory=list)
    banquillo: list[Jugador] = field(default_factory=list)
    completo: bool = True
    faltan: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        jugadores = ([self.portero] if self.portero else []) + \
                    self.defensas + self.medios + self.delanteros
        return sum(j.esperado for j in jugadores)

    @property
    def titulares(self) -> list[Jugador]:
        return ([self.portero] if self.portero else []) + \
               self.defensas + self.medios + self.delanteros

    def __str__(self) -> str:
        d, m, dl = self.formacion
        estado = "" if self.completo else f"  INCOMPLETO: falta {', '.join(self.faltan)}"
        return f"{d}-{m}-{dl} · esperado {self.total:.2f}{estado}"


_MI_PLANTILLA = text("""
    SELECT p.id, p.name, p.position, p.status, s.player_team_id,
           CAST((p.raw->>'lastSeasonPoints') AS INTEGER) AS puntos,
           -- Rendimiento de esta temporada. `raw->weekPoints` llega vacío para parte
           -- de la liga en el maestro público, así que la fuente es `player_points`,
           -- que cachea lo que trae `/player/{id}`, y `raw` queda de respaldo.
           --
           -- El `NULLIF` de la cuenta hace falta porque las dos columnas no caen al
           -- respaldo igual: `sum()` sobre cero filas devuelve NULL, pero `count(*)`
           -- devuelve 0 y el COALESCE se queda en ese 0. Con `jornadas = 0` la
           -- referencia es `lastSeasonPoints` entera, así que un fichaje recién
           -- comprado se juzga solo por el año pasado: un defensa con 48 puntos este
           -- año y 44 el anterior pasa de 4,39 a 1,16 de esperado y sale del once.
           COALESCE((SELECT sum(pp.points) FROM player_points pp
                      WHERE pp.player_id = p.id AND pp.points IS NOT NULL),
                    CAST((p.raw->>'points') AS INTEGER), 0) AS puntos_temporada,
           COALESCE(NULLIF((SELECT count(*) FROM player_points pp
                             WHERE pp.player_id = p.id AND pp.points IS NOT NULL), 0),
                    jsonb_array_length(p.raw->'weekPoints'), 0) AS jornadas,
           -- `MAX` por si algún día hay más de una fuente: hoy solo está futbolfantasy
           -- y la PK (player_id, jornada, source) ya impide duplicados. La jornada la
           -- fija la ingesta desde `week/current`; aquí solo se lee la última escrita.
           (SELECT MAX(pl.probability) FROM probable_lineups pl
             WHERE pl.player_id = p.id
               AND pl.jornada = (SELECT MAX(jornada) FROM probable_lineups)) AS prob,
           v.value AS valor
    FROM squad_snapshots s
    JOIN players p  ON p.id = s.player_id
    JOIN managers m ON m.id = s.manager_id
    LEFT JOIN v_latest_values v ON v.player_id = p.id
    WHERE s.ts = (SELECT max(ts) FROM squad_snapshots) AND m.is_me
""")


async def mi_plantilla() -> list[Jugador]:
    async with Session() as s:
        jugadores = [
            Jugador(player_id=r[0], nombre=r[1], posicion=r[2] or 0, estado=r[3],
                    player_team_id=r[4], puntos_previos=r[5],
                    puntos_temporada=int(r[6] or 0), jornadas=int(r[7] or 0),
                    # `is not None`, no truthiness: una probabilidad de 0 es un dato, y de
                    # los más informativos. Con `if r[8]` pasaría por "sin datos" y
                    # recibiría el trato de a quien la fuente no cubre.
                    prob_titular=float(r[8]) if r[8] is not None else None,
                    valor=int(r[9]) if r[9] else None)
            for r in await s.execute(_MI_PLANTILLA)
        ]
    # Jornadas ya disputadas: el máximo de partidos que ha jugado cualquiera de los míos.
    # Sirve de denominador para la tasa de participación.
    disputadas = max((j.jornadas for j in jugadores), default=0)
    for j in jugadores:
        j.jornadas_liga = disputadas
    return jugadores


def _mejores(jugadores: list[Jugador], posicion: int, n: int) -> tuple[list[Jugador], int]:
    """Los n mejores de una posición. Devuelve también cuántos faltaron."""
    disponibles = sorted((j for j in jugadores if j.posicion == posicion),
                         key=lambda j: -j.esperado)
    return disponibles[:n], max(n - len(disponibles), 0)


def construir(jugadores: list[Jugador], formacion: tuple[int, int, int]) -> Once:
    nd, nm, ndl = formacion
    porteros, faltan_por = _mejores(jugadores, PORTERO, 1)
    defensas, faltan_def = _mejores(jugadores, DEFENSA, nd)
    medios, faltan_med = _mejores(jugadores, MEDIO, nm)
    delanteros, faltan_del = _mejores(jugadores, DELANTERO, ndl)

    faltan: list[str] = []
    for n, etiqueta in ((faltan_por, "portero"), (faltan_def, "defensa"),
                        (faltan_med, "medio"), (faltan_del, "delantero")):
        if n:
            faltan.append(f"{n} {etiqueta}{'s' if n > 1 else ''}")

    titulares = {j.player_id for j in porteros + defensas + medios + delanteros}
    banquillo = sorted((j for j in jugadores if j.player_id not in titulares),
                       key=lambda j: -j.esperado)

    return Once(formacion=formacion,
                portero=porteros[0] if porteros else None,
                defensas=defensas, medios=medios, delanteros=delanteros,
                banquillo=banquillo, completo=not faltan, faltan=faltan)


async def mejor_once(jugadores: list[Jugador] | None = None) -> Once:
    """Prueba todas las formaciones y devuelve la que más puntos estimados suma.

    Si ninguna se puede completar (falta gente), devuelve la mejor de las incompletas
    marcada con `completo=False` y `faltan`, para que el llamante sepa qué falta.
    No es una alineación mandable: `valido()` la rechaza, y un once de diez anula la
    jornada igual que no alinear (evento tipo 7).
    """
    jugadores = jugadores if jugadores is not None else await mi_plantilla()
    onces = [construir(jugadores, f) for f in FORMACIONES]
    completos = [o for o in onces if o.completo]
    candidatos = completos or onces
    mejor = max(candidatos, key=lambda o: o.total)
    if not completos:
        log.error("ninguna formación se puede completar: %s", mejor.faltan)
    return mejor


# --------------------------------------------------------------------------------
# Guardado en LaLiga
# --------------------------------------------------------------------------------

def a_payload(once: Once) -> dict:
    """Convierte el once al formato que espera la API.

    Los bloques se identifican por `playerTeamId` (el id de la ficha dentro de tu
    equipo), no por el id del jugador: son cosas distintas y confundirlas manda una
    alineación con gente que no es tuya.
    """
    def ids(js: list[Jugador]) -> list[str]:
        return [j.player_team_id for j in js if j.player_team_id]

    d, m, dl = once.formacion
    # Formato exacto que acepta la API; cualquier variación responde 500:
    #   · `goalkeeper` es un valor suelto, no una lista de uno.
    #   · el campo es `tactical_formation` en snake_case, no `tacticalFormation`.
    #   · `coach` y `bench` no van; mandarlos rompe la petición.
    return {
        "goalkeeper": once.portero.player_team_id if once.portero else None,
        "defender": ids(once.defensas),
        "midfield": ids(once.medios),
        "striker": ids(once.delanteros),
        "tactical_formation": [d, m, dl],
    }


def formaciones_viables(por_posicion: dict[int, int]) -> list[tuple[int, int, int]]:
    """Las formaciones que ese reparto de jugadores puede llenar. Posiciones 1=POR..4=DEL."""
    return [f for f in FORMACIONES
            if por_posicion.get(1, 0) >= 1
            and por_posicion.get(2, 0) >= f[0]
            and por_posicion.get(3, 0) >= f[1]
            and por_posicion.get(4, 0) >= f[2]]


def aguanta_una_baja(por_posicion: dict[int, int]) -> tuple[bool, str]:
    """Si perdiendo un jugador más en cualquier línea seguiría habiendo once legal.

    El margen es la pregunta, no el número de fichas: tener de sobra en una línea no sirve
    si la que va justa es otra. Por eso se comprueba línea por línea, incluida la portería,
    que es donde de verdad no hay margen: con un solo portero, su baja deja un once de
    diez y eso anula la jornada entera (tipo 7).
    """
    for pos in (1, 2, 3, 4):
        c = dict(por_posicion)
        c[pos] = c.get(pos, 0) - 1
        if not formaciones_viables(c):
            nombre = {1: "portero", 2: "defensa", 3: "medio", 4: "delantero"}[pos]
            return False, f"una baja más en {nombre} dejaría el equipo sin once legal"
    return True, "ok"


def margen_tras_vender(plantilla: dict, ptid_que_sale: str) -> tuple[bool, str]:
    """Si tras vender a ese jugador seguiría habiendo once legal con margen de una baja.

    Toma la respuesta cruda de la plantilla (`/leagues/{lg}/teams/{id}`) para que la use
    cualquiera de los caminos de venta sin pedir nada extra. Vive aquí y no en cada
    llamante a propósito: hay dos vías automáticas que cobran ofertas (el vigilante de
    venta y el job de ofertas) y una regla escrita dos veces se aplica de dos formas.
    """
    c: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0}
    for p in plantilla.get("players") or []:
        if str(p.get("playerTeamId")) == str(ptid_que_sale):
            continue
        pm = p.get("playerMaster") or {}
        # El `playerStatus` de la plantilla manda sobre el del maestro: cuando discrepan,
        # este es el que ve la app. Un lesionado o sancionado ocupa plaza y no cubre su
        # puesto, así que contarlo produce un once de diez con pinta de completo.
        if (p.get("playerStatus") or pm.get("playerStatus")) in (
                "injured", "suspended", "out_of_league"):
            continue
        try:
            pos = int(pm.get("positionId") or 0)
        except (TypeError, ValueError):
            continue
        c[pos] = c.get(pos, 0) + 1
    return aguanta_una_baja(c)


def valido(once: Once) -> tuple[bool, str]:
    """Comprobación previa: no se manda una alineación que consta incompleta.

    Un PUT con huecos puede dejar el equipo peor de como está, así que se prefiere
    no tocar nada y avisar.
    """
    if not once.completo:
        return False, f"faltan {', '.join(once.faltan)}"
    sin_id = [j.nombre for j in once.titulares if not j.player_team_id]
    if sin_id:
        return False, f"sin playerTeamId: {', '.join(sin_id)}"
    d, m, dl = once.formacion
    if (len(once.defensas), len(once.medios), len(once.delanteros)) != (d, m, dl):
        return False, "los bloques no cuadran con la formación"
    if not once.portero:
        return False, "sin portero"
    # LaLiga descarta en silencio a quien va en una línea que no es la suya, y el once
    # queda en diez.
    fuera = [j.nombre for pos, bloque in ((1, [once.portero]), (2, once.defensas),
                                          (3, once.medios), (4, once.delanteros))
             for j in bloque if j.posicion != pos]
    if fuera:
        return False, f"fuera de su línea: {', '.join(fuera)}"
    return True, "ok"
