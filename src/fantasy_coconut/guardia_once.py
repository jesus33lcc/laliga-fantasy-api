"""Repara el once cuando te rompen la plantilla, sin esperar a que alguien lo mire.

El agujero que cierra

Tres piezas que por separado funcionan y juntas dejan un hueco:

- `job_alineacion` se para en seco si hay candado (`if await candado("once"): return`).
  Es lo correcto para que el modelo no pise una decisión humana, pero también impide
  reparar.
- `vigilante.py` y el centinela detectan el robo y avisan como emergencia. Sin este
  módulo ahí acabaría todo.
- La alineación se congela en el `openingWeekDate`. Lo que no esté arreglado a esa
  hora ya no se arregla.

Resultado: candado puesto + clausulazo a un titular = once de 10 = la jornada entera a
cero (tipo 7).

Qué hace

Compara el once que está guardado en la API con la plantilla real. Si falta alguien, o
si un titular está en una línea que ya no es la suya porque LaLiga le cambió la
demarcación:

1. Sustituye solo el hueco, con el mejor suplente de esa misma posición. La decisión
   humana sobre los otros diez se respeta: es lo que la distingue de un recálculo.
2. Si no hay recambio en esa posición, recalcula el once entero y cambia de formación
   si hace falta. Con diez, la jornada entera puntúa cero.
3. Si ni así sale un once legal, no manda nada y avisa: dejarlo como está es mejor
   que sustituirlo por algo roto.

Por qué se salta el candado

Porque una reparación no es un recálculo. El candado existe para que el cron no pise
tu criterio; aquí el criterio ya no se puede cumplir, porque el jugador no es tuyo, y la
alternativa a saltárselo es puntuar cero. Se avisa siempre de qué se ha tocado, y el
candado se deja puesto para que el resto siga protegido.
"""

from __future__ import annotations

import datetime as dt
import logging

from .alerts import EMERGENCIA, enviar

log = logging.getLogger("coconut.guardia_once")

LINEAS = ("goalkeeper", "defender", "midfield", "striker")
# Cada línea del payload se nutre de una posición del maestro.
POS_DE_LINEA = {"goalkeeper": 1, "defender": 2, "midfield": 3, "striker": 4}
NOMBRE = {"goalkeeper": "portero", "defender": "defensa",
          "midfield": "medio", "striker": "delantero"}


def _ids(payload: dict) -> dict[str, list[str]]:
    """Normaliza el once a listas de `playerTeamId` por línea.

    `goalkeeper` llega como valor suelto y las demás como lista; unificarlo aquí evita
    repetir el `isinstance` en cada recorrido.
    """
    out: dict[str, list[str]] = {}
    for k in LINEAS:
        v = payload.get(k)
        if v is None:
            out[k] = []
        elif isinstance(v, list):
            out[k] = [str(x) for x in v if x]
        else:
            out[k] = [str(v)]
    return out


async def _once_guardado() -> dict | None:
    """El once que la API tiene ahora mismo, en formato de payload."""
    from .clients.fantasy import FantasyAPI
    from .clients.via_api import cliente_lectura
    from .config import settings

    async with cliente_lectura() as http:
        datos = await FantasyAPI(http).http.get(
            f"/v1/competition/1/teams/{settings.fantasy_team_id}/lineup",
            ttl=30, auth=await _bearer())
    f = datos.get("formation") or {}
    if not f:
        return None

    payload: dict = {}
    for k in LINEAS:
        v = f.get(k) or []
        ids = [str(p.get("playerTeamId")) for p in (v if isinstance(v, list) else [v])
               if p.get("playerTeamId")]
        payload[k] = ids[0] if k == "goalkeeper" and ids else ids
    # El GET lo devuelve en camelCase y el PUT lo exige en snake_case.
    tf = datos.get("tacticalFormation") or f.get("tacticalFormation")
    if tf:
        payload["tactical_formation"] = list(tf)
    return payload


async def _bearer() -> str:
    from .clients.auth import tokens
    return await tokens.bearer()


async def _refrescar_candado(payload: dict) -> None:
    """Pone al día a quién protege el candado, sin abrirlo ni cambiar su motivo.

    Solo toca la lista de titulares. Si no hay candado echado no hace nada: refrescar uno
    que no existe sería echarlo sin que nadie lo haya pedido.
    """
    import json as _json

    from sqlalchemy import text as _text

    from .db import Session

    ids: list[str] = []
    for k in LINEAS:
        v = payload.get(k) or []
        ids += [str(x) for x in (v if isinstance(v, list) else [v]) if x]
    if len(ids) != 11:
        # No se congela una lista que no cuadra: dejar la vieja es menos malo que
        # sustituirla por una incompleta, y el aviso de reparación ya ha salido.
        log.warning("candado no refrescado: el once reparado trae %d ids, no 11", len(ids))
        return

    async with Session() as s:
        fila = (await s.execute(_text(
            "SELECT datos FROM bloqueos WHERE recurso = 'once'"))).scalar()
        if fila is None:
            return
        datos = fila if isinstance(fila, dict) else _json.loads(fila or "{}")
        datos["titulares"] = ids
        datos["refrescado_por_la_guardia"] = dt.datetime.now(dt.UTC).isoformat()
        await s.execute(_text("UPDATE bloqueos SET datos = :d WHERE recurso = 'once'"),
                        {"d": _json.dumps(datos)})
        await s.commit()
    log.warning("candado refrescado: protege los 11 del once reparado")


async def _mios_ahora() -> dict[str, int | None] | None:
    """Los `playerTeamId` que son míos ahora mismo, con su posición, según la API.

    La posición viene de la misma lectura y sin caché: el maestro público va hasta 8 h por
    detrás, y un titular al que LaLiga cambia de demarcación sigue en su línea vieja del
    once hasta que algo refresca la alineación y lo descarta.

    Por qué no vale `mi_plantilla()` para esto

    `mi_plantilla()` lee la base, y la base va hasta una hora por detrás (un ciclo de
    `league`). Para elegir un suplente eso da igual (lo que se necesita de ahí son puntos
    esperados y titularidad, que no cambian en una hora), pero para preguntar "¿este
    jugador sigue siendo mío?" es justo el dato que no se puede tener viejo.

    Con la foto vieja, el jugador que te acaban de clausular sigue apareciendo en tu
    plantilla, así que el hueco no se detecta y la guardia devuelve `ok: True`.

    Cuesta una petición por pasada y se lee con `ttl=0`. Si falla se devuelve `None`
    y quien llama se queda con la foto de la base: peor que fresco, mejor que nada.
    """
    from .clients.fantasy import FantasyAPI
    from .clients.via_api import cliente_lectura
    from .config import settings
    try:
        async with cliente_lectura() as http:
            datos = await FantasyAPI(http).plantilla(
                await _bearer(), settings.fantasy_league_id,
                str(settings.fantasy_team_id), ttl=0)
        out: dict[str, int | None] = {}
        for j in datos.get("players") or []:
            if not j.get("playerTeamId"):
                continue
            pos = (j.get("playerMaster") or {}).get("positionId")
            out[str(j["playerTeamId"])] = int(pos) if pos not in (None, "") else None
        return out
    except Exception as e:
        log.warning("guardia: no se pudo leer la plantilla en vivo (%s); "
                    "se compara contra la base", type(e).__name__)
        return None


async def revisar(*, reparar: bool = True, motivo: str = "revisión") -> dict:
    """Comprueba el once real contra la plantilla real y lo arregla si hace falta."""
    from .models.alineacion import a_payload, mejor_once, mi_plantilla, valido

    try:
        actual = await _once_guardado()
    except Exception:
        log.exception("guardia: no se pudo leer el once guardado")
        return {"ok": False, "error": "no se pudo leer el once"}
    if not actual:
        return {"ok": True, "sin_once": True}

    plantilla = await mi_plantilla()
    puestos = _ids(actual)

    # La propiedad y la posición se preguntan a la API; los puntos esperados salen de la
    # base. Son cosas distintas y solo las dos primeras envejecen mal.
    en_vivo = await _mios_ahora()
    if en_vivo is not None:
        # Con la posición de la base, el recambio y el recálculo colocarían al jugador
        # cambiado en su línea vieja y el once reparado saldría igual de roto.
        for j in plantilla:
            pos = en_vivo.get(j.player_team_id or "")
            if pos is not None and pos != j.posicion:
                j.posicion = pos
    mios = {j.player_team_id: j for j in plantilla if j.player_team_id}
    propios = set(en_vivo) if en_vivo is not None else set(mios)

    # Dos clases de hueco: el titular que ya no es tuyo, y el que sigue siéndolo pero está
    # en una línea que ya no es la suya. LaLiga descarta a los dos al refrescar el once.
    huecos, descolocados = [], []
    for linea in LINEAS:
        for pid in puestos[linea]:
            if pid not in propios:
                huecos.append((linea, pid))
            elif pid in mios and mios[pid].posicion != POS_DE_LINEA[linea]:
                huecos.append((linea, pid))
                descolocados.append(mios[pid].nombre)
    titulares = sum(len(v) for v in puestos.values())

    if not huecos and titulares == 11:
        # El once está sano, pero el candado puede seguir protegiendo a quien ya no juega,
        # porque una reparación cambia el once sin tocar su lista. Sincronizarlo aquí,
        # y no solo al reparar, es lo que mantiene la garantía viva entre pasadas: un
        # candado desfasado deja vender a un titular de verdad y rechaza al que ya no está.
        await _refrescar_candado(actual)
        return {"ok": True, "titulares": 11, "huecos": 0}

    log.error("guardia: el once tiene %d hueco(s) y %d titulares (%s)",
              len(huecos), titulares, motivo)
    if not reparar:
        return {"ok": False, "huecos": len(huecos), "titulares": titulares,
                "reparado": False}

    # ---------- 1) sustituir hueco por hueco, conservando el resto de la decisión ----------
    # Sin los huecos: un descolocado puede cubrir el de su línea nueva.
    en_once = {p for v in puestos.values() for p in v} - {pid for _, pid in huecos}
    nuevo = {k: list(v) for k, v in puestos.items()}
    sustituciones, sin_recambio, refuerzos = [], [], []

    for linea, pid in huecos:
        pos = POS_DE_LINEA[linea]
        # El recambio sale de la base (es donde están los puntos esperados) pero se
        # cruza con la propiedad real: con alguien que ya no es tuyo en el payload,
        # LaLiga lo descarta en silencio o responde 400, y el once se queda en diez.
        libres = [j for j in plantilla
                  if j.posicion == pos and j.player_team_id not in en_once
                  and j.player_team_id in propios
                  and (j.estado or "ok") not in ("injured", "suspended", "out_of_league")]
        if not libres:
            sin_recambio.append((linea, pid))
            continue
        mejor = max(libres, key=lambda j: j.esperado)
        nuevo[linea] = [mejor.player_team_id if x == pid else x for x in nuevo[linea]]
        en_once.discard(pid)
        en_once.add(mejor.player_team_id)
        sustituciones.append((NOMBRE[linea], mejor.nombre))
        # Si el recambio es flojo, el once queda legal pero no puntúa: se deja
        # preparada la alternativa de pagar una cláusula.
        refuerzos.append(await proponer_refuerzo(linea, mejor.prob_titular))

    # ---------- 2) sin recambio en esa línea: se recalcula entero, formación incluida ----------
    recalculado = False
    if sin_recambio or sum(len(v) for v in nuevo.values()) != 11:
        log.warning("guardia: sin recambio en %s -> se recalcula el once entero",
                    ", ".join(NOMBRE[pos] for pos, _ in sin_recambio) or "alguna línea")
        # Solo con los que la API dice que son míos: la base puede traer todavía al
        # jugador robado, y con su `playerTeamId` el PUT falla igual que arriba.
        once = await mejor_once([j for j in plantilla if j.player_team_id in propios])
        ok, por_que = valido(once)
        if not ok:
            await enviar(
                "🚨 <b>No se puede reparar el once</b>\n"  # sin-rastro-ok: va a Telegram
                f"Faltan {len(huecos)} titular(es) y no hay con qué cubrirlos.\n"
                f"Motivo: {por_que}\n"
                "<b>No se ha mandado nada</b>: un once roto guardado es peor que el actual.",
                nivel=EMERGENCIA, categoria="once_roto",
                clave=f"guardia-sin-solucion:{por_que}", silencio=3 * 3600)
            return {"ok": False, "huecos": len(huecos), "reparado": False,
                    "motivo": por_que}
        nuevo = a_payload(once)
        recalculado = True
    else:
        # `_ids()` deja el portero como lista de uno, y el PUT lo exige suelto.
        # `/accion/alineacion` lo rechaza con 409 (la lista no casa con ningún
        # `playerTeamId`) y por la vía directa LaLiga responde 500.
        nuevo["goalkeeper"] = nuevo["goalkeeper"][0] if nuevo["goalkeeper"] else None
        nuevo["tactical_formation"] = actual.get("tactical_formation") or [
            len(nuevo["defender"]), len(nuevo["midfield"]), len(nuevo["striker"])]

    # ---------- 3) guardar ----------
    # `reparacion=True` salta el candado a propósito (ver el docstring del módulo). El
    # resto de guardas de `/accion/alineacion` siguen aplicándose.
    from .ingest.scheduler import _guardar_once
    try:
        res = await _guardar_once(nuevo, reparacion=True)
    except Exception as e:
        log.exception("guardia: fallo al guardar el once reparado")
        await enviar(f"🚨 <b>No se pudo guardar el once reparado</b>\n{type(e).__name__}: "  # sin-rastro-ok: va a Telegram
                     f"{str(e)[:200]}", nivel=EMERGENCIA, categoria="once_roto",
                     clave="guardia-fallo-guardar", silencio=3600)
        return {"ok": False, "reparado": False, "error": str(e)[:200]}

    # ---------- 4) refrescar a quién protege el candado ----------
    # Tras reparar, la lista del candado sigue con el jugador robado y sin su sustituto:
    # vender al sustituto pasaría y el once volvería a diez. Se refresca la lista, no el
    # candado, que sigue puesto y con su motivo.
    await _refrescar_candado(nuevo)

    detalle = (", ".join(f"{ln} → {nom}" for ln, nom in sustituciones)
               if sustituciones else "once recalculado entero")
    await enviar(
        "🚨 <b>Once reparado automáticamente</b>\n"  # sin-rastro-ok: va a Telegram
        + (f"Faltaban {len(huecos) - len(descolocados)} titular(es): te los han quitado "
           "o se vendieron.\n" if len(huecos) > len(descolocados) else "")
        + (f"Cambio de demarcación, recolocado: {', '.join(descolocados)}.\n"
           if descolocados else "")
        + f"{detalle}\n"
        + ("Se cambió la formación porque no había recambio en su puesto.\n"
           if recalculado else "")
        + ("El candado sigue puesto, con la lista puesta al día." if recalculado
           else "El candado sigue puesto; solo se ha tocado el hueco.")
        + "".join(r for r in refuerzos if r),
        nivel=EMERGENCIA, categoria="once_roto",
        clave=f"guardia-reparado:{dt.date.today()}:{len(huecos)}", silencio=1800)

    log.warning("guardia: once reparado (%s) -> %s", detalle, str(res)[:120])
    return {"ok": True, "reparado": True, "huecos": len(huecos),
            "sustituciones": sustituciones, "recalculado": recalculado}



#  Clausulazo urgente: se propone, no se ejecuta

#
# Cuando te roban un titular, meter al suplente salva la legalidad pero no siempre los
# puntos: el recambio de portero puede ser alguien que sale titular el 10 % de las
# jornadas. La respuesta buena es fichar un sustituto de verdad, y con el mercado libre
# vacío la única vía garantizada es el clausulazo.
#
# Pero eso gasta dinero, así que aquí solo se propone. La regla del proyecto es que
# ninguna operación con dinero se ejecuta sin una persona detrás, y una reparación
# automática no es motivo para saltársela: el coste de no fichar es puntuar poco, el de
# fichar mal es puntuar poco *y* quedarse sin caja. Lo que sí hace el sistema es dejar la
# decisión lista: objetivo, importe exacto al euro y qué pasa si no haces nada.

SQL_OBJETIVOS = """
SELECT p.nickname, mg.name AS dueno, s.buyout_clause, s.clause_lock_until, s.shielded,
       CAST((p.raw->>'marketValue') AS INTEGER) AS valor,
       COALESCE(CAST((p.raw->>'points') AS INTEGER), 0)   AS pts
FROM squad_snapshots s
JOIN players  p  ON p.id = s.player_id
JOIN managers mg ON mg.id = s.manager_id
WHERE s.ts = (SELECT max(ts) FROM squad_snapshots)
  AND NOT mg.is_me
  AND p.position = :pos
  AND p.status = 'ok'
  AND s.buyout_clause <= :tope
  AND NOT COALESCE(s.shielded, false)
  AND (s.clause_lock_until IS NULL OR s.clause_lock_until <= now())
-- Desempate explícito: entre los rivales hay grupos de jugadores con los mismos puntos,
-- y con `LIMIT 3` el empate decide a quién se propone clausular. Sin él, Postgres y
-- SQLite devuelven objetivos distintos con los mismos datos. A igualdad de puntos gana
-- el más barato, y el id cierra el orden.
ORDER BY COALESCE(CAST((p.raw->>'points') AS INTEGER), 0) DESC,
         s.buyout_clause ASC,
         p.id ASC
LIMIT 3
"""


async def objetivos_urgentes(posicion: int, tope: int) -> list[dict]:
    """Los mejores clausulables de una posición que caben en la caja.

    Solo devuelve los atacables: sin blindaje y con los 14 días vencidos. Los demás
    los rechaza la API con un 400.
    """
    from sqlalchemy import text

    from .db import Session
    async with Session() as s:
        filas = (await s.execute(text(SQL_OBJETIVOS),
                                 {"pos": posicion, "tope": tope})).all()
    return [{"nombre": r[0], "dueno": r[1], "clausula": int(r[2] or 0),
             "valor": int(r[5] or 0), "puntos": r[6]} for r in filas]


async def caja_libre() -> int | None:
    """`teamMoney - teamInvestment`, leído de la API. Las pujas vivas no bajan de
    `teamMoney`, así que sin la resta se propondría gastar dinero ya comprometido.

    Devuelve `None` si no se pudo leer, que no es lo mismo que cero. Con un 0, el aviso
    de un once roto anuncia "sin caja, 0.00M libres": un dato inventado sobre dinero,
    justo en el mensaje que se lee con prisa para decidir un refuerzo.
    """
    from .clients.via_api import cliente_lectura
    from .config import settings
    try:
        async with cliente_lectura() as http:
            m = await http.get(f"/v1/competition/1/teams/{settings.fantasy_team_id}/money",
                               ttl=60, auth=await _bearer())
        return int(m.get("teamMoney") or 0) - int(m.get("teamInvestment") or 0)
    except Exception:
        log.warning("no se pudo leer la caja para proponer un clausulazo", exc_info=True)
        return None


async def proponer_refuerzo(linea: str, suplente_prob: float | None) -> str:
    """Texto con el clausulazo que arreglaría el hueco. Vacío si no hace falta.

    Solo se propone cuando el recambio que ha entrado es flojo de verdad
    (probabilidad de ser titular por debajo del 50 %): si el suplente juega, fichar es
    gastar por gusto.
    """
    if suplente_prob is not None and suplente_prob >= 0.5:
        return ""
    pos = POS_DE_LINEA.get(linea)
    if not pos:
        return ""

    caja = await caja_libre()
    if caja is None:
        return "\nNo se ha podido leer la caja, así que no sé si hay margen para reforzar."

    from .politica import LIMITES
    tope = caja - LIMITES.reserva_intocable
    if tope <= 0:
        return f"\n💸 Sin caja para reforzar el {NOMBRE[linea]} ({caja/1e6:.2f}M libres)."  # sin-rastro-ok: va a Telegram

    objetivos = await objetivos_urgentes(pos, tope)
    if not objetivos:
        return (f"\n💸 Ningún {NOMBRE[linea]} clausulable por debajo de "  # sin-rastro-ok: va a Telegram
                f"{tope/1e6:.2f}M ahora mismo.")

    lineas = [f"\n💸 <b>Refuerzo posible</b> (caja libre {caja/1e6:.2f}M):"]  # sin-rastro-ok: va a Telegram
    for o in objetivos:
        # El `.replace()` va sobre el NÚMERO, no sobre la frase: sobre la cadena entera
        # se lleva también la coma del texto y deja "de Fulano . 14.564.916 €".
        euros = f"{o['clausula']:,}".replace(",", ".")
        lineas.append(f"   · <b>{o['nombre']}</b> ({o['puntos']} pts) de {o['dueno']}, "
                      f"{euros} € exactos")
    lineas.append("   Dilo y lo ejecuto. <b>No se hace solo: mueve dinero.</b>")
    return "\n".join(lineas)
