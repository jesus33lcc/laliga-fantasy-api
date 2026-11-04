"""El vigilante: cruza el último snapshot con el anterior y con el tablón, y avisa si hay
que actuar.

Solo se avisa de lo accionable. Cada detector vive en su función:

  - un jugador mío ha cambiado de plantilla (robo), la única emergencia real;
  - la jornada no está puntuando, o LaLiga publica que la ha anulado;
  - un rival baja una cláusula, así que su jugador sale más barato mientras siga así;
  - un rival blinda a alguien, que señala qué jugador teme perder;
  - un objetivo rival con la cláusula por debajo de su valor y dentro de mi saldo;
  - una protección de 14 días que caduca en las próximas horas;
  - jugadores míos que el rival más rico puede pagar.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from ..alerts import EMERGENCIA, enviar
from ..db import Session
from ..models.saldos import estimar
from ..models.vigilancia import (
    blindados_ahora,
    mi_exposicion,
    mi_saldo,
    objetivos,
    proximas_liberaciones,
)
from ..sqlite_compat import fecha
from ..tiempo import ahora, hace

log = logging.getLogger("coconut.vigilante")

# Los blindajes salen del tablón (evento `blindaje`), no de la plantilla: el flag
# `isShielded` dice que alguien está blindado, pero no quién lo blindó ni cuándo.
_BLINDAJES = text("""
    SELECT e.ts, m.name AS rival, m.is_me, p.name AS jugador
    FROM league_events e
    JOIN managers m ON m.id = e.manager_id
    LEFT JOIN players p ON p.id = e.player_id
    WHERE e.kind = 'blindaje' AND e.ts > :desde
    ORDER BY e.ts DESC
""")

# Compara los dos snapshots más recientes de cada jugador de la liga.
_CAMBIOS_CLAUSULA = text("""
    WITH ult AS (SELECT max(ts) t FROM squad_snapshots),
         ant AS (SELECT max(ts) t FROM squad_snapshots
                  WHERE ts < (SELECT t FROM ult))
    SELECT m.name AS rival, m.is_me, p.name AS jugador,
           a.buyout_clause AS antes, u.buyout_clause AS ahora,
           u.clause_lock_until AS bloqueada_hasta,
           mv.value AS valor
    FROM squad_snapshots u
    JOIN squad_snapshots a
      ON a.ts = (SELECT t FROM ant)
     AND a.manager_id = u.manager_id AND a.player_id = u.player_id
    JOIN managers m ON m.id = u.manager_id
    JOIN players  p ON p.id = u.player_id
    LEFT JOIN v_latest_values mv ON mv.player_id = u.player_id
    WHERE u.ts = (SELECT t FROM ult)
      -- Umbral: sin él, esto avisa de "bajadas" de céntimos cuyo antes y después son el
      -- mismo número al redondear. No las decide nadie: es la cláusula siguiendo al valor
      -- de mercado en el recálculo de las 00:15. Una bajada de verdad es un acto
      -- deliberado y se nota, así que se exige al menos un 3 % y 100.000 euros.
      AND u.buyout_clause <= a.buyout_clause * 0.97
      AND a.buyout_clause - u.buyout_clause >= 100000
""")


# La única emergencia real: un jugador mío que ya no está en mi plantilla. Se distingue
# robo de venta sin mirar el tablón: si aparece en la plantilla de otro manager en el
# snapshot nuevo, se lo han llevado (clausulazo o compra). Si no aparece en ninguna, está
# en el mercado o lo has vendido tú, que no es una emergencia.
_ME_LO_HAN_QUITADO = text("""
    WITH ult AS (SELECT max(ts) t FROM squad_snapshots),
         ant AS (SELECT max(ts) t FROM squad_snapshots
                  WHERE ts < (SELECT t FROM ult))
    SELECT p.name AS jugador, a.buyout_clause AS clausula,
           mv.value AS valor, ladron.name AS ladron
    FROM squad_snapshots a
    JOIN managers mio ON mio.id = a.manager_id AND mio.is_me
    JOIN players  p   ON p.id = a.player_id
    LEFT JOIN v_latest_values mv ON mv.player_id = a.player_id
    -- ¿está ahora en la plantilla de otro?
    LEFT JOIN squad_snapshots nuevo
           ON nuevo.ts = (SELECT t FROM ult)
          AND nuevo.player_id = a.player_id
          AND nuevo.manager_id <> a.manager_id
    LEFT JOIN managers ladron ON ladron.id = nuevo.manager_id
    WHERE a.ts = (SELECT t FROM ant)
      AND NOT EXISTS (SELECT 1 FROM squad_snapshots u
                       WHERE u.ts = (SELECT t FROM ult)
                         AND u.manager_id = a.manager_id
                         AND u.player_id  = a.player_id)
""")


# Segunda vía de detección, y la fiable: el tablón. Comparar snapshots falla por dos
# motivos que se suman. La plantilla se pide con caché, así que un ciclo puede escribir su
# snapshot desde una copia tomada antes del clausulazo; y la consulta compara último
# contra anterior, así que un snapshot intermedio se traga el cambio y ningún ciclo lo ve.
#
# El tablón lo declara: un evento `clausulazo` (tipo 1) con `user2Id` = mi manager. Se
# mira una ventana de horas para no resucitar avisos viejos, y el antirrebote de `alerts`
# evita repetirlo cada ciclo.
_ROBOS_DEL_TABLON = text("""
    SELECT p.name AS jugador, e.amount AS clausula, ladron.name AS ladron, e.ts
    FROM league_events e
    JOIN managers ladron ON ladron.id = e.manager_id
    LEFT JOIN players p ON p.id = e.player_id
    WHERE e.kind = 'clausulazo'
      AND (e.raw->>'user2Id') = (SELECT CAST(id AS TEXT) FROM managers WHERE is_me LIMIT 1)
      -- El corte se calcula en Python (`hace(horas=…)`) y entra como parámetro: los
      -- intervalos de Postgres (`make_interval`) no existen en SQLite.
      AND e.ts > :desde
    ORDER BY e.ts DESC
""")


# Las dos vías de detección de robos cuelgan de `is_me`: el JOIN de `_ME_LO_HAN_QUITADO` y
# la subconsulta de `_ROBOS_DEL_TABLON`. Sin esa fila las dos devuelven cero filas sin dar
# error, y el único detector marcado como emergencia se queda mudo.
_HAY_MANAGER_PROPIO = text("SELECT count(*) FROM managers WHERE is_me")


def _m(v) -> str:
    return f"{(v or 0) / 1e6:.1f}M"


async def _revisar_jornada_muerta() -> None:
    """Avisa si el once tiene puntos pero el marcador de la jornada sigue a cero.

    Cuesta 1 petición por pasada, y 3 con la jornada viva. Va por `cliente_lectura()` y
    no por un `ClienteEducado` propio: con cliente propio tendría su token bucket aparte
    y no entraría en el contador que enseña Coconut.
    """
    from ..clients.auth import tokens
    from ..clients.via_api import cliente_lectura
    from ..config import settings

    liga, yo = settings.fantasy_league_id, str(settings.fantasy_team_id)
    async with cliente_lectura() as http:
        tok = await tokens.bearer()
        # La semana se lee primero y con el mismo ttl que la plantilla, porque las dos
        # cifras tienen que describir el mismo instante. `fixturePoints` es el marcador de
        # la jornada en curso y se reinicia a cero en cuanto una jornada cierra: con un ttl
        # mayor aquí, la plantilla llega fresca (marcador a 0) y la semana de caché
        # (todavía viva), que es la combinación que dispara un aviso falso.
        semana = await http.get("/v1/competition/1/week/current", ttl=300)
        n = int(semana.get("weekNumber") or 0)
        if not n or not semana.get("isLive"):
            return
        # Y aunque `isLive` diga que sí: si la hora de cierre ya pasó, la jornada terminó
        # y su marcador ya no es comparable con lo que sumó el once.
        cierre = fecha(semana.get("closingWeekDate"))
        if cierre and ahora() >= cierre:
            return
        equipos = await http.get(f"/v1/competition/1/leagues/{liga}/teams",
                                 ttl=300, auth=tok)
        mio = next((t for t in equipos if str(t.get("id")) == yo), None)
        if not mio:
            return
        oficiales = mio.get("fixturePoints")
        once = await http.get(f"/v1/competition/1/teams/{yo}/lineup/week/{n}",
                              ttl=300, auth=tok)

    f = once.get("formation") or {}
    titulares, suma = 0, 0
    for k in ("goalkeeper", "defender", "midfield", "striker"):
        v = f.get(k) or []
        for p in (v if isinstance(v, list) else [v]):
            titulares += 1
            for st in (p["playerMaster"].get("lastStats") or []):
                if int(st.get("weekNumber", 0)) == n:
                    suma += int(st.get("totalPoints") or 0)

    if titulares < 11:
        await enviar(
            f"🚨 <b>ONCE INCOMPLETO en la J{n}: {titulares} jugadores</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
            f"Con la plantilla incompleta la jornada NO puntúa. Llevas {suma} puntos "
            f"hechos y el marcador oficial dice {oficiales}.\n"
            f"Si la jornada aún no ha abierto, completa el once YA.",
            clave=f"once-incompleto:{n}", silencio=6 * 3600, nivel=EMERGENCIA, categoria="once_roto")
    elif suma > 0 and oficiales == 0:
        await enviar(
            f"🚨 <b>La J{n} no está puntuando</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
            f"Tu once suma {suma} puntos pero el marcador oficial es 0.",
            clave=f"jornada-muerta:{n}", silencio=12 * 3600, nivel=EMERGENCIA, categoria="once_roto")


# ---------- los detectores ----------
#
# Cada uno se ejecuta y falla por su cuenta (ver `_detectar`). Seguidos dentro de
# `vigilar()`, uno que lanza impide correr a los de abajo: un fallo leyendo las cláusulas
# bajadas se llevaría por delante "mi exposición", que es el que avisa de que te pueden
# robar, y la excepción subiría al scheduler sin decir qué detector la produjo.


async def _detectar(nombre: str, corrutina, por_defecto):
    """Ejecuta un detector sin que su fallo se lleve a los demás.

    Un detector que revienta es un bug, así que se registra con traza. Dejar de vigilar
    lo demás mientras tanto no ayuda a nadie.
    """
    try:
        return await corrutina
    except Exception:
        log.exception("vigilancia: el detector %r falló; los demás siguen", nombre)
        return por_defecto


async def _robos() -> tuple[list[dict], int]:
    """¿Me han quitado a alguien? Lo único que justifica sonar el móvil.

    Se cruzan dos vías. Comparar el último snapshot con el anterior es frágil: basta una
    copia cacheada o un snapshot intermedio para que el cambio desaparezca entre dos fotos
    consecutivas. El tablón lo declara, con un evento con `user2Id` = mi manager.
    """
    avisos = 0
    async with Session() as s:
        if not (await s.execute(_HAY_MANAGER_PROPIO)).scalar():
            log.error("sin manager propio en `managers`, la detección de robos NO "
                      "funciona: las dos vías devuelven cero. Pasa `ingest_league` antes "
                      "de fiarte de un 'robados: 0'")
            return [], 0
        perdidos = [dict(f._mapping) for f in await s.execute(_ME_LO_HAN_QUITADO)]
    robados = [p for p in perdidos if p["ladron"]]

    async with Session() as s:
        del_tablon = [dict(f._mapping) for f in
                      await s.execute(_ROBOS_DEL_TABLON, {"desde": hace(horas=12)})]
    ya = {str(p["jugador"]) for p in robados}
    for t in del_tablon:
        if str(t["jugador"]) not in ya:
            log.warning("robo detectado por el TABLÓN (los snapshots no lo vieron): %s",
                        t["jugador"])
            robados.append({"jugador": t["jugador"], "clausula": t["clausula"],
                            "valor": None, "ladron": t["ladron"]})

    for p in robados:
        texto = (
            f"🚨 <b>TE HAN QUITADO A {p['jugador']}</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
            f"   Se lo ha llevado <b>{p['ladron']}</b> por {_m(p['clausula'])}"
            f" (valía {_m(p['valor'])})\n"
            f"   Revisa si puedes alinear un once legal y con qué caja te quedas."
        )
        # Sin `clave`: un robo no se silencia por antirrebote, pasa una vez y es grave.
        if await enviar(texto, nivel=EMERGENCIA, categoria="robo"):
            avisos += 1

    for p in (x for x in perdidos if not x["ladron"]):
        # Salió de la plantilla sin dueño nuevo: venta al mercado. No es emergencia.
        log.info("%s ya no está en la plantilla (sin dueño nuevo: venta)", p["jugador"])

    if robados:
        log.error("ROBADOS: %s",
                  ", ".join(f"{p['jugador']} <- {p['ladron']}" for p in robados))
        await _reparar_once_tras_robo(robados[0]["jugador"])
    return robados, avisos


async def _reparar_once_tras_robo(jugador: str) -> None:
    """Avisar no basta: el once se repara aquí mismo.

    La alineación se congela en el `openingWeekDate`, y un titular robado deja el once en
    10, con lo que la jornada entera puntúa 0 (tipo 7). Si el robo cae de noche o con el
    candado puesto, nadie llega a tiempo a partir de un aviso.
    """
    try:
        from ..guardia_once import revisar as revisar_once
        r = await revisar_once(motivo=f"robo de {jugador}")
        if r.get("reparado"):
            log.warning("once reparado tras el robo: %s", r.get("sustituciones"))
    except Exception:
        log.exception("no se pudo reparar el once tras el robo")


# Los tipos con los que LaLiga declara que una jornada no ha puntuado. El 8 es el que
# menos se espera: quedarse en rojos anula la jornada entera, y es la segunda razón de
# ser de `reserva_intocable`. Los `tipo_N` son los mismos antes de traducirles el nombre.
_MOTIVO_ANULACION = {
    "no_puntua_once_incompleto": "el once estaba INCOMPLETO",
    "tipo_7": "el once estaba INCOMPLETO",
    "no_puntua_saldo_negativo": "tenías SALDO NEGATIVO",
    "tipo_8": "tenías SALDO NEGATIVO",
    "no_puntua_alineacion": "la alineación era INCORRECTA",
    "tipo_32": "la alineación era INCORRECTA",
}


# Una anulación deja de ser noticia pasados unos días: la jornada ya se perdió. La
# ventana es generosa para cubrir un fin de semana con el sistema caído, y el silencio
# dura mucho más que ella a propósito: así el evento sale de la ventana antes de que
# expire el antirrebote y no se puede volver a avisar.
HORAS_ANULACION_RECIENTE = 72
DIAS_SILENCIO_ANULACION = 30


async def _anulaciones_publicadas() -> int:
    """La confirmación oficial de que una jornada no ha puntuado, desde el tablón.

    LaLiga lo publica de madrugada, antes de que nadie mire el marcador. Solo avisa de las
    recientes: sin filtro de fecha, una anulación de hace semanas sale por el canal de
    emergencias como si acabara de pasar.
    """
    avisos = 0
    async with Session() as s:
        anulaciones = (await s.execute(text("""
            SELECT e.kind, e.raw->>'weekNumber' AS jornada, e.ts
            FROM league_events e JOIN managers m ON m.id = e.manager_id
            WHERE m.is_me AND e.kind IN ('no_puntua_once_incompleto',
                                         'no_puntua_saldo_negativo',
                                         'no_puntua_alineacion',
                                         'tipo_7', 'tipo_8', 'tipo_32')
              AND e.ts > :desde
            ORDER BY e.ts DESC LIMIT 3"""),
            {"desde": hace(horas=HORAS_ANULACION_RECIENTE)})).all()
    for kind, jornada, cuando in anulaciones:
        # `astimezone()` porque la base guarda en UTC y el juego va en hora de Madrid:
        # sin convertir, el aviso da una hora desplazada respecto a la del tablón.
        local = cuando.astimezone() if cuando.tzinfo else cuando
        texto = (f"🚨 <b>LaLiga confirma: la jornada {jornada} NO ha puntuado</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
                 f"   Motivo: {_MOTIVO_ANULACION.get(kind, kind)}.\n"
                 f"   Publicado en el tablón el {local:%d-%b a las %H:%M}.")
        if await enviar(texto, clave=f"anulada:{jornada}:{kind}",
                        silencio=DIAS_SILENCIO_ANULACION * 86400,
                        nivel=EMERGENCIA, categoria="once_roto"):
            avisos += 1
    return avisos


async def _clausulas_bajadas(saldo: int) -> tuple[list[dict], int]:
    """Cláusulas que un rival ha bajado, o sea que su jugador sale más barato.

    No hay ninguna ventana que obligue al rival a dejarla baja: puede volver a subirla
    cuando quiera. Lo que sí tiene hora es el clausulazo, que se cierra 24 h antes de la
    siguiente jornada (`030.01.17`).
    """
    avisos = 0
    async with Session() as s:
        bajadas = [dict(f._mapping) for f in await s.execute(_CAMBIOS_CLAUSULA)]
    for b in bajadas:
        if b["is_me"]:
            continue
        alcanzable = (b["ahora"] or 0) <= saldo
        # `clause_lock_until` es la protección del recién comprado (compra + 14 días) y
        # lo que impide es clausularlo, no que su dueño suba la cláusula. Tener fecha no
        # es estar protegido: puede estar vencida, así que se compara contra ahora.
        protegido = fecha(b["bloqueada_hasta"]) if b["bloqueada_hasta"] else None
        if protegido and ahora() < protegido:
            estado = f"protegido del clausulazo hasta {protegido:%d-%b %H:%M}"
        else:
            estado = "sin protección: se le puede clausular"
        texto = (
            f"🔻 <b>{b['rival']} ha BAJADO la cláusula de {b['jugador']}</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
            f"   {_m(b['antes'])} → <b>{_m(b['ahora'])}</b>"
            f"  (valor {_m(b['valor'])})\n"
            f"   {estado}\n"
            f"   Tu saldo: {_m(saldo)} → "
            + ("<b>PUEDES pagarla</b>" if alcanzable else "no te llega")
        )
        if await enviar(texto, clave=f"bajada:{b['rival']}:{b['jugador']}:{b['ahora']}",
                        categoria="clausula_rival"):
            avisos += 1
    return bajadas, avisos


async def _blindajes_rivales() -> int:
    """Quién ha blindado a quién.

    Un blindaje cuesta un anuncio y sale publicado, así que nadie blinda por gusto: marca
    al jugador que un rival teme perder. Si llega minutos después de un movimiento tuyo,
    te ha visto venir y te está cerrando la vía.
    """
    avisos = 0
    async with Session() as s:
        blindajes = [dict(f._mapping) for f in
                     await s.execute(_BLINDAJES, {"desde": hace(horas=24)})]
    for b in blindajes:
        if b["is_me"]:
            continue
        texto = (
            f"🛡️ <b>{b['rival']} ha BLINDADO a {b['jugador']}</b>\n"  # sin-rastro-ok: lo lee el usuario en Telegram
            f"   Ya no se le puede hacer clausulazo.\n"
            f"   Lo protege porque le duele perderlo: apúntalo como pieza clave suya."
        )
        if await enviar(texto, clave=f"blindaje:{b['rival']}:{b['jugador']}:{b['ts']}",
                        categoria="blindaje_rival"):
            avisos += 1
    return avisos


async def _chollos(saldo: int) -> int:
    """Rivales con la cláusula por debajo de su valor de mercado y a mi alcance."""
    avisos = 0
    for o in await objetivos(saldo, limite=5):
        if o["ratio"] is None or o["ratio"] >= 1.0:
            continue
        texto = (
            f"🎯 <b>Chollo: {o['jugador']}</b> ({o['rival']})\n"  # sin-rastro-ok: lo lee el usuario en Telegram
            f"   Cláusula {_m(o['clausula'])} · valor {_m(o['valor'])}"
            f" → pagas {o['ratio']:.2f}× su valor\n"
            f"   Tu saldo: {_m(saldo)}"
        )
        if await enviar(texto, clave=f"chollo:{o['jugador']}:{o['clausula']}",
                        categoria="chollo"):
            avisos += 1
    return avisos


async def _protecciones_que_caducan(saldo: int) -> int:
    """Los que salen de sus 14 días en 24 h: hay que llegar antes que los demás."""
    avisos = 0
    for libre in await proximas_liberaciones(dias=1, limite=5):
        if (libre["clausula"] or 0) > saldo or libre["ratio"] is None:
            continue
        texto = (
            f"⏰ <b>{libre['jugador']}</b> ({libre['rival']}) sale de protección\n"  # sin-rastro-ok: lo lee el usuario en Telegram
            f"   {libre['se_libera']:%d-%b %H:%M}\n"
            f"   Cláusula {_m(libre['clausula'])} · vale {_m(libre['valor'])}"
            f" → {libre['ratio']:.2f}× · tu saldo {_m(saldo)}"
        )
        if await enviar(texto, clave=f"libera:{libre['jugador']}:{libre['se_libera']}",
                        categoria="proteccion", silencio=20 * 3600):
            avisos += 1
    return avisos


async def _mi_exposicion(tope_rival: int, dudoso: bool) -> tuple[list[dict], int]:
    """Jugadores míos que el rival más rico podría pagar."""
    avisos = 0
    # La lista entera y se recorta DESPUÉS de filtrar: `mi_exposicion` aplica su `limite`
    # antes de descartar a los protegidos, así que un `limite=5` con cinco a salvo en
    # cabeza deja mudo el aviso teniendo a alguien expuesto de verdad.
    expuestos = await mi_exposicion(tope_rival, limite=30) if tope_rival else []
    caros = [e for e in expuestos if not e["bloqueada"] and (e["valor"] or 0) > 5e6]

    # Cruce con el blindaje real: la base va hasta 30 min por detrás. Con `None` se sigue
    # con la foto de la base (ver `blindados_ahora`).
    blindados = await blindados_ahora()
    if blindados is not None:
        caros = [e for e in caros if e["jugador"] not in blindados]
    else:
        log.warning("exposición: sin blindaje en vivo, se avisa con la foto de la base")

    if caros:
        lineas = [f"🛡️ <b>Expuesto</b>: el rival más rico puede pagar ~{_m(tope_rival)}"  # sin-rastro-ok: lo lee el usuario en Telegram
                  + (" (estimación poco fiable)" if dudoso else "")]
        for e in caros[:4]:
            lineas.append(f"• {e['jugador']}: cláusula {_m(e['clausula'])}"
                          f" (vale {_m(e['valor'])}, ×{e['ratio']:.2f})")
        lineas.append("Súbeles la cláusula si te importan.")
        clave = "exposicion:" + ",".join(str(e["jugador"]) for e in caros[:4])
        if await enviar("\n".join(lineas), clave=clave, silencio=24 * 3600,
                        categoria="proteccion"):
            avisos += 1
    return caros, avisos


async def vigilar() -> dict[str, int]:
    """Los detectores, en orden de urgencia y cada uno aislado del resto."""
    saldo = await mi_saldo()

    # 0) Lo primero y lo único urgente: ¿me han quitado a alguien? Va antes que nada
    #    porque es lo único que justifica sonar el móvil, y porque deja la plantilla
    #    corta: puede impedir alinear en la próxima jornada.
    robados, avisos = await _detectar("robos", _robos(), ([], 0))

    # 0bis) ¿Está puntuando la jornada? Un once incompleto la anula entera: los puntos
    #    se hacen igual pero el marcador oficial se queda a cero.
    await _detectar("jornada muerta", _revisar_jornada_muerta(), None)
    avisos += await _detectar("anulaciones publicadas", _anulaciones_publicadas(), 0)

    # Cuánto puede pagar el rival más rico: es la vara para medir mi exposición.
    saldos, _ = await estimar()
    rivales = [s for s in saldos if not s.es_mio and s.estimado is not None]
    tope_rival = max((s.estimado for s in rivales), default=0)
    dudoso = any(s.incompleto for s in rivales)

    bajadas, n = await _detectar("cláusulas bajadas", _clausulas_bajadas(saldo), ([], 0))
    avisos += n
    avisos += await _detectar("blindajes rivales", _blindajes_rivales(), 0)
    avisos += await _detectar("chollos", _chollos(saldo), 0)
    avisos += await _detectar("protecciones", _protecciones_que_caducan(saldo), 0)
    caros, n = await _detectar("mi exposición", _mi_exposicion(tope_rival, dudoso), ([], 0))
    avisos += n

    log.info("vigilancia: %d robados, %d bajadas, %d expuestos, %d avisos",
             len(robados), len(bajadas), len(caros), avisos)
    return {"robados": len(robados), "bajadas": len(bajadas),
            "expuestos": len(caros), "avisos": avisos}
