"""Qué hace cada proceso del worker, para qué sirve y qué se puede tocar.

Por qué existe este fichero

Un nombre (`ofertas`) y un botón Lanzar no bastan para decidir nada: no dicen qué va
a pasar al pulsarlo, ni por qué existe el proceso, ni cada cuánto corre, ni si
se puede apagar.

Aquí vive esa información, en un solo sitio, junto a lo que se puede configurar de
cada uno. Añadir un ajuste nuevo es añadir una entrada a `opciones`: la pantalla lo pinta
sola y el scheduler lo aplica solo.

Cómo se resuelve la configuración

El `defecto` de aquí, pisado por lo guardado. Un proceso que no se ha tocado nunca usa
el valor de este fichero; en cuanto se guarda algo, manda lo guardado. Resolverlo así (y
no escribiendo el defecto en la tabla) permite cambiar el valor por defecto de un proceso
sin pisar lo que ya se haya elegido a mano.

Ningún proceso se puede apagar "a medias". Los que sostienen la serie de precios o
la alineación llevan `critico: True`: se pueden reprogramar, pero apagarlos avisa de lo
que cuesta, porque una toma de precios perdida no se recupera y un once sin guardar
son 0 puntos.

El que falta a propósito

`job_reprogramar` corre en el worker y NO está aquí, así que la pantalla no lo enseña y
nadie puede apagarlo. Es justamente el que relee esta configuración cada 10 min: con un
interruptor, apagarlo dejaría el sistema sin forma de volver a encender nada ni de
aplicar ningún horario, y para arreglarlo habría que recrear el contenedor.

Quien compare este catálogo con los `job_*` del scheduler va a encontrar esa diferencia.
Es la única, y es deliberada.
"""

from __future__ import annotations

from typing import Any

# Tipos de opción que la interfaz sabe pintar. Añadir uno nuevo obliga a tocar la web;
# con estos cuatro se cubre todo lo que hay hoy.
NUMERO, HORAS, INTERVALO, BOOL = "numero", "horas", "intervalo", "bool"


def _op(clave: str, tipo: str, titulo: str, ayuda: str, **extra: Any) -> dict:
    return {"clave": clave, "tipo": tipo, "titulo": titulo, "ayuda": ayuda, **extra}


CATALOGO: dict[str, dict[str, Any]] = {
    "players": {
        "titulo": "Maestro de jugadores y precios",
        "que_hace": "Descarga la lista completa de jugadores de LaLiga con su valor de "
                    "mercado y guarda una foto del precio de cada uno.",
        "para_que": "Es la que construye la serie histórica de precios, que es el activo "
                    "irrecuperable del proyecto: si un día no se toma, ese día se pierde "
                    "para siempre. Sobre ella se calcula el momentum, que es la única "
                    "señal de precio que el backtest ha validado.",
        "coste": "1 petición por toma. El endpoint es público y cachea 8 h.",
        "critico": True,
        "defecto": {"activo": True, "horas": ["00:20", "08:30", "14:30", "20:30"]},
        "opciones": [
            _op("horas", HORAS, "A qué horas toma la foto",
                "El valor se recalcula a las 00:15, así que la primera va justo después. "
                "Más tomas al día no dan más información: el precio solo cambia una vez."),
        ],
    },
    "league": {
        "titulo": "Liga, plantillas, mercado y tablón",
        "que_hace": "Lee la clasificación, las cuatro plantillas con sus cláusulas y "
                    "blindajes, el mercado del día y el tablón de actividad. Luego pasa "
                    "el vigilante, que compara con la foto anterior.",
        "para_que": "Es de donde sale casi todo lo que ves: quién tiene qué, a quién "
                    "puedes atacar, quién te puede robar y cuánta caja tiene cada uno. "
                    "El vigilante que va detrás es el que detecta un clausulazo contra ti "
                    "y dispara la reparación del once.",
        "coste": "Unas 15 por ciclo: el agregado, una plantilla por manager, el saldo, una por página del tablón, el mercado y tres del vigilante. Varias salen de caché.",
        "critico": True,
        "defecto": {"activo": True, "cada_minutos": 30, "minuto": 25},
        "opciones": [
            _op("cada_minutos", NUMERO, "Cada cuántos minutos",
                "Manda sobre las horas si está puesto. Es lo que decide **cuánto puede "
                "envejecer la caja de un rival antes de decidir sobre ella**: se ha visto "
                "pasar de 7M a 84M en una sola operación, y con una foto vieja se decide "
                "un clausulazo sobre dinero que ya no existe. 0 = usar las horas.",
                min=0, max=59),
            _op("cada_horas", NUMERO, "O cada cuántas horas",
                "Solo se usa si los minutos están a 0.", min=1, max=12),
            _op("minuto", NUMERO, "En el minuto", "Para no chocar con los demás procesos. "
                "Se ignora si va por minutos.", min=0, max=59),
        ],
    },
    "alineacion": {
        "titulo": "Guardar la alineación",
        "que_hace": "Calcula el mejor once con los jugadores disponibles y lo guarda "
                    "contra la API. Respeta el candado y el once fijado a mano.",
        "para_que": "El once se CONGELA cuando abre la jornada: lo que no esté guardado a "
                    "esa hora no puntúa. Y un once incompleto anula la jornada entera.",
        "coste": "1-2 peticiones.",
        "critico": True,
        "defecto": {"activo": True, "horas": ["10:15", "18:15", "20:15"]},
        "opciones": [
            _op("horas", HORAS, "A qué horas recalcula",
                "Esto solo reparte el día. La red de seguridad real es la guardia, que "
                "mira cuándo abre la jornada de verdad."),
        ],
    },
    "alineacion_guardia": {
        "titulo": "Guardia del once",
        "que_hace": "Cada hora comprueba dos cosas: si falta poco para que abra la "
                    "jornada (fuerza una pasada) y si el once tiene huecos porque te han "
                    "quitado a alguien (lo repara solo).",
        "para_que": "Es la red que evita el peor caso: un clausulazo contra un titular "
                    "deja el once en 10 y la jornada entera a cero. Se salta el candado a "
                    "propósito, porque una reparación no es un recálculo.",
        "coste": "~2 peticiones/hora, casi todas de caché.",
        "critico": True,
        "defecto": {"activo": True, "minuto": 45, "aviso_minutos": 75},
        "opciones": [
            _op("minuto", NUMERO, "En el minuto de cada hora", "", min=0, max=59),
            _op("aviso_minutos", NUMERO, "Fuerza una pasada si faltan menos de (min)",
                "Cuántos minutos antes de que abra la jornada se guarda el once sí o sí.",
                min=15, max=240),
        ],
    },
    "ofertas": {
        "titulo": "Ofertas recibidas",
        "que_hace": "Mira si alguien (el sistema o un rival) ha ofertado por alguno de "
                    "tus jugadores listados, y avisa.",
        "para_que": "Listar no es vender: el sistema manda su oferta cuando quiere y "
                    "caduca en minutos. Sin esto hay que acordarse de mirar.",
        "coste": "1 petición por jugador listado.",
        "critico": False,
        "defecto": {"activo": True, "minutos": [20, 50], "solo_avisar": False},
        "opciones": [
            _op("minutos", INTERVALO, "En qué minutos de cada hora",
                "Separados por comas. Más veces no acelera la oferta, solo gasta cuota."),
            _op("solo_avisar", BOOL, "Avisar, pero no cobrar",
                "Con esto encendido nadie vende solo: las ofertas llegan como aviso y las "
                "apruebas tú. Cuesta ventas y a cambio decides el "
                "precio: esperar a una oferta por encima del valor rinde +6,4 puntos "
                "porcentuales frente a aceptar la primera."),
        ],
    },
    "radar": {
        "titulo": "Radar de oportunidades y peligros",
        "que_hace": "Tres detectores: jugadores tuyos que salen de protección, chollos en "
                    "el mercado y lastre (lesionados o suplentes fijos que ocupan plaza).",
        "para_que": "El de protección es el más valioso: es el único momento predecible "
                    "en que eres vulnerable, y da tiempo a blindar antes: que es gratis.",
        "coste": "Lee de la base de datos, no gasta peticiones.",
        "critico": False,
        "defecto": {"activo": True, "cada_horas": 2, "minuto": 50},
        "opciones": [
            _op("cada_horas", NUMERO, "Cada cuántas horas", "", min=1, max=12),
            _op("minuto", NUMERO, "En el minuto", "", min=0, max=59),
        ],
    },
    "centinela": {
        "titulo": "Centinela de clausulazos",
        "que_hace": "Mira la primera página del tablón buscando clausulazos contra ti. "
                    "Si encuentra uno, avisa como emergencia y manda reparar el once.",
        "para_que": "Entre dos pasadas del proceso «liga» la alineación puede guardarse "
                    "con el jugador robado dentro: LaLiga lo descarta y el once se queda "
                    "en 10. Esto lo detecta en minutos y cuesta mucho menos que acelerar "
                    "\"liga\", que son unas 15 peticiones por ciclo.",
        "coste": "1 petición por pasada, y SOLO cuando se puede clausular. Mientras la "
                 "ventana esté cerrada (desde 24 h antes de cada jornada hasta que "
                 "termina) no gasta ninguna.",
        "critico": False,
        "defecto": {"activo": True, "minutos": [0, 10, 20, 30, 40, 50]},
        "opciones": [
            _op("minutos", INTERVALO, "En qué minutos de cada hora",
                "Separados por comas. Cada uno es 1 petición, pero solo si se puede "
                "clausular en ese momento; si no, no gasta nada."),
        ],
    },
    "emergencia": {
        "titulo": "Huecos críticos de plantilla",
        "que_hace": "Cuenta cuántos jugadores útiles tienes por posición y avisa si no "
                    "llegas al mínimo de ninguna formación.",
        "para_que": "Un lesionado o sancionado ocupa plaza pero NO cuenta como cobertura. "
                    "Sin esto te enteras al intentar alinear, que suele ser tarde.",
        "coste": "Lee de la base de datos.",
        "critico": False,
        "defecto": {"activo": True, "minutos": [5, 35]},
        "opciones": [
            _op("minutos", INTERVALO, "En qué minutos de cada hora", "Separados por comas."),
        ],
    },
    "onces": {
        "titulo": "Onces probables",
        "que_hace": "Rasca futbolfantasy y guarda la probabilidad de que cada jugador "
                    "salga titular en la próxima jornada.",
        "para_que": "Es el prior del modelo de alineación: un jugador bueno que no juega "
                    "vale 0. Acierta alrededor del 86 %.",
        "coste": "20 peticiones a futbolfantasy (una por equipo), con pausa entre ellas. "
                 "Tarda ~40 s.",
        "critico": False,
        "defecto": {"activo": True, "horas": ["09:40", "19:40"]},
        "opciones": [_op("horas", HORAS, "A qué horas", "")],
    },
    "salud": {
        "titulo": "Autodiagnóstico",
        "que_hace": "Comprueba los invariantes de los datos: precios huérfanos, un "
                    "jugador en dos plantillas, deuda por encima del 20 % del equipo, "
                    "protecciones imposibles, managers fuera del último snapshot…",
        "para_que": "Un dato corrupto no da error, da respuestas falsas. Esto lo caza "
                    "antes de que una decisión se tome con él.",
        "coste": "Lee de la base de datos.",
        "critico": False,
        "defecto": {"activo": True, "horas": ["07:05", "15:05", "23:05"]},
        "opciones": [_op("horas", HORAS, "A qué horas", "")],
    },
    "puntos": {
        "titulo": "Puntos por jornada de las plantillas",
        "que_hace": "Pide la ficha individual de cada jugador en plantilla y guarda sus "
                    "puntos jornada a jornada.",
        "para_que": "El maestro público, que es de donde sale todo lo demás, NO trae los "
                    "puntos de todo el mundo, y no avisa de que le faltan: puede dar 0 "
                    "puntos de un titular que lleva 13. Como el once mezcla el histórico "
                    "con el ritmo de esta temporada, ese hueco manda al banquillo a quien "
                    "no debería. Y se retroalimenta: a quien se sienta por no tener datos, "
                    "nunca se le piden los datos.",
        "coste": "Una petición por jugador, unas 55 con las cuatro plantillas. La ficha "
                 "trae TODAS sus jornadas de golpe, así que no se repite por jornada.",
        "critico": False,
        "defecto": {"activo": True, "horas": ["04:10", "16:10"], "solo_mios": False},
        "opciones": [
            _op("horas", HORAS, "A qué horas los actualiza",
                "Dos veces al día a propósito: una jornada solo se guarda cuando ha "
                "cerrado, y el once de la siguiente se decide pocas horas después. Con "
                "una sola pasada puede caer justo en medio y decidir con una jornada de "
                "menos."),
            _op("solo_mios", BOOL, "Solo mi plantilla",
                "Apagado recorre las cuatro. Los puntos de un rival deciden si su cláusula "
                "sale a cuenta, así que tenerlos mal cuesta lo mismo que los propios."),
        ],
    },
    "recompensa": {
        "titulo": "Recompensa diaria",
        "que_hace": "Cobra los 100.000 € que LaLiga regala cada día por ver un anuncio.",
        "para_que": "Es dinero que la liga regala cada día y que solo se pierde si nadie "
                    "lo pide. Corre dos veces al día porque una toma puede fallar; la "
                    "segunda no cuesta nada si ya se cobró.",
        "coste": "2 peticiones. Ingresa dinero, no lo gasta.",
        "critico": False,
        "defecto": {"activo": True, "horas": ["09:07", "21:07"]},
        "opciones": [_op("horas", HORAS, "A qué horas lo intenta", "")],
    },
    "trading": {
        "titulo": "Trading: comprar para vender",
        "que_hace": "Mira los agentes libres nuevos y marca los que vienen SUBIENDO de "
                    "precio, que son los únicos que suelen seguir subiendo. Avisa también "
                    "cuando toca vender lo que ya se compró.",
        "para_que": "El precio se recalcula cada día a las 00:15 con la demanda global, y "
                    "tiene inercia: lo que subió ayer sube hoy el 77,6 % de las veces. "
                    "PROPONE, no compra: solo el 10,6 % de lo que sale al mercado está en "
                    "racha, y el retorno real está medido sobre 6 casos.",
        "coste": "3 peticiones (mercado, saldo y plantilla), todas por el cliente compartido.",
        "critico": False,
        "defecto": {"activo": True, "horas": ["13:52"]},
        "opciones": [
            _op("horas", HORAS, "A qué horas revisa",
                "El mercado se renueva a las 13:48. Va tras `mercado_avisos` para no "
                "pedir el mercado dos veces a la vez."),
        ],
    },
    "mercado_avisos": {
        "titulo": "Mis búsquedas del mercado",
        "que_hace": "Revisa las búsquedas que hayas guardado contra la baraja nueva y "
                    "avisa si aparece algo que cumple tus filtros.",
        "para_que": "El mercado se renueva a las 13:48 y las fichas duran un día. Sin "
                    "esto hay que mirarlo a mano cada día.",
        "coste": "1 petición.",
        "critico": False,
        "defecto": {"activo": True, "horas": ["13:50"]},
        "opciones": [
            _op("horas", HORAS, "A qué horas revisa",
                "El mercado se renueva a las 13:48: antes de esa hora no hay nada nuevo."),
        ],
    },
    "precios_oficiales": {
        "titulo": "Histórico oficial de precios",
        "que_hace": "Trae el histórico de precios que LaLiga guarda de cada jugador, "
                    "desde junio. Va por lotes pequeños y priorizando: primero los tuyos, "
                    "luego los del mercado, luego los rivales.",
        "para_que": "Da más recorrido que la serie propia. Cuanto más histórico, mejor "
                    "se puede medir el momentum.",
        "coste": "1 petición POR JUGADOR. Traerlos todos serían unas 800, treinta veces "
                 "el consumo diario normal. Por eso va por lotes.",
        "critico": False,
        "defecto": {"activo": True, "horas": ["04:40"], "lote": 20},
        "opciones": [
            _op("horas", HORAS, "A qué horas", ""),
            _op("lote", NUMERO, "Cuántos jugadores por pasada",
                "Cada uno es una petición. Con 20 al día se cubren los ~100 que importan "
                "en cinco días. Subirlo mucho acerca el baneo.", min=5, max=60),
        ],
    },
    "resumen_diario": {
        "titulo": "Resumen diario",
        "que_hace": "Compone un mensaje con todo lo que hay que atender y lo manda por "
                    "Telegram. Si no hay nada por encima del umbral, no manda nada.",
        "para_que": "Un mensaje al día en vez de goteo. El goteo hace que dejes de leer "
                    "los avisos buenos.",
        "coste": "1 llamada interna + 1 a Telegram.",
        "critico": False,
        "defecto": {"activo": True, "horas": ["09:00"]},
        "opciones": [_op("horas", HORAS, "A qué hora", "")],
    },
    "respaldo": {
        "titulo": "Copia de seguridad",
        "que_hace": "Exporta tu configuración, tus tareas y tu historial y los sube a tu "
                    "repositorio privado de GitHub, en un solo commit y solo si algo ha "
                    "cambiado desde la última vez.",
        "para_que": "Que no se pierda lo que no se puede recuperar de ninguna otra parte. "
                    "Al ir a un repositorio te llevas el historial: se puede volver a la "
                    "copia de anteayer, no solo a la última.",
        "coste": "Ninguna petición a LaLiga. Unas pocas a GitHub, y solo de los ficheros "
                 "que hayan cambiado.",
        "critico": False,
        # Apagado por defecto: hasta que no hay repositorio y token no puede funcionar, y
        # un trabajo que falla cada noche enseña a ignorar los fallos.
        "defecto": {"activo": False, "horas": ["04:20"]},
        "opciones": [_op("horas", HORAS, "A qué hora", "")],
    },
}


def defecto(nombre: str) -> dict[str, Any]:
    return dict((CATALOGO.get(nombre) or {}).get("defecto") or {"activo": True})


def publico() -> dict[str, Any]:
    """El catálogo tal como lo consume la interfaz, sin el `defecto` duplicado."""
    return {n: {k: v for k, v in d.items() if k != "defecto"} for n, d in CATALOGO.items()}
