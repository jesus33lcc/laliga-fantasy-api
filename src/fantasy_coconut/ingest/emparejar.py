"""Casar los nombres de futbolfantasy con los ids de LaLiga Fantasy.

Sin este puente hay una probabilidad de ser titular pero no se sabe de quién. Los formatos
no se parecen y no hay identificador común: LaLiga usa el apodo corto ("M. Garcia",
"Rodriguez") y futbolfantasy el nombre completo sin acentos ("kike salas", "unai simon").
El `slug` de LaLiga solo viene en la ficha individual, o sea una petición por jugador.

Cuatro reglas, de más a menos fiable, cada una comentada donde se aplica. El equipo
desempata cuando quedan varios candidatos, no es un requisito. Ante ambigüedad no se
empareja: atribuirle a un jugador la titularidad de otro es peor que no tener el dato.
"""

from __future__ import annotations

import collections
import logging
import re
import unicodedata
from dataclasses import dataclass

log = logging.getLogger("coconut.emparejar")

_LIMPIAR = re.compile(r"[^a-z0-9 ]+")


def normalizar(nombre: str) -> str:
    """Sin acentos, en minúsculas y sin iniciales sueltas ('M. Garcia' -> 'garcia')."""
    s = unicodedata.normalize("NFKD", nombre or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("-", " ").replace(".", " ")
    s = _LIMPIAR.sub(" ", s)
    tokens = [t for t in s.split() if len(t) > 1]  # fuera iniciales tipo "m"
    return " ".join(tokens)


def variantes_inicial_perdida(normalizado: str) -> list[str]:
    """Nombres candidatos si a un token le falta su primera letra acentuada.

    futbolfantasy genera su slug comiéndose la mayúscula acentuada: "Álex Baena" llega
    como "lex-baena" y "Julián Álvarez" como "julian-lvarez". Solo se prueban vocales, que
    son las que llevan tilde, y en orden estable para que el resultado sea reproducible.
    """
    tokens = normalizado.split()
    return [" ".join([*tokens[:i], v + t, *tokens[i + 1:]])
            for i, t in enumerate(tokens) for v in "aeiou"]


@dataclass
class Emparejamiento:
    player_id: int
    nombre_laliga: str
    nombre_ff: str
    metodo: str


def _mapa_equipos(jugadores_laliga: list[dict], jugadores_ff: list) -> dict[str, int]:
    """Deduce qué slug de futbolfantasy corresponde a cada `team_id` de LaLiga.

    Se resuelve por mayoría sobre los emparejamientos exactos, que son los únicos
    indiscutibles. Así no hay que mantener a mano una tabla de 20 equipos que además
    cambia con los ascensos y descensos.
    """
    porNombre: dict[str, list[dict]] = {}
    for p in jugadores_laliga:
        porNombre.setdefault(normalizar(p["name"]), []).append(p)

    votos: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for j in jugadores_ff:
        cands = porNombre.get(normalizar(j.nombre), [])
        if len(cands) == 1 and cands[0].get("team_id"):
            votos[j.equipo][cands[0]["team_id"]] += 1
    return {eq: c.most_common(1)[0][0] for eq, c in votos.items() if c}


def emparejar(jugadores_laliga: list[dict], jugadores_ff: list) -> tuple[
        list[Emparejamiento], list[str]]:
    """Devuelve (emparejados, nombres de futbolfantasy que se quedaron sin casar)."""
    # Índice de LaLiga por nombre normalizado.
    porNombre: dict[str, list[dict]] = {}
    for p in jugadores_laliga:
        porNombre.setdefault(normalizar(p["name"]), []).append(p)

    # El equipo es lo que desambigua a los "Vargas" y "Romero" de la liga.
    equipos = _mapa_equipos(jugadores_laliga, jugadores_ff)
    log.info("equipos deducidos: %d de %d", len(equipos),
             len({j.equipo for j in jugadores_ff}))

    salida: list[Emparejamiento] = []
    huerfanos: list[str] = []
    usados: set[int] = set()

    for j in jugadores_ff:
        largo = normalizar(j.nombre)
        if not largo:
            continue
        candidatos: list[tuple[dict, str]] = []

        # 1) idéntico
        for p in porNombre.get(largo, []):
            candidatos.append((p, "exacto"))

        if not candidatos:
            tokens_largo = set(largo.split())
            apellido = largo.split()[-1]
            for corto, ps in porNombre.items():
                if not corto:
                    continue
                tokens_corto = set(corto.split())
                # 2) el nombre corto es exactamente el apellido
                if corto == apellido:
                    candidatos += [(p, "apellido") for p in ps]
                # 3) todos los tokens del corto están en el largo
                elif tokens_corto and tokens_corto <= tokens_largo:
                    candidatos += [(p, "subconjunto") for p in ps]

        if not candidatos:
            # 4) le falta la inicial acentuada al slug de la fuente. La variante tiene que
            #    casar exacta con un nombre real, así que no puede inventar parejas.
            for variante in variantes_inicial_perdida(largo):
                candidatos += [(p, "inicial_perdida") for p in porNombre.get(variante, [])]

        libres = [(p, m) for p, m in candidatos if p["id"] not in usados]
        # El equipo desempata, no filtra: con un único candidato se acepta sin mirarlo,
        # y si el equipo no se pudo deducir tampoco entra en juego.
        tid = equipos.get(j.equipo)
        if len(libres) > 1 and tid is not None:
            mismo = [(p, m) for p, m in libres if p.get("team_id") == tid]
            if mismo:
                libres = mismo
        if len(libres) == 1:
            p, metodo = libres[0]
            usados.add(p["id"])
            salida.append(Emparejamiento(p["id"], p["name"], j.nombre, metodo))
        else:
            # 0 candidatos, o varios sin poder elegir: mejor no inventar.
            huerfanos.append(j.nombre)

    return salida, huerfanos
