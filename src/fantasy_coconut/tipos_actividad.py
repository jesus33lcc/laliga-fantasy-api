"""Los 33 `activityTypeId` del tablón, con nombre y con su texto oficial.

Generado desde `GET {API}/v5/activity-types`, que devuelve cada tipo con su plantilla
de texto oficial.

Por qué importa tenerlos todos

Hay TRES formas de no puntuar una jornada, y solo una es evidente:

| | |
|---|---|
| 7 | alineación incompleta: un once de diez no puntúa |
| 8 | saldo negativo: quedarse en números rojos anula la jornada |
| 32 | alineación incorrecta |

Los tres son la confirmación oficial de LaLiga de que una jornada no ha contado, y llegan
de madrugada: antes de que a nadie le dé por mirar el marcador.

Que el saldo negativo anule la jornada le da una segunda razón a `reserva_intocable`:
no es solo para tener margen de maniobra, es que gastarse hasta el último euro puede
costar una jornada entera.

Y entra más dinero del que parece: los tipos 12 y 20-24 también son ingresos
("recompensa 11 ideal", "ajuste de venta", "actualización de puntos", "rectificación en
el cálculo", "otro ajuste"). El estimador de caja rival tiene que sumarlos o se queda
corto con cualquier manager activo.

Para rehacer la tabla cuando LaLiga añada un tipo, se vuelca la respuesta de ese mismo
endpoint: trae el `activityTypeId` y su texto en varios idiomas, así que no hay que
deducir de un evento suelto qué significa.
"""

# id nombre estable, el que se guarda en `league_events.kind`.
NOMBRES: dict[int, str] = {
    1: "clausulazo",
    2: "cesion",
    3: "fin_cesion",
    4: "blindaje",
    5: "denuncia",
    6: "recompensa",
    7: "no_puntua_once_incompleto",
    8: "no_puntua_saldo_negativo",
    9: "alta_manager",
    10: "baja_manager",
    11: "nuevo_admin",
    12: "recompensa_ideal",
    13: "premium_capitán",
    14: "premium_banquillo",
    15: "premium_formaciones",
    16: "premium_entrenador",
    17: "premium_11_ideal",
    18: "premium_cesiones",
    19: "premium_premium_activado",
    20: "otra_recompensa_20",
    21: "otra_recompensa_21",
    22: "otra_recompensa_22",
    23: "otra_recompensa_23",
    24: "otra_recompensa_24",
    25: "premium_capitán",
    26: "premium_banquillo",
    27: "premium_formaciones",
    28: "premium_entrenador",
    29: "premium_11_ideal",
    30: "premium_cesiones",
    31: "market_buy",
    32: "no_puntua_alineacion",
    33: "market_sale",
}

# id plantilla de texto oficial de LaLiga, en español.
TEXTOS: dict[int, str] = {
    1: "#user1Id# ha comprado al jugador #playerMasterId# de #user2Id# por #amount#€",
    2: "#user1Id# ha cedido al jugador #playerMasterId# a #user2Id# por #amount#€",
    3: "#playerMasterId# ha vuelto a la plantilla de #user1Id#.",
    4: "#user1Id# ha blindado a #playerMasterId#",
    5: "Después de una denuncia, el administrador de la liga ha decidido expulsar a un mánager.",
    6: "En la jornada #weekNumber#, #user1Id# ha ganado #amount#€",
    7: "En la jornada #weekNumber#, #user1Id# no ha puntuado debido a alineación incompleta.",
    8: "En la jornada #weekNumber#, #user1Id# no ha puntuado debido a saldo negativo.",
    9: "#user1Id# se ha unido a la liga.",
    10: "Un mánager ha dejado la liga",
    11: "El nuevo administrador de la liga es #user1Id#.",
    12: "#user1Id# ha ganado #amount#€ por tener jugadores en el 11 ideal de la jornada #weekNumber#.",
    13: "El administrador de la liga ha activado la funcionalidad Capitán.",
    14: "El administrador de la liga ha activado la funcionalidad Banquillo.",
    15: "El administrador de la liga ha activado la funcionalidad Formaciones.",
    16: "El administrador de la liga ha activado la funcionalidad Entrenador.",
    17: "El administrador de la liga ha activado la funcionalidad 11 ideal.",
    18: "El administrador de la liga ha activado la funcionalidad Cesiones.",
    19: "La liga ha sido convertida en una liga premium.",
    20: "#user1Id# ha ganado #amount#€ por Recompensa 11 ideal",
    21: "#user1Id# ha ganado #amount#€ por Ajuste de venta",
    22: "#user1Id# ha ganado #amount#€ por Actualización de puntos",
    23: "#user1Id# ha ganado #amount#€ por Rectificación en el cálculo",
    24: "#user1Id# ha ganado #amount#€ por Otro ajuste",
    25: "El administrador de la liga ha desactivado la funcionalidad Capitán.",
    26: "El administrador de la liga ha desactivado la funcionalidad Banquillo.",
    27: "El administrador de la liga ha desactivado la funcionalidad Formaciones.",
    28: "El administrador de la liga ha desactivado la funcionalidad Entrenador.",
    29: "El administrador de la liga ha desactivado la funcionalidad 11 ideal.",
    30: "El administrador de la liga ha desactivado la funcionalidad Cesiones.",
    31: "#user1Id# ha comprado al jugador #playerMasterId# de LALIGA por #amount#€",
    32: "En la jornada #weekNumber#, #user1Id# no ha puntuado debido a alineación incorrecta.",
    33: "#user1Id# ha vendido al jugador #playerMasterId# a LALIGA por #amount#€",
}

# Los que ANULAN la jornada. Si aparece uno con tu manager_id, es una emergencia:
# significa que esa jornada cuenta 0 pase lo que pase.
ANULAN_JORNADA = {7, 8, 32}

# Los que MUEVEN DINERO y hay que contar para estimar la caja rival.
INGRESOS = {6, 12, 20, 21, 22, 23, 24, 33}
GASTOS = {1, 31}


def nombre(tipo: int) -> str:
    """Nombre estable del tipo. Uno desconocido se marca para que se note."""
    return NOMBRES.get(tipo, f"tipo_{tipo}")
