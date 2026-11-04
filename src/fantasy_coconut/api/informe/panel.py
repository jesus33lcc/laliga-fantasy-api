"""El cruce: qué hacer hoy, ordenado por lo que cuesta no hacerlo, y lo que alimenta esa
decisión: cajas rivales, dificultad del calendario y funciones premium.

Parte de `/informe/*`. Ver `comun.py` para lo compartido."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from ...config import settings
from ...db import Session
from ...models.saldos import estimar
from ...sqlite_compat import fecha as _fecha
from ..deps import api, bearer, cliente, protegido
from .comun import CMP, POS, PREMIUM, _equipos_de_jugadores, _saldo_real, bloque, cobertura

# El panel es el único módulo del paquete que importa de sus hermanos, porque cruza
# dominios. La dependencia va en un solo sentido: ni `equipo` ni `ventanas` importan de
# aquí, y mantenerlo así es lo que evita el ciclo.
#
# Un import que falte aquí no da error visible. `_asuntos_de_jornada` lo envuelve
# todo en un `except`, así que el `NameError` sale en la pantalla como un aviso de
# urgencia 60 ("No se pudo validar el once"). Una comparación de formas tampoco lo
# detecta: la respuesta conserva la forma y solo cambia el contenido.
from .equipo import validar_once
from .ventanas import ventana_clausulazo

# Cuelga de `coconut.api.informe`, así que hereda su configuración.
log = logging.getLogger("coconut.api.informe.panel")

router = APIRouter()


def _hora_local(cuando) -> str:
    """Formatea un instante en hora local y día en palabras: "hoy a las 13:48".

    Devuelve `str(cuando)` sin tocar si no se puede interpretar como fecha.

    Un instante sin zona horaria se trata como UTC, no como local. La base guarda
    en UTC y `astimezone()` sobre un dato ingenuo asume la zona del proceso, así que sin
    esto una expiración de las 13:48 se mostraría como las 11:48, sin nada que lo delate.

    El día va en palabras ("hoy", "mañana") y no como fecha porque el error que cuesta
    dinero es confundir el día, no la hora.
    """
    try:
        t = dt.datetime.fromisoformat(str(cuando).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(cuando)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    t = t.astimezone()
    dias = (t.date() - dt.datetime.now(t.tzinfo).date()).days
    return f"{ {0: 'hoy', 1: 'mañana'}.get(dias) or t.strftime('el %d-%b') } a las {t:%H:%M}"


@router.get("/hoy", summary="Qué hacer hoy, ordenado por urgencia")
async def hoy():
    """El cruce. Junta en una pantalla lo que vive repartido en cinco endpoints.

    La ventana de clausulazo, el once, la exposición, el momentum y la caja se saben por
    separado, y cruzarlas es lo que da la decisión: abrir cinco sitios y sostener la
    foto en la cabeza no escala. Aquí sale una sola lista ordenada por lo que cuesta no
    hacer cada cosa.

    La urgencia mide el coste de fallar, y estos son TODOS los valores que se emiten:

    | urgencia | asunto | qué cuesta |
    |---|---|---|
    | 100 | `once_invalido` · `saldo_negativo` | la jornada entera |
    | 85 / 55 / 25 | `ventana_clausulazo`, según queden <8 h, <24 h o más | la ventana |
    | 70 | `exposicion` | un jugador, si el rival paga la cláusula |
    | 65 | `premium_nuevo` | puntos en silencio, por una función activa sin implementar |
    | 45 | `saldo_justo` | margen para operar |
    | 40 | `momentum` | una compra con fecha |
    | 35 | `rival_no_alinea` | nada tuyo: es información sobre otro |
    | 15 | `bloque_roto:*` | una sección del panel que no se pudo calcular |
    | 10 | `clausulazos_cerrados` | nada, informa de que la ventana está cerrada |
    | 5 | `sin_momentum` | nada |

    El 15 es el bloque que falla por algo de fuera (la API no responde). Si falla por un
    fallo de programación (`BUGS` en `comun.py`) sube a `urgencia_bug`, que vale **60** en
    la validez del once, **70** en quién te puede clausular y **55** en los otros cuatro:
    un `NameError` ahí deja el panel mintiendo, y con 15 se leería como un aviso menor.
    """
    # Las cuatro secciones van EN paralelo, cada una a su lista: son independientes.
    #
    # El token bucket sigue sirviendo las peticiones de una en una (eso no se toca, es lo
    # que separa de un baneo), pero mientras una espera su turno las otras adelantan su
    # trabajo de base de datos y de cálculo. Y como el cliente coalesce las peticiones
    # en vuelo, dos secciones que piden la misma ruta a la vez gastan una sola.
    #
    # Cada una escribe en SU lista y se concatenan en orden fijo, en vez de compartir
    # una. Con una lista común el orden dependería de quién termine antes, y dos
    # ejecuciones con los mismos datos darían salidas distintas: que es justo lo que
    # convierte un fallo en irreproducible.
    listas: list[list[dict]] = [[], [], [], []]
    await asyncio.gather(
        _asuntos_de_jornada(listas[0]),
        _asuntos_de_dinero(listas[1]),
        _asuntos_de_rivales(listas[2]),
        _asuntos_premium(listas[3]),
    )
    out: list[dict] = [a for lista in listas for a in lista]
    # `sort` es estable, así que a igualdad de urgencia manda el orden de arriba.
    out.sort(key=lambda a: -a["urgencia"])
    hay = [a for a in out if a["urgencia"] >= 40]
    return {
        "resumen": (f"{len(hay)} asunto(s) que atender" if hay else "Sin acción. Todo en orden."),
        "critico": any(a["urgencia"] >= 85 for a in out),
        "asuntos": out,
    }


@router.get("/saldos", summary="Cuánta caja tiene cada manager (estimada del tablón)")
async def saldos_rivales():
    """La caja de cada manager, deducida de la contabilidad del tablón.

    Se recalcula en cada consulta leyendo `league_events`, así que está tan fresco
    como la última ingesta de liga (`job_league`, cada 30 min). El
    `vigilante` también lo recalcula y lo guarda en `managers.balance_est` tras cada
    ingesta, así que no hay que hacer nada para refrescarlo.

    Es una cota superior. Dos conceptos mueven dinero y no salen en el tablón:
    subir cláusula (gasto) y la recompensa diaria (ingreso, 100.000 €/día, ya
    sumada aquí). Como las subidas de cláusula ajenas son invisibles y solo restan, el
    saldo real de un rival es igual o menor que lo que dice esto, nunca mayor.

    El campo `desvio_del_estimador` compara el estimado propio con el saldo real y es
    la calibración viva: mejor mirarlo que fiarse de un porcentaje escrito aquí.
    """
    saldos, inicial = await estimar()
    async with Session() as s:
        ult = (await s.execute(text(
            "SELECT max(ts) FROM league_events"))).scalar()
        n_ev = (await s.execute(text("SELECT count(*) FROM league_events"))).scalar()

    # Mi saldo no se estima: se lee de la API. Estimarlo teniendo el dato exacto engaña,
    # porque el estimador no ve las subidas de cláusula propias y sale alto. Ese desvío,
    # medible solo en el equipo propio, es la medida de cuánto se equivoca el
    # estimador con un rival activo.
    mio_real = await _saldo_real()

    filas = []
    for x in sorted(saldos, key=lambda v: -(v.estimado or 0)):
        est = int(x.estimado or 0)
        real = int(mio_real) if (x.es_mio and mio_real) else None
        filas.append({
            "manager": x.nombre, "es_mio": x.es_mio,
            "caja_estimada": real if real is not None else est,
            "es_exacto": real is not None,
            "solo_estimado": est,
            "desvio_del_estimador": (est - real) if real is not None else None,
            "valor_plantilla": int(x.valor_plantilla or 0),
            "compras": int(x.compras or 0), "ventas": int(x.ventas or 0),
            "movimientos": int(x.movimientos or 0),
            # Se puede pujar en deuda hasta el 20 % del valor del equipo (`030.01.32`,
            # que habla de pujas); para clausular no se puede uno endeudar, así que lo
            # que cuenta para saber si te pueden robar es la caja limpia.
            "margen_deuda": int(0.20 * (x.valor_plantilla or 0)),
        })
    return {
        "saldos": filas,
        "inicial_usado": inicial,
        "eventos_en_el_tablon": n_ev,
        # `fecha()` porque `ult` sale de un `max(ts)`, y un agregado pierde el tipo
        # declarado: en SQLite vuelve como texto, no como `datetime`, así que un
        # `.isoformat()` directo revienta con `'str' object has no attribute ...`.
        "ultimo_evento": _fecha(ult).isoformat() if ult else None,
        "aviso": ("El tuyo es EXACTO (leído de la API); los rivales son estimados y son "
                  "una COTA SUPERIOR: no se restan sus subidas de cláusula, invisibles "
                  "en el tablón, así que su saldo real es igual o menor. Para clausular "
                  "NO se puede uno endeudar, o sea que esta cifra es la que decide a "
                  "quién te pueden quitar."),
        "calibracion": ("Mi propio desvío mide el error del modelo con un manager "
                        "activo: es lo que el estimador se pasa por no ver las subidas "
                        "de cláusula."),
    }


@router.get("/premium", summary="¿Es premium esta liga? Y qué cambiaría si lo fuera")
async def premium():
    """Lee `config.premiumFeatures` de la liga y dice qué funciones premium están activas.

    Para cada una devuelve además qué aportaría, sus reglas y las rutas conocidas del APK,
    de forma que activarla no obligue a descubrirlas a base de 400.

    Va por liga, no por cuenta. Si la liga no es premium, ningún manager tiene
    capitán, banquillo, cesiones ni once ideal, así que no hay desventaja que compensar.

    Esas rutas premium salen del catálogo del APK y no se ha ejecutado ninguna.
    Son un punto de partida, no un camino verificado.
    """
    tok = await bearer()
    try:
        ligas = await protegido(cliente().get(f"{CMP}/leagues", ttl=600, auth=tok))
    except Exception as e:
        raise HTTPException(502, f"no se pudo leer la liga: {type(e).__name__}") from e

    liga = next((x for x in (ligas if isinstance(ligas, list) else [ligas])
                 if str(x.get("id")) == str(settings.fantasy_league_id)), None)
    if liga is None:
        liga = (ligas if isinstance(ligas, list) else [ligas])[0] if ligas else {}

    cfg = liga.get("config") or {}
    activas = cfg.get("premiumFeatures") or {}
    ajustes = cfg.get("premiumConfigurations") or {}

    funciones = []
    for clave, meta in PREMIUM.items():
        funciones.append({
            "clave": clave, **meta,
            "activa": bool(activas.get(clave)),
            "ajustes": ajustes.get(clave),
            # Ninguna de las seis tiene código detrás, así que va en `False` para todas.
            # Quien implemente una la saca de aquí: activa y sin implementar es lo que
            # dispara el aviso de urgencia 65 de `/informe/hoy`.
            "implementada": False,
        })
    funciones.sort(key=lambda f: (not f["activa"], f["clave"]))

    n = sum(1 for f in funciones if f["activa"])
    return {
        "liga": liga.get("name"), "premium": n > 0,
        "activas": n, "total": len(funciones),
        "features_crudas": activas,
        "configuraciones": ajustes,
        "funciones": funciones,
        "nota": ("Premium va por LIGA, no por cuenta: si no lo es, no lo es para nadie y "
                 "no hay desventaja. Si se activara, aquí está lo que aparecería."),
    }


@router.get("/calendario-dificultad", summary="Contra quién juega cada uno tuyo, y cuánto duele")
async def calendario_dificultad(jornadas: int = Query(4, ge=1, le=8)):
    """Los próximos `jornadas` rivales de cada jugador propio, con su dificultad.

    Por jugador devuelve la dificultad del rival según su posición (para un defensa
    pesa el ataque del rival; para un delantero, su defensa), la probabilidad de portería
    a cero para porteros y defensas, y el riesgo de rotación a partir de la titularidad.
    El modelo vive en `models/dificultad.py`.

    La probabilidad de portería a cero no está calibrada. Se deriva del ataque
    del rival, no de cuotas. Sirve para ordenar jugadores entre sí, no como probabilidad
    en términos absolutos.
    """
    from ...models.dificultad import dificultad, fuerzas, prob_porteria_cero

    tok = await bearer()
    fz = await fuerzas()
    equipos_jug = await _equipos_de_jugadores()
    semana = await protegido(api().jornada_actual())
    actual = int(semana.get("weekNumber") or 1)

    # Probabilidad de ser titular, de los onces probables ya ingeridos.
    async with Session() as s:
        probs = {int(r[0]): float(r[1]) for r in (await s.execute(text("""
            SELECT player_id, probability FROM probable_lineups
             WHERE jornada = (SELECT max(jornada) FROM probable_lineups)"""))).all()}

    # Calendario de las próximas N jornadas: quién juega contra quién.
    rival_de: dict[int, dict[int, dict]] = {}
    for n in range(actual, actual + jornadas):
        try:
            cal = await protegido(api().calendario(n))
        except Exception as e:
            # Una jornada sin calendario deja a sus jugadores "sin partido", que se
            # parece mucho a "descansa". Se salta, pero queda dicho por qué.
            log.warning("calendario de la jornada %s no disponible (%s)", n, type(e).__name__)
            continue
        for c in (cal if isinstance(cal, list) else []):
            local, visitante = int(c["localId"]), int(c["visitorId"])
            rival_de.setdefault(n, {})[local] = {"rival": visitante, "casa": True}
            rival_de.setdefault(n, {})[visitante] = {"rival": local, "casa": False}

    plantilla = await protegido(api().plantilla(
        tok, settings.fantasy_league_id, settings.fantasy_team_id))

    jugadores = []
    for p in (plantilla.get("players") or []):
        pm = p.get("playerMaster") or {}
        pid = int(pm.get("id") or 0)
        pos = int(pm.get("positionId") or 0)
        equipo = equipos_jug.get(pid)
        prob = probs.get(pid)

        proximas = []
        for n in sorted(rival_de):
            info = rival_de[n].get(equipo or -1)
            if info is None:
                proximas.append({"jornada": n, "rival": None, "nivel": None,
                                 "etiqueta": "no juega"})
                continue
            riv = fz.get(info["rival"])
            d = dificultad(pos, riv, fz)
            proximas.append({
                "jornada": n, "rival": info["rival"], "en_casa": info["casa"],
                **d,
                "porteria_cero": (prob_porteria_cero(riv, fz)
                                  if pos in (1, 2) else None),
            })

        niveles = [x["nivel"] for x in proximas if x.get("nivel")]
        jugadores.append({
            "player_id": pid, "nombre": pm.get("nickname"),
            "posicion": POS.get(pos, "?"), "equipo": equipo,
            "puntos": pm.get("points") or 0,
            "estado": pm.get("playerStatus"),
            "prob_titular": prob,
            # Un jugador bueno que no sale titular vale lo que su probabilidad de salir.
            "riesgo_rotacion": (prob is not None and prob < 0.6),
            "dificultad_media": round(sum(niveles) / len(niveles), 2) if niveles else None,
            "proximas": proximas,
        })

    jugadores.sort(key=lambda j: (j["dificultad_media"] or 9))
    return {
        "desde_jornada": actual, "jornadas": jornadas,
        "equipos_con_rating": len(fz),
        "fiable": sum(1 for f in fz.values() if f.fiable),
        "jugadores": jugadores,
        "nota": ("Con pocas jornadas jugadas esto ORIENTA, no decide: cada rating se "
                 "suaviza hacia la media para que un solo partido bueno no dispare a un "
                 "equipo."),
    }


#  /informe/hoy: "¿qué hago?"


async def _asuntos_de_jornada(out: list[dict]) -> None:
    """Lo que puede costar la jornada entera: once ilegal y ventana de clausulazo."""
    async with bloque(out, "validez del once", urgencia_bug=60):
        v = await validar_once()
        if not v.get("valido"):
            out.append({"urgencia": 100, "clave": "once_invalido",
                        "titulo": "⛔ El once NO es válido",  # sin-rastro-ok: titulo del panel, lo ve el usuario
                        "detalle": "; ".join(v.get("problemas") or []),
                        "accion": "Corregir la alineación antes de que abra la jornada",
                        "porque": "Un once incompleto anula la jornada entera (tipo 7)"})

    async with bloque(out, "ventana de clausulazo"):
        vc = await ventana_clausulazo()
        h = vc.get("horas_restantes") or 0
        if vc.get("abierta"):
            # La urgencia sube según se cierra la ventana. Informa siempre, aunque falten
            # días: saber cuánto queda es lo que permite planificar en vez de correr.
            urg = 85 if h < 8 else 55 if h < 24 else 25
            out.append({"urgencia": urg, "clave": "ventana_clausulazo",
                        "titulo": f"⏳ Quedan {h:.0f} h de ventana de clausulazo",  # sin-rastro-ok: titulo del panel, lo ve el usuario
                        "detalle": f"Se cierran el {vc['clausulazos_hasta'][:16].replace('T', ' ')}",
                        "accion": "Decidir clausulazos ANTES de esa hora",
                        "porque": "Se desactivan 24 h antes de la jornada (030.01.17)"})
        elif vc.get("abierta") is False:
            out.append({"urgencia": 10, "clave": "clausulazos_cerrados",
                        "titulo": "🔒 Clausulazos cerrados",  # sin-rastro-ok: titulo del panel, lo ve el usuario
                        "detalle": "Estamos dentro de las 24 h previas a la jornada; "
                                   "se reabren en cuanto empiece",
                        "accion": "Nada que hacer por esta vía hasta entonces"})


async def _asuntos_de_dinero(out: list[dict]) -> None:
    """Saldo, exposición y momentum. Todo lo que se mide en euros."""
    from ...models import precio
    from ...models.vigilancia import mi_exposicion

    saldo = await _saldo_real()
    if saldo is not None:
        if saldo < 0:
            out.append({"urgencia": 100, "clave": "saldo_negativo",
                        "titulo": "🚨 SALDO NEGATIVO",  # sin-rastro-ok: titulo del panel, lo ve el usuario
                        "detalle": f"{saldo/1e6:.2f}M",
                        "accion": "Vender ya para volver a positivo",
                        "porque": "El saldo negativo ANULA la jornada (tipo 8)"})
        elif saldo < 2_000_000:
            out.append({"urgencia": 45, "clave": "saldo_justo",
                        "titulo": f"Caja muy justa: {saldo/1e6:.2f}M",
                        "detalle": "Sin margen para reaccionar a un clausulazo",
                        "accion": "Considerar vender lastre"})

    async with bloque(out, "quién te puede clausular", urgencia_bug=70):
        # `estimar()` devuelve (lista de Saldo, presupuesto_inicial deducido).
        saldos, _ = await estimar()
        # Es una COTA SUPERIOR: las subidas de cláusula ajenas no salen en el tablón y no
        # se restan, así que aquí entran jugadores que nadie puede pagar de verdad. El
        # sesgo va al lado seguro (avisa de más), pero para gastar el cupo de blindajes
        # hay que cruzarlo a mano con el gasto en cláusulas medido.
        caja_rival = max([s.estimado or 0 for s in saldos if not s.es_mio] or [0])
        # Pedir la lista entera y recortar después de filtrar. `mi_exposicion` aplica
        # su `limite` antes de descartar a los protegidos: con `limite=3`, tres jugadores
        # a salvo en cabeza dejan el panel mudo aunque haya alguien expuesto de verdad.
        expuestos = [x for x in await mi_exposicion(caja_rival, limite=30)
                     if not x.get("bloqueada")]

        # Cruce con el blindaje real antes de avisar: la BD va hasta 30 min por detrás y
        # uno puesto hace un rato todavía no está, así que sin esto el panel señala un
        # peligro ya resuelto, y un aviso falso en la pantalla de urgencias enseña a
        # ignorarla. Con `None` nos quedamos con la foto de la BD: avisar de más es
        # preferible a callarse con alguien de verdad expuesto.
        from ...models.vigilancia import blindados_ahora
        blindados = await blindados_ahora()
        if blindados is not None:
            expuestos = [x for x in expuestos if x["jugador"] not in blindados]
        else:
            log.warning("exposición: sin blindaje en vivo, se usa la BD")

        # Con 2 blindajes por jornada lo que decide es cuánto cuesta perder a cada uno, y
        # el ratio mide otra cosa: lo barato que sale robarlo. Tampoco sabe si el jugador
        # juega, así que por ahí encabezan un lesionado o un suplente, de caída cero,
        # mientras queda fuera un titular. La caída sale de rehacer el once sin él.
        from ...models.blindaje import anotar_perdida
        await anotar_perdida(expuestos)
        expuestos.sort(key=lambda x: (-x.get("perdida", 0.0), x.get("ratio") or 0))

        # Perder a quien no entra en el once no cuesta puntos, así que no es una urgencia.
        # Queda en el log porque una caída de 0 sale también cuando el modelo no la puede
        # calcular, y desde fuera las dos se leen igual.
        sin_coste = [x["jugador"] for x in expuestos if x.get("perdida", 0.0) <= 0]
        if sin_coste:
            log.info("expuestos que no cuestan puntos, no se avisa de ellos: %s",
                     ", ".join(sin_coste))

        for x in [e for e in expuestos if e.get("perdida", 0.0) > 0][:3]:
            out.append({"urgencia": 70, "clave": "exposicion",
                        "titulo": f"🎯 Te pueden clausular a {x['jugador']}",  # sin-rastro-ok: titulo del panel, lo ve el usuario
                        "detalle": f"cláusula {(x.get('clausula') or 0)/1e6:.2f}M "
                                   f"(×{x.get('ratio') or 0:.2f} su valor) · perderlo cuesta "
                                   f"{x.get('perdida', 0.0):.2f} del once · caja del rival "
                                   f"más rico ≈ {caja_rival/1e6:.1f}M",
                        "accion": "Blindar (gratis, 24 h) o subir la cláusula (factor 2)"})

    async with bloque(out, "momentum de precios"):
        op = await precio.oportunidades(top=99)   # sin recortar: el conteo debe ser real
        # Se filtra por lo que cabe en caja: un fichaje que no se puede pagar no es una
        # propuesta. Los que suben pero no caben salen aparte, en `caros`.
        tope = saldo if saldo is not None else 0
        suben = [c for c in op["comprables"]
                 if c["sube_1d_pct"] > 0.5 and c["valor"] <= tope]
        caros = [c for c in op["comprables"]
                 if c["sube_1d_pct"] > 0.5 and c["valor"] > tope]
        for c in suben[:3]:
            # La hora va en hora de Madrid y con el día dicho, no recortando la cadena.
            # `expira` puede llegar en UTC, y un corte a ciegas la enseña dos horas antes
            # sin avisar. Y la subasta resuelve CUANDO expira la ficha, que puede ser hoy:
            # un "de mañana" escrito a fuego regala un día que no existe, así que la hora
            # sale del dato.
            cuando = _hora_local(c["expira"])
            out.append({"urgencia": 40, "clave": "momentum",
                        "titulo": f"📈 {c['nombre']} sube {c['sube_1d_pct']:+.1f}%",  # sin-rastro-ok: titulo del panel, lo ve el usuario
                        # `sube_3d_pct` viene `None` para quien lleva menos de 4 días
                        # de serie, que es justo el recién llegado al mercado. Un
                        # `f"{None:+.1f}"` lanza `TypeError`, y `TypeError` está en
                        # `BUGS`: el bloque entero se sustituiría por "el panel está
                        # incompleto", o sea que el aviso de compra del día se pierde.
                        "detalle": (f"valor {c['valor']/1e6:.2f}M"
                                    + (f" · 3d {c['sube_3d_pct']:+.1f}%"
                                       if c.get("sube_3d_pct") is not None else "")
                                    + " · agente libre"),
                        "accion": f"Pujar antes de que resuelva la subasta, {cuando}",
                        "porque": "El momentum rinde +2,5 %/día sobre la media y dura "
                                  "varios días"})
        if not suben:
            det = f"{len(op['comprables'])} agentes libres"
            if caros:
                det += (f", y {'el que sube no cabe' if len(caros) == 1 else f'los {len(caros)} que suben no caben'}"
                        f" en caja (el más barato, {min(c['valor'] for c in caros)/1e6:.1f}M)")
            else:
                det += " y ninguno sube"
            out.append({"urgencia": 5, "clave": "sin_momentum",
                        "titulo": "📉 Nada que comprar hoy", "detalle": det,  # sin-rastro-ok: titulo del panel, lo ve el usuario
                        "accion": "Esperar a la baraja de mañana"})


async def _asuntos_de_rivales(out: list[dict]) -> None:
    """Añade a `out` un aviso por cada rival que no pueda formar un once legal.

    Un once incompleto anula la jornada entera del afectado (evento de tipo 7), y las
    plantillas rivales son públicas, así que se ve con antelación.

    Cuenta las posiciones desde una sola petición: `GET {CMP}/leagues/{lg}/teams` trae
    las cuatro plantillas con `positionId` y `playerStatus` de cada ficha. Llamar a
    `equipo(tid)` por rival traería además el once congelado, el calendario, el saldo y el
    estimador de cajas, que aquí no se usan, y el token bucket sirve a 0,5 req/s.
    """
    async with bloque(out, "si los rivales pueden alinear"):
        equipos = await protegido(cliente().get(
            f"{CMP}/leagues/{settings.fantasy_league_id}/teams", ttl=300, auth=await bearer()))
        _rivales_que_no_alinean(out, equipos)


def _rivales_que_no_alinean(out: list[dict], equipos: list[dict]) -> None:
    """Recorre las plantillas de `equipos` y anota en `out` las que no cubren ninguna
    formación legal. Sin E/S: recibe ya la respuesta de la API."""
    for eq in equipos:
        tid = str(eq.get("id"))
        if tid == str(settings.fantasy_team_id):
            continue
        fichas = [((pm := p.get("playerMaster") or {}) and POS.get(pm.get("positionId"), "?"),
                   pm.get("playerStatus"))
                  for p in (eq.get("players") or [])]
        if not fichas:
            continue                      # sin plantilla no se puede afirmar nada
        _cuenta, faltan = cobertura(fichas)
        if not faltan:
            continue
        mgr = (eq.get("manager") or {}).get("managerName") or eq.get("name") or tid
        out.append({
            "urgencia": 35, "clave": "rival_no_alinea",
            "titulo": f"👀 {mgr} no puede alinear",  # sin-rastro-ok: titulo del panel, lo ve el usuario
            "detalle": f"{'; '.join(faltan)} · va {eq.get('position')}º con "
                       f"{eq.get('teamPoints')} pts",
            "accion": "Vigílalo: si no lo arregla antes de que abra la jornada, puntúa 0",
            "porque": "Un once incompleto anula la jornada entera (tipo 7)"})


async def _asuntos_premium(out: list[dict]) -> None:
    """Avisa si la liga pasa a premium con funciones que el sistema no implementa.

    Es un cambio que no genera ningún error: capitán, banquillo o cesiones
    simplemente aparecerían en la app oficial y aquí no, y se irían perdiendo puntos sin
    que nada fallara. Por eso se vigila.
    """
    async with bloque(out, "funciones premium"):
        p = await premium()
        faltan = [f["titulo"] for f in p["funciones"] if f["activa"] and not f["implementada"]]
        if faltan:
            out.append({
                "urgencia": 65, "clave": "premium_nuevo",
                "titulo": f"⭐ La liga tiene {len(faltan)} función(es) premium sin usar",  # sin-rastro-ok: titulo del panel, lo ve el usuario
                "detalle": ", ".join(faltan),
                "accion": "Implementarlas: se están perdiendo puntos sin que nada dé error",
                "porque": "Premium va por liga; si está activo, los rivales sí lo usan"})
