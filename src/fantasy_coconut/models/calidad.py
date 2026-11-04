"""Señales de calidad deportiva: filtro duro más nota orientativa.

`evaluar()` devuelve un `Veredicto(apto, nota, motivo)`. La nota va de 0 a 1 y solo es
comparable entre jugadores de la misma posición. Decidir solo por precio detecta gangas
pero no distingue a un titular de un suplente ni descarta a un lesionado.

Tres variables, por orden de fiabilidad: el estado (`playerStatus`), que es filtro duro y
lo decide `ESTADOS_VETADOS`, una política del proyecto y no una regla del juego; los
puntos de la temporada pasada, que si faltan dejan el veredicto en no valorable en vez de
inventar una nota; y la probabilidad de ser titular. No mira minutos, xG/xA, dificultad de
calendario ni forma reciente.
"""

from __future__ import annotations

from dataclasses import dataclass

# Estados que vetan el fichaje. Es una política del proyecto, editable: ninguno de los
# tres impide comprar por API, lo que hacen es puntuar 0 mientras duren.
ESTADOS_VETADOS = {"injured", "suspended", "out_of_league"}
# Duda: no se veta, pero penaliza.
ESTADO_DUDA = "doubtful"

# Referencia: el techo observado en una temporada ronda los 200 puntos.
PUNTOS_REFERENCIA = 200.0

POSICIONES = {1: "portero", 2: "defensa", 3: "medio", 4: "delantero", 5: "entrenador"}


@dataclass
class Veredicto:
    apto: bool
    nota: float          # 0..1, solo comparable entre jugadores de la misma posición
    motivo: str

    def __bool__(self) -> bool:
        return self.apto


def evaluar(nombre: str, estado: str | None, puntos_temporada_pasada: int | None,
            posicion: int | None = None,
            prob_titular: float | None = None) -> Veredicto:
    """Aplica el filtro duro de estado y devuelve la nota. Sin datos devuelve no valorable.

    `prob_titular` es la que más mueve el resultado: multiplica la nota y además corta
    por debajo de 0,5. Ese corte se aplica a TODOS, incluido un `doubtful`: estar en
    duda penaliza la nota, no exime del filtro.
    """
    est = (estado or "").lower()

    if est in ESTADOS_VETADOS:
        return Veredicto(False, 0.0, f"estado '{est}': no se ficha")

    if puntos_temporada_pasada is None:
        # Sin histórico no hay base para la nota. Sale con la misma nota 0 que un
        # estado vetado; lo que distingue «sin datos» de «malo» es el `motivo`.
        return Veredicto(False, 0.0, "sin puntos de la temporada pasada: no valorable")

    nota = min(puntos_temporada_pasada / PUNTOS_REFERENCIA, 1.0)
    detalle = f"{puntos_temporada_pasada} pts la temporada pasada"

    # La duda penaliza y sigue. Devolver aquí se salta el corte de titularidad de abajo,
    # y entonces un dudoso que sale titular el 10 % de las jornadas queda apto mientras
    # un sano con el 40 % se rechaza: el estado pesaría más que la probabilidad de
    # jugar, que es al revés de lo que decide los puntos.
    if est == ESTADO_DUDA:
        nota *= 0.5
        detalle += ", EN DUDA (nota a la mitad)"

    if posicion in POSICIONES:
        detalle += f" · {POSICIONES[posicion]}"

    # La titularidad multiplica: un suplente rinde una fracción de lo que dice su
    # historial. Sin dato se deja la nota como está, que castigar a ciegas es peor.
    if prob_titular is not None:
        nota *= prob_titular
        detalle += f" · {int(prob_titular*100)}% titular"
        if prob_titular < 0.5:
            return Veredicto(False, nota, detalle + " poco probable que juegue")

    return Veredicto(True, nota, detalle)
