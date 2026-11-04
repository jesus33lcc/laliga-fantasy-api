"""Piloto automático: decide y (si procede) ejecuta.

Orden de las comprobaciones, de fuera hacia dentro. Cada capa puede abortar:

    1 invariantes en verde  ->  2 saldo y límites  ->  3 precondición contra el último
    snapshot  ->  4 sin puja propia viva  ->  5 acción (sin reintentos) y registro

Los números son los de los comentarios `Capa N` de `ejecutar()`. El kill switch no está
aquí: vive en `ClienteEducado`, así que corta cualquier petición, venga de donde venga.

La Capa 3 reconsulta el ÚLTIMO SNAPSHOT, no la API: descarta lo que ya consta (blindado,
protegido, cláusula distinta a la de la propuesta), pero lee la misma foto de la que salió
la propuesta, así que no ve un cambio de los últimos minutos. Quien relee contra LaLiga y
al euro es `/accion/clausulazo`, justo antes de pagar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from sqlalchemy import text

from .alerts import EMERGENCIA, enviar
from .clients.acciones import Acciones
from .clients.fantasy import FantasyAPI
from .db import Session
from .models.calidad import evaluar
from .models.emergencia import NOMBRES, plan_emergencia, plan_refuerzo
from .models.vigilancia import mi_saldo, objetivos
from .politica import (
    LIMITES,
    datos_fiables,
    es_real,
    modo,
    puede_gastar,
    registrar,
)
from .tiempo import hace

log = logging.getLogger("coconut.piloto")


@dataclass
class Propuesta:
    accion: str
    objetivo: str
    importe: int
    motivo: str
    # Con qué se reconsulta el snapshot en la Capa 3.
    player_id: int | None = None
    clausula_esperada: int | None = None
    market_id: str | None = None
    # El id de la ficha DENTRO del equipo rival, que es el que pide `pagar_clausula`.
    # Lo rellenan las dos ramas, no solo emergencia.
    player_team_id: str | None = None

    def __str__(self) -> str:
        return f"{self.accion} · {self.objetivo} · {self.importe/1e6:.1f}M, {self.motivo}"


_SIGUE_VALIDO = text("""
    SELECT s.buyout_clause, s.clause_lock_until, COALESCE(s.shielded, false)
    FROM squad_snapshots s
    WHERE s.ts = (SELECT max(ts) FROM squad_snapshots)
      AND s.player_id = :pid
""")


async def _precondicion_clausulazo(p: Propuesta) -> tuple[bool, str]:
    """Vuelve a mirar al jugador en el último snapshot antes de soltar el dinero."""
    async with Session() as s:
        fila = (await s.execute(_SIGUE_VALIDO, {"pid": p.player_id})).first()
    if not fila:
        return False, "el jugador ya no aparece en ninguna plantilla"
    clausula, bloqueo, blindado = int(fila[0] or 0), fila[1], bool(fila[2])
    if blindado:
        return False, "está blindado"
    if bloqueo is not None:
        from datetime import datetime
        if bloqueo > datetime.now(UTC):
            return False, f"protegido hasta {bloqueo:%d-%b %H:%M}"
    if p.clausula_esperada is not None and clausula != p.clausula_esperada:
        return False, (f"la cláusula cambió: esperaba {p.clausula_esperada/1e6:.1f}M "
                       f"y ahora es {clausula/1e6:.1f}M")
    # Esto filtra contra la BASE, no contra la API: lee el mismo snapshot del que salió
    # la propuesta, así que descarta lo que ya se sabe (blindado, protegido, cláusula
    # distinta a la de hace un ciclo) y no ve un cambio de los últimos minutos. El importe
    # exacto al euro lo relee `/accion/clausulazo` justo antes de pagar, que es por donde
    # se ejecuta.
    p.importe = clausula
    return True, "sigue válida"


async def _clausulazo(p: Propuesta, acciones: Acciones) -> dict:
    """Paga la cláusula por `/accion/clausulazo`, no contra LaLiga directamente.

    La precondición de arriba mira la base. `/accion/clausulazo` relee la cláusula
    contra la API y al euro (409 `030.01.49` si redondeas), y además pasa por
    `puede_gastar`, el lock de caja y el registro en `decisions`.

    si la API interna no
    responde, se ejecuta con el importe que trae la propuesta. Puede salir un 409 por
    cláusula desfasada, pero un 409 se ve; dejar el clausulazo sin lanzar en una ventana
    que dura horas, no.

    El `player_team_id` solo lo necesita ese respaldo: `/accion/clausulazo` recibe el
    nombre y resuelve el id. Por eso no condiciona la ejecución, y si falta se lanza aquí
    en vez de pedir `/buyout/None/pay`.
    """
    from .clients.via_api import accion as accion_api
    # `/accion/*` marca lo que recibe con `orden_explicita()`, y eso hace que `es_real()`
    # sea cierto AL OTRO LADO aunque el modo sea `agente`. Esa marca está para el humano
    # que pulsa un botón; el piloto no lo es. Sin esta guarda, aquí se registra "SOMBRA
    # (no ejecutado)" mientras la API paga la cláusula: dos procesos contestando distinto
    # a la misma pregunta. `pujar` y `ofertar` no la necesitan porque deciden en este
    # proceso, donde la marca no está puesta.
    if not es_real():
        return {"ok": False, "sombra": True}
    try:
        return await accion_api("clausulazo",
                                {"jugador": p.objetivo.split(" (")[0],
                                 "importe": int(p.importe), "confirmar": True,
                                 "importe_confirmado": int(p.importe)})
    except Exception as e:
        log.warning("clausulazo por /accion falló (%s); se va por el cliente directo",
                    type(e).__name__)
        if not p.player_team_id:
            raise RuntimeError(
                f"sin player_team_id para {p.objetivo}: el respaldo directo no puede "
                f"pagar la cláusula y /accion/clausulazo no respondió") from e
        return await acciones.pagar_clausula(p.player_team_id, p.importe)


async def proponer() -> list[Propuesta]:
    """Qué haría el piloto ahora mismo. No ejecuta nada."""
    saldo = await mi_saldo()
    propuestas: list[Propuesta] = []

    # EMERGENCIA primero: si no se puede alinear, eso manda sobre cualquier chollo.
    # Una plaza vacia cuesta la jornada entera; un fichaje caro cuesta dinero, que se
    # recupera. Por eso la emergencia se salta el filtro de ratio del piloto normal.
    for e in await plan_emergencia(saldo):
        h = e["hueco"]
        if not e.get("via"):
            log.error("hueco de %s SIN SOLUCION: %s", h, e.get("motivo"))
            await enviar(f"🚨 <b>No puedo alinear</b>\n{h}\n{e.get('motivo')}", categoria="once_roto",  # sin-rastro-ok: lo ve el usuario
                         nivel=EMERGENCIA,
                         clave=f"sin-solucion:{h.posicion}", silencio=6 * 3600)
            continue
        c = e["candidato"]
        if e["via"] != "mercado":
            accion = "emergencia_clausula"
        elif e.get("garantizado"):
            accion = "emergencia_puja"        # agente libre: la subasta resuelve sola
        else:
            accion = "emergencia_oferta"      # lo vende un rival: puede decir que no

        aviso = ""
        if e.get("solo_hay_de_rival"):
            aviso = ("NO hay ningún agente libre en esa posición: esto es una oferta a "
                     "otro manager, que puede rechazarla o dejarla caducar, así que el "
                     "hueco puede seguir abierto mañana")

        propuestas.append(Propuesta(
            accion=accion,
            objetivo=f"{c['name']} ({e['via']})",
            importe=int(e["importe"]),
            motivo=(f"🚨 {h}: sin esto no hay once válido. "  # sin-rastro-ok: lo ve el usuario
                    f"{c.get('puntos') or '?'} pts · "
                    f"{int((c.get('prob_titular') or 0)*100)}% titular · "
                    f"{e['alternativas']} alternativas{aviso}"),
            player_id=int(c.get("player_id") or c.get("id") or 0) or None,
            market_id=str(c.get("market_id")) if c.get("market_id") else None,
            player_team_id=str(c["player_team_id"]) if c.get("player_team_id") else None,
        ))

    # Refuerzos: posiciones cubiertas de número pero que puntúan cero. No son urgentes,
    # así que van solo a agente libre, donde la subasta resuelve sola. Una oferta a otro
    # manager depende de que acepte, y para algo que puede esperar a la baraja de mañana
    # eso es cambiar una compra segura por un quizá.
    for r in await plan_refuerzo(saldo):
        c = r["candidato"]
        propuestas.append(Propuesta(
            accion="refuerzo_puja",
            objetivo=f"{c['name']} (agente libre)",
            importe=int(r["importe"]),
            motivo=(f"⬆️ {NOMBRES.get(r['posicion'], '?')}: tu mejor titular sale "  # sin-rastro-ok: lo ve el usuario
                    f"{int(r['prob_actual']*100)}% de las jornadas. "
                    f"{c.get('puntos') or '?'} pts · "
                    f"{int((c.get('prob_titular') or 0)*100)}% titular · "
                    f"{r['alternativas']} opciones libres"),
            player_id=int(c.get("player_id") or 0) or None,
            market_id=str(c.get("market_id")) if c.get("market_id") else None,
        ))

    # Clausulazos: tienen que pasar dos filtros, precio y calidad. El de precio solo
    # dice que es barato; el de calidad, que va a puntuar.
    for o in await objetivos(saldo, limite=25):
        ratio = o["ratio"]
        if ratio is None or ratio > LIMITES.max_ratio_compra:
            continue
        cal = evaluar(o["jugador"], o.get("status"), o.get("puntos_anteriores"),
                      o.get("position"), o.get("prob_titular"))
        if not cal:
            log.info("descartado por calidad (%s): %s", cal.motivo, o["jugador"])
            continue
        if cal.nota < LIMITES.min_nota_calidad:
            log.info("nota de calidad baja (%.2f): %s", cal.nota, o["jugador"])
            continue
        propuestas.append(Propuesta(
            accion="clausulazo",
            objetivo=f"{o['jugador']} ({o['rival']})",
            importe=int(o["clausula"]),
            motivo=(f"pagas {ratio:.2f}× su valor ({o['valor']/1e6:.1f}M) · "
                    f"calidad {cal.nota:.2f}, {cal.motivo}"),
            player_id=int(o["player_id"]),
            player_team_id=str(o["player_team_id"]) if o.get("player_team_id") else None,
            clausula_esperada=int(o["clausula"]),
        ))

    # Ordenar solo por importe contradice el "EMERGENCIA primero" de arriba: con
    # `max_operaciones_dia` en 3, tres clausulazos baratos se llevan el cupo y el hueco
    # que anula la jornada se queda sin cubrir. El prefijo `emergencia` es el mismo
    # criterio que usa `politica.py` para relajar los topes.
    propuestas.sort(key=lambda x: (not x.accion.startswith("emergencia"), x.importe))
    return propuestas


# Las subastas de agente libre se resuelven a las 13:48 del día siguiente, así que 30 h
# cubren de sobra el ciclo de una puja viva. Se compara por el NOMBRE del jugador (lo que
# va antes del paréntesis) porque el market_id no se guarda en `decisions`, y una ficha
# puede cambiar de id entre barajas mientras la puja sigue en pie.
_PUJA_VIVA = text("""
    SELECT count(*) FROM decisions
    WHERE accion IN ('puja', 'refuerzo_puja', 'emergencia_puja')
      AND ejecutada
      AND ts > :desde
      -- `split_part` es de Postgres. El objetivo se guarda como "Nombre (equipo)",
      -- así que basta con comparar el nombre exacto o el prefijo con paréntesis:
      -- mismo resultado y vale en los dos motores.
      AND (objetivo = :jugador OR objetivo LIKE :jugador_prefijo)
""")


async def _ya_pujado(objetivo: str) -> bool:
    jugador = objetivo.split(" (")[0].strip()
    try:
        async with Session() as s:
            n = (await s.execute(_PUJA_VIVA, {
                "jugador": jugador, "jugador_prefijo": f"{jugador} (%",
                "desde": hace(horas=30)})).scalar()
        return bool(n)
    except Exception:
        # Ante la duda no se bloquea la operación: se registra y se sigue.
        log.exception("no se pudo comprobar si ya había puja por %s", jugador)
        return False


async def ejecutar(api: FantasyAPI, token: str, propuestas: list[Propuesta] | None = None,
                   ) -> dict[str, Any]:
    """Recorre las propuestas y ejecuta las que superen todas las comprobaciones."""
    resumen = {"modo": modo(), "propuestas": 0, "ejecutadas": 0, "rechazadas": 0}

    # Capa 1: ¿los datos son de fiar? Si los invariantes están en rojo, no se toca nada.
    fiable = await datos_fiables()
    if not fiable:
        log.error("piloto detenido: %s", fiable.motivo)
        await registrar("abortar", "-", 0, fiable.motivo, ejecutada=False)
        await enviar(f"🛑 <b>Piloto detenido</b>\n{fiable.motivo}", clave="piloto:abortado", categoria="sistema")  # sin-rastro-ok: lo ve el usuario
        return resumen | {"abortado": fiable.motivo}

    propuestas = propuestas if propuestas is not None else await proponer()
    resumen["propuestas"] = len(propuestas)
    if not propuestas:
        log.info("piloto: nada que hacer")
        return resumen

    saldo = await mi_saldo()
    acciones = Acciones(api.http, token)

    for p in propuestas:
        # Capa 2: límites de gasto.
        permiso = await puede_gastar(p.importe, saldo,
                                     emergencia=p.accion.startswith("emergencia"))
        if not permiso:
            log.info("descartada (%s): %s", permiso.motivo, p)
            await registrar(p.accion, p.objetivo, p.importe,
                            f"{p.motivo} | RECHAZADA: {permiso.motivo}", ejecutada=False)
            resumen["rechazadas"] += 1
            continue

        # Capa 3: releer la realidad justo antes de actuar.
        # Aplica a toda acción que pague una cláusula, emergencia incluida: la API exige
        # el importe exacto al euro (409 `030.01.49` si no cuadra) y rechaza a los
        # blindados, así que saltarse esto en una emergencia es perder la operación.
        if p.accion in ("clausulazo", "emergencia_clausula") and p.player_id is not None:
            vale, porque = await _precondicion_clausulazo(p)
            if not vale:
                log.info("precondición fallida (%s): %s", porque, p)
                await registrar(p.accion, p.objetivo, p.importe,
                                f"{p.motivo} | RECHAZADA: {porque}", ejecutada=False)
                resumen["rechazadas"] += 1
                continue

        # Capa 4: ¿ya hay una puja mía viva por este jugador?
        #
        # La API no lo dice: `numberOfBids` cuenta todas las pujas, la propia incluida,
        # así que un objetivo ya pujado sigue apareciendo como candidato. En modo real eso
        # sería pujar dos veces por lo mismo.
        if (p.accion in ("emergencia_puja", "refuerzo_puja") and p.market_id
                and await _ya_pujado(p.objetivo)):
            log.info("ya hay una puja viva por %s; no se duplica", p.objetivo)
            continue

        # Capa 5: ejecutar (o simular).
        try:
            if p.accion in ("emergencia_puja", "refuerzo_puja") and p.market_id:
                res = await acciones.pujar(p.market_id, p.importe)
            elif p.accion == "emergencia_oferta" and p.player_team_id:
                # `direct-offer` quiere el `playerTeamId`, no el id del maestro. El dueño
                # acepta o rechaza, así que esto no garantiza tapar el hueco.
                res = await acciones.ofertar(p.player_team_id, p.importe)
            elif p.accion in ("clausulazo", "emergencia_clausula"):
                res = await _clausulazo(p, acciones)
            else:
                # Sin ejecutor no es una simulación: es una propuesta que no debería
                # haberse generado. Tratarlo como sombra la contaría entre las
                # ejecutadas y le restaría el importe al presupuesto del día, así que una
                # propuesta imposible gastaría cupo sin mover un euro.
                log.error("propuesta sin ejecutor: %s · %s (faltan ids o acción "
                          "desconocida)", p.accion, p.objetivo)
                await registrar(p.accion, p.objetivo, p.importe, p.motivo,
                                ejecutada=False, resultado="sin ejecutor")
                resumen["rechazadas"] += 1
                continue

            ejecutada = es_real() and not res.get("sombra")
            # El saldo baja también en sombra: la simulación tiene que toparse con los
            # mismos límites que el modo real, o no sirve para decidir si fiarse de ella.
            saldo -= p.importe
            await registrar(p.accion, p.objetivo, p.importe, p.motivo,
                            ejecutada=ejecutada, resultado=str(res)[:400])
            resumen["ejecutadas"] += 1

            etiqueta = "EJECUTADO" if ejecutada else "SOMBRA (no ejecutado)"
            await enviar(f"{etiqueta}\n<b>{p.accion}</b>: {p.objetivo}\n"
                         f"Importe: {p.importe/1e6:.1f}M · saldo tras: {saldo/1e6:.1f}M\n"
                         f"{p.motivo}",
                         clave=f"piloto:{p.accion}:{p.objetivo}")
        except Exception as e:
            # Sin reintentos: una escritura fallida puede haberse aplicado igualmente.
            log.exception("fallo al ejecutar %s", p)
            await registrar(p.accion, p.objetivo, p.importe, p.motivo,
                            ejecutada=False, resultado=f"ERROR: {e}")
            await enviar(f"❌ <b>Fallo ejecutando</b> {p.accion} · {p.objetivo}\n{e}\n"  # sin-rastro-ok: lo ve el usuario
                         f"⚠️ Comprueba a mano si se llegó a aplicar.",  # sin-rastro-ok: lo ve el usuario
                         clave=f"piloto:error:{p.objetivo}", nivel=EMERGENCIA,
                         categoria="sistema")
            resumen["rechazadas"] += 1
            break  # ante un error, no se encadenan más operaciones

    return resumen
