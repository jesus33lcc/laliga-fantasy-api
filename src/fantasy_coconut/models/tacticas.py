"""Catálogo de tácticas de mercado, cada una con su ventaja medida y su muestra.

Qué es esto y qué no es

Un catálogo implementado para que las tácticas se puedan ejecutar, no porque haya que
ejecutarlas. Cada una declara su ventaja medida, su coste y qué la falsaría.

Cada función devuelve el *porqué* junto al *qué*, y los descartes con su motivo: una
lista de candidatos sin los descartes obliga a repetir el análisis a mano.

Nada de aquí ejecuta. Todas devuelven propuestas; mover dinero sigue pasando por
`/accion/*`, con sus frenos y su confirmación.

Las tácticas

| | | ventaja medida |
|---|---|---|
| `escaparate()` | listar y esperar a la oferta buena del sistema | +6,4 pp por venta (n=8) |
| `clausulas_baratas()` | subir cláusula donde sale barato proteger | coste vs puntos, sin medir |
| `radiografia_rivales()` | qué táctica usa cada rival, deducida de sus datos | sin medir |
"""

from __future__ import annotations

import logging
import statistics as st
from typing import Any

log = logging.getLogger("coconut.models.tacticas")

# ---------- la distribución de lo que paga el sistema ----------
#
# El reglamento dice "aleatorio entre el valor de mercado y ±10 %", y dos guías de la
# comunidad lo repiten con la misma frase. Las 8 ofertas medidas encajan dentro:
# −5,4 −4,5 −2,9 −2,6 +0,7 +6,1 +7,0 +9,0.
#
# n=8. Los porcentajes de abajo son la mejor estimación que hay, no una ley. Se
# recalculan solos en `escaparate()` con lo que haya en `market/history`, así que la
# muestra crece sola; estas constantes son solo el respaldo cuando no hay datos.
PRIMA_SI_ACEPTAS_TODO = 0.93       # %: media de las 8
PRIMA_SI_ESPERAS = 7.36            # %: media de las que salieron por encima de +1 %
PROB_OFERTA_BUENA = 0.375          # 3 de 8 ciclos
UMBRAL_ACEPTAR = 1.0               # % sobre el valor a partir del cual compensa aceptar

# Lo que pierde al día un jugador cualquiera, que es el coste de esperar una oferta mejor.
# Sale de la 24/25 completa (197.041 observaciones, 337 días), no de los 34 días de agosto
# que daban −0,445 %: esa muestra cae en el arranque de liga y exagera la caída casi
# cuatro veces. Con la cifra alta, esperar tres días parece costar 1,35 % en vez de 0,35 %,
# y eso desaconseja esperar en un escenario donde esperar gana +6,42 pp.
CAIDA_DIARIA_PCT = 0.115

# Mínimos para poder alinear: ninguna de las 7 formaciones baja de aquí.
MINIMOS = {1: 1, 2: 3, 3: 3, 4: 1}


def _coma(n: float) -> str:
    """Un número para leer en castellano: 0.115 se escribe 0,115."""
    return f"{n:g}".replace(".", ",")


def _pct(nuevo: float, base: float) -> float | None:
    return round((nuevo - base) / base * 100, 2) if base else None


def distribucion_ofertas(historico: list[dict],
                         umbral_pct: float = UMBRAL_ACEPTAR) -> dict:
    """Qué paga de verdad el sistema, recalculado con el histórico real de la liga.

    `umbral_pct` es la prima a partir de la cual compensa aceptar. Decide qué ofertas
    cuentan como buenas, así que cambia `prima_esperando`, `prob_oferta_buena` y
    `ventaja_pp`: pedir otro umbral y recibir las cifras del 1 % daba una ganancia
    esperada que no corresponde a lo preguntado.

    Se excluyen las ventas inmediatas, que pagan el 50 % fijo y no son ofertas:
    una sola de ellas arrastra la media de +0,93 % a −4,72 % y da la impresión de que
    vender al sistema es ruinoso. Se detectan por su prima, que ronda el −50 % exacto.
    """
    primas = [o["sobre_valor_pct"] for o in historico
              if o.get("operacion") == "sale" and o.get("sobre_valor_pct") is not None
              and o["sobre_valor_pct"] > -40]
    if len(primas) < 3:
        return {"n": len(primas), "fiable": False,
                "prima_aceptando_todo": PRIMA_SI_ACEPTAS_TODO,
                "prima_esperando": PRIMA_SI_ESPERAS,
                "prob_oferta_buena": PROB_OFERTA_BUENA,
                "nota": "muestra insuficiente: se usan los valores de referencia"}
    buenas = [p for p in primas if p >= umbral_pct]
    return {
        "n": len(primas),
        "fiable": len(primas) >= 30,
        "min": round(min(primas), 2), "max": round(max(primas), 2),
        "mediana": round(st.median(primas), 2),
        "prima_aceptando_todo": round(st.mean(primas), 2),
        "prima_esperando": round(st.mean(buenas), 2) if buenas else None,
        "prob_oferta_buena": round(len(buenas) / len(primas), 3),
        "ventaja_pp": (round(st.mean(buenas) - st.mean(primas), 2) if buenas else None),
        "nota": ("con n<30 esto ordena, no mide: sirve para decidir a quién listar, "
                 "no para prometer un retorno"),
    }



#  T1 · ESCAPARATE: listar y esperar la oferta buena


def escaparate(plantilla: list[dict], mercado: list[dict], historico: list[dict],
               *, mi_team_id: str, titulares: set[str] | None = None,
               umbral_pct: float = UMBRAL_ACEPTAR) -> dict:
    """Qué jugadores conviene tener listados de forma permanente.

    Recibe la plantilla, el mercado crudo y el histórico de ventas de la liga. Devuelve
    los candidatos ordenados (`out_of_league` > lastre > banquillo) con la ganancia
    esperada de esperar, los descartados con su motivo y el umbral de aceptación.

    `umbral_pct` llega hasta el cálculo: `distribucion_ofertas()` lo usa para separar
    las ofertas buenas, así que las cifras que salen son las del umbral pedido.
    No ejecuta nada.

    El mecanismo

    LaLiga hace ofertas automáticas por lo que tengas en venta, aleatorias entre el
    valor y ±10 %, y la oferta caduca en el siguiente ciclo de mercado: cada día es una
    tirada nueva. Como se puede rechazar, listar no es vender: es una opción que solo
    se ejerce cuando la tirada sale por encima del umbral.

    Medido sobre el histórico propio (n=8 ofertas): aceptar la primera oferta da
    +0,93 %; aceptar solo las que superan el umbral da +7,36 %, y esas salen en
    3 de los 8 ciclos.

    Se reconoce a quien la juega porque lista casi toda su plantilla, y a menudo con
    el precio de salida *por debajo* del valor. Eso último no regala nada: el precio de
    salida no influye en lo que ofrece el sistema, y una oferta de rival tiene que ser
    ≥ valor de mercado igual. Solo hace la ficha más visible.

    Lo que cuesta

    - Revela intención. Una ficha en venta es información pública: listar al portero
      anuncia que sobra un portero.
    - Un rival puede ofertarte. Aceptarle le entrega un jugador útil. El vigilante
      `venta` solo acepta ofertas del sistema.
    - No se listan titulares. Con el candado puesto la venta se rechaza con 423; la
      comprobación se repite aquí para no proponer algo que la API va a rechazar.
    """
    titulares = titulares or set()
    dist = distribucion_ofertas(historico, umbral_pct)

    # El cruce tiene que cuadrar, y si no cuadra hay que decirlo. Un filtro que no
    # encuentra nada que filtrar es indistinguible de uno roto: si los titulares no casan
    # con la plantilla, esta táctica propondría listar el once entero sin dar un error y
    # con una lista perfectamente plausible.
    ids_plantilla = {str(p.get("playerTeamId") or "") for p in plantilla}
    casan = len(titulares & ids_plantilla)
    aviso_cruce = None
    if titulares and casan == 0:
        aviso_cruce = (" el once no casa con la plantilla: NO se han podido filtrar los "
                       "titulares, así que esta lista puede incluirlos. No ejecutar.")
        log.error("escaparate: 0 de %d titulares casan con la plantilla", len(titulares))
    elif titulares and casan < len(titulares):
        aviso_cruce = (f"solo {casan} de {len(titulares)} titulares casan con la "
                       "plantilla; revisa antes de listar")
    ya_listados = {str((f.get("playerTeam") or {}).get("playerTeamId"))
                   for f in mercado
                   if str((f.get("sellerTeam") or {}).get("id") or "") == str(mi_team_id)}

    # Cuántos hay por posición: no se puede listar a quien te deja sin cubrir el mínimo.
    por_pos: dict[int, int] = {}
    for p in plantilla:
        pm = p.get("playerMaster") or {}
        if str(pm.get("playerStatus")) == "ok":
            por_pos[int(pm.get("positionId") or 0)] = por_pos.get(int(pm.get("positionId") or 0), 0) + 1

    candidatos, descartados = [], []
    for p in plantilla:
        pm = p.get("playerMaster") or {}
        # La ficha de plantilla no tiene `id`: el identificador es `playerTeamId`. Con
        # `p["id"]` esto sale siempre vacío y el filtro de titulares deja de filtrar.
        ptid = str(p.get("playerTeamId") or "")
        valor = int(pm.get("marketValue") or 0)
        pos = int(pm.get("positionId") or 0)
        estado = str(pm.get("playerStatus"))
        ficha = {"player_team_id": ptid, "nombre": pm.get("nickname"),
                 "posicion": pos, "valor": valor, "estado": estado,
                 "puntos": int(pm.get("points") or 0)}

        if ptid in ya_listados:
            descartados.append({**ficha, "motivo": "ya está listado"})
            continue
        if ptid in titulares:
            descartados.append({**ficha, "motivo": "es titular del once"})
            continue
        if estado == "ok" and por_pos.get(pos, 0) <= MINIMOS.get(pos, 0):
            descartados.append({**ficha, "motivo":
                                f"te dejaría en {por_pos.get(pos)} de esa posición, "
                                f"el mínimo para alinear es {MINIMOS.get(pos)}"})
            continue

        # `out_of_league` es el caso más claro: su valor deja de recalcularse, así que
        # esperar no tiene ningún coste de oportunidad. No va a subir.
        prioridad = ("out_of_league" if estado == "out_of_league" else
                     "lastre" if estado in ("injured", "suspended") else "banquillo")
        candidatos.append({
            **ficha, "prioridad": prioridad,
            "ganancia_esperada_si_esperas": (
                int(valor * (dist["prima_esperando"] or 0) / 100)
                if dist.get("prima_esperando") else None),
            "vs_aceptar_la_primera": (
                int(valor * (dist.get("ventaja_pp") or 0) / 100)
                if dist.get("ventaja_pp") else None),
        })

    orden = {"out_of_league": 0, "lastre": 1, "banquillo": 2}
    candidatos.sort(key=lambda c: (orden[c["prioridad"]], -(c["valor"] or 0)))
    return {
        "tactica": "escaparate",
        "resumen": ("Lista jugadores de forma permanente y acepta SOLO las ofertas del "
                    f"sistema por encima de +{umbral_pct:g} %. Cada ciclo de mercado es "
                    "una tirada nueva entre −10 % y +10 %, y rechazar es gratis."),
        "umbral_aceptar_pct": umbral_pct,
        "distribucion": dist,
        "ya_listados": len(ya_listados),
        "titulares_excluidos": casan,
        "aviso": aviso_cruce,
        "candidatos": candidatos,
        "descartados": descartados,
        "riesgos": [
            "Listar revela que quieres vender: un rival que sepa dónde te sobra puede "
            "deducir dónde te falta.",
            "Una oferta de un rival no la acepta el vigilante: le entregarías un "
            "jugador útil a un rival directo.",
            f"Esperar tiene un coste: el jugador medio pierde ~{_coma(CAIDA_DIARIA_PCT)} % "
            f"de valor al día, así que una espera muy larga se come la prima.",
        ],
        "que_lo_falsaria": ("Que al crecer la muestra la prima media de las ofertas "
                            "aceptadas por encima del umbral se acerque a la de aceptar "
                            "todas. Entonces esperar no aporta y esto se apaga."),
    }



#  T2 · CLÁUSULAS BARATAS: proteger donde sale rentable


def clausulas_baratas(plantilla: list[dict], caja_rival_max: int,
                      *, objetivo_ratio: float = 1.6,
                      modo: str = "disuasorio") -> dict:
    """Dónde sale barato subir la cláusula, con el importe exacto y el coste de cada subida.

    Recibe la plantilla y la caja del rival más rico. Devuelve las propuestas ordenadas
    por coste por punto protegido, cada una con `subir_hasta`, `value_to_increase`,
    `coste` y `factor`, más los descartados con su motivo. No ejecuta nada.

    El matiz que la hace distinta de "sube cláusulas"

    La guía de referencia de la comunidad recomienda lo contrario de lo que parece
    intuitivo: no subir ninguna cláusula salvo a jugadores de valor bajo. El motivo es
    aritmético: con `factor=2` la subida cuesta la mitad de lo que sube, así que
    proteger a un jugador de 2M cuesta poco y proteger a uno de 70M cuesta una plantilla.
    Lo que se compra es el *diferencial* entre su cláusula y la caja del rival más rico, y
    ese diferencial vale lo mismo protegiendo a un barato que a un caro.

    Los dos filtros que cambian la respuesta entera

    Son los mismos que aplica el blindaje, y por los mismos motivos:

    1. Quien está fuera del alcance de todos ya está protegido. Para clausular no se
       puede uno endeudar (lo dice el reglamento; `030.01.32` es otra cosa, el margen de
       deuda del 20 % en las PUJAS), así que la vara es la caja limpia del rival más
       rico, no su margen de deuda. Subirle la cláusula a quien nadie puede pagar es
       gastar por nada.
    2. Un `out_of_league` no se protege. Si un rival paga su cláusula, te hace un favor.

    Dos objetivos que no son el mismo, y mezclarlos da respuestas absurdas

    | `modo` | objetivo | cuándo |
    |---|---|---|
    | `disuasorio` *(por defecto)* | `valor × objetivo_ratio` | lo que recomiendan las guías: encarecer el robo en proporción a lo que vale. Barato justo en los de valor bajo |
    | `inalcanzable` | `caja del rival más rico + 1` | ponerlo fuera de alcance del todo. El coste es el mismo en euros para todos, así que es carísimo en los baratos y razonable en los que ya tienen cláusula alta |

    Los dos modos no se mezclan: un `max()` de ambos sube la cláusula de un jugador de
    1,91M hasta 28,63M, siete veces su valor, bajo el título "cláusulas baratas".
    """
    if modo not in ("disuasorio", "inalcanzable"):
        raise ValueError("modo debe ser 'disuasorio' o 'inalcanzable'")
    propuestas, descartados = [], []
    for p in plantilla:
        pm = p.get("playerMaster") or {}
        valor = int(pm.get("marketValue") or 0)
        clausula = int(p.get("buyoutClause") or 0)
        estado = str(pm.get("playerStatus"))
        ficha = {"player_team_id": str(p.get("playerTeamId") or ""),
                 "nombre": pm.get("nickname"),
                 "valor": valor, "clausula": clausula, "puntos": int(pm.get("points") or 0),
                 "ratio": round(clausula / valor, 2) if valor else None}

        if estado == "out_of_league":
            descartados.append({**ficha, "motivo": "out_of_league: que se lo lleven"})
            continue
        if clausula > caja_rival_max:
            descartados.append({**ficha, "motivo":
                                f"ya inalcanzable: su cláusula ({clausula/1e6:.2f}M) pasa "
                                f"de la caja del rival más rico ({caja_rival_max/1e6:.2f}M)"})
            continue
        if p.get("isShielded"):
            descartados.append({**ficha, "motivo": "blindado ahora mismo"})
            continue

        objetivo = (int(valor * objetivo_ratio) if modo == "disuasorio"
                    else caja_rival_max + 1)
        subida = objetivo - clausula
        if subida <= 0:
            descartados.append({**ficha, "motivo": "ya está por encima del objetivo"})
            continue
        # `factor=2` sube exactamente `valueToIncrease` y cuesta la mitad. Verificado.
        coste = subida // 2
        propuestas.append({
            **ficha, "subir_hasta": objetivo, "value_to_increase": subida,
            "coste": coste, "factor": 2,
            "coste_por_punto_protegido": (round(coste / ficha["puntos"])
                                          if ficha["puntos"] else None),
            # Cuánto cuesta proteger en proporción a lo que vale el jugador: es la
            # métrica que hace visible por qué los baratos salen a cuenta.
            "coste_sobre_valor": round(coste / valor, 2) if valor else None,
            "queda_inalcanzable": objetivo > caja_rival_max,
        })

    # Lo barato primero: el orden es el euro por punto protegido, no el punto a secas.
    propuestas.sort(key=lambda x: (x["coste_por_punto_protegido"] is None,
                                   x["coste_por_punto_protegido"] or 0))
    return {
        "tactica": "clausulas_baratas",
        "modo": modo,
        "resumen": ("Subir cláusula solo donde sale barato: con factor=2 cuesta la MITAD "
                    "de lo que sube, así que proteger a un jugador de valor bajo es "
                    "calderilla y a uno caro es inviable."),
        "que_significa_el_modo": (
            f"objetivo = valor × {objetivo_ratio:g} (encarecer el robo en proporción)"
            if modo == "disuasorio" else
            "objetivo = caja del rival más rico + 1 € (fuera de alcance del todo). "
            "Cuesta lo MISMO en euros para todos, así que en los baratos es desorbitado."),
        "caja_del_rival_mas_rico": caja_rival_max,
        "propuestas": propuestas,
        "descartados": descartados,
        "riesgos": [
            "Subir cláusula NO reinicia los 14 días de protección: no abre ninguna ventana.",
            "El gasto es invisible en el tablón, así que los rivales no lo ven: pero "
            "tampoco lo ve el estimador de saldos, y por eso sobreestima el ajeno.",
            "No se puede uno endeudar para subir una cláusula: lo dice el reglamento. "
            "(`030.01.32` es otra cosa: el margen de deuda del 20 % en las pujas.)",
        ],
        # No vale poner aquí "que la cláusula sea max(valor, cláusula puesta)": eso ya
        # está medido y no se cumple hacia arriba (Fornals lleva cinco semanas con la
        # cláusula clavada en 65.000.001 y el valor un 71 % por encima). El consejo que
        # sale de ahí es el contrario: en un jugador en racha la subida urge más, no
        # menos, porque cada día que pasa sale más barato robarlo.
        "que_lo_falsaria": ("Que la cláusula baje sola cuando baja el valor: eso sí está "
                            "sin comprobar, y si pasara, subirla antes de una caída sería "
                            "gasto tirado."),
    }



#  T3 · RADIOGRAFÍA: qué táctica usa cada rival


def radiografia_rivales(mercado: list[dict], plantillas: dict[str, int]) -> dict:
    """Clasifica la táctica de mercado de cada rival a partir de datos públicos.

    Recibe el mercado crudo y el tamaño de plantilla por manager. Devuelve, por rival,
    cuántas fichas tiene listadas, qué porcentaje de su plantilla suponen, la prima media
    del precio de salida sobre el valor, cuántas tienen oferta, y una `lectura`.

    La señal que clasifica es el porcentaje de plantilla listado: un 100 % no
    significa que quiera vender a todo el mundo, es la táctica del escaparate, o sea
    recoger una tirada diaria de oferta por cada jugador. Los cortes (80 % y 25 %) son un
    criterio propio, no un umbral medido.
    """
    por_manager: dict[str, dict[str, Any]] = {}
    for f in mercado:
        if f.get("discr") != "marketPlayerTeam":
            continue
        m = ((f.get("sellerTeam") or {}).get("manager") or {}).get("managerName") or "?"
        d = por_manager.setdefault(m, {"listados": 0, "primas": [], "con_oferta": 0})
        d["listados"] += 1
        val = int((f.get("playerMaster") or {}).get("marketValue") or 0)
        sal = int(f.get("salePrice") or 0)
        if val and sal:
            d["primas"].append(_pct(sal, val))
        if int(f.get("numberOfOffers") or 0) > 0:
            d["con_oferta"] += 1

    salida = []
    for m, d in por_manager.items():
        total = plantillas.get(m) or 0
        pct = round(d["listados"] / total * 100) if total else None
        prima = round(st.mean(d["primas"]), 1) if d["primas"] else None
        if pct is not None and pct >= 80:
            lectura = ("ESCAPARATE: lista casi toda la plantilla para recoger una tirada "
                       "diaria de oferta del sistema por cada jugador")
        elif pct is not None and pct >= 25:
            lectura = "rotación activa: lista una parte, probablemente para hacer caja"
        else:
            lectura = "listados puntuales"
        salida.append({"manager": m, "plantilla": total, "listados": d["listados"],
                       "pct_listado": pct, "prima_media_pct": prima,
                       "con_oferta": d["con_oferta"], "lectura": lectura})
    salida.sort(key=lambda x: -(x["pct_listado"] or 0))

    return {
        "tactica": "radiografia_rivales",
        "resumen": "Qué táctica de mercado usa cada rival, deducida de lo que es público.",
        "rivales": salida,
        "como_leerlo": [
            "pct_listado ≥ 80 %: está jugando al escaparate.",
            "prima_media NEGATIVA no es un descuento: el precio de salida no baja el "
            "suelo, porque una oferta de rival tiene que ser ≥ valor de mercado igual. "
            "Suele significar que listó hace días y el valor ha subido desde entonces.",
            "con_oferta alto: el sistema ya le está ofertando, o un rival le quiere algo.",
        ],
    }
