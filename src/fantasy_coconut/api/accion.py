"""`/accion/*`: ejecutar operaciones de liga, con las mismas guardas que los scripts.

La mayoría reciben el jugador por nombre; las de mercado, un id de ficha. Guardas que
aplica esta capa:

  1. `_resolver()` busca el nombre en la plantilla propia, en las rivales y en el
     mercado, y saca el `playerTeamId` y el estado.
  2. Se relee el estado contra la API antes de ejecutar. Ojo: `_resolver` lee la
     plantilla con el ttl por defecto (900 s), así que la cláusula puede venir de hace
     un rato. El importe exacto lo vuelve a comprobar LaLiga con 409 `030.01.49`.
  3. `?dry=true` devuelve qué se haría. Lo aceptan todas salvo `fijar-once`.
  4. `Idempotency-Key`: repetir la misma petición devuelve el resultado guardado, porque
     reintentar una puja la duplica. La caché es en memoria (ver `_VISTAS`).
  5. `politica.puede_gastar` en lo que gasta: `pujar`, `ofertar`, `clausulazo` y
     `subir-clausula`. Los cobros, el blindaje y la recompensa no pasan por ahí.
     Hay dos escotillas distintas: `/clausulazo` acepta `?forzar=true`, que salta el
     veredicto entero; y en `pujar` y `ofertar` repetir la cifra en `importe_confirmado`
     pasa `orden_humana=True`, que levanta el tope por operación y el diario. El único
     donde un 409 de límites es definitivo es `subir-clausula`.
  6. Registro en `decisions`, salvo `alineacion` y `fijar-once`, que no pasan por
     `_ejecutar` y por eso tampoco llevan idempotencia ni candado de dispositivo.

En modo `agente`, las seis acciones que MUEVEN dinero exigen además `confirmar: true` en
el cuerpo: `pujar`, `ofertar`, `clausulazo`, `subir-clausula`, `venta-inmediata` y
`aceptar-oferta`. Las dos últimas cobran, no gastan, y van igual: el jugador se va.
"""

from __future__ import annotations

import logging
import time
import unicodedata
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query
from pydantic import BaseModel, Field

from ..clients.acciones import Acciones, ErrorDeLaLiga, cuerpo_de
from ..config import settings
from ..locks import BloqueoOcupado, lock_caja, lock_jugador
from ..politica import REAL, modo, orden_explicita, puede_gastar, registrar
from .deps import api, bearer, cliente, protegido

log = logging.getLogger("coconut.api.accion")


def _mil(n: int) -> str:
    """7873663 -> "7.873.663". Formatear aparte evita que un `.replace(',', '.')`
    sobre la frase entera se coma también las comas del texto."""
    return f"{int(n):,}".replace(",", ".")
router = APIRouter(prefix="/accion", tags=["acciones"])

CMP = "/v1/competition/1"

# Idempotencia en memoria. Se pierde al reiniciar, y es un compromiso consciente: cubre
# el caso real (un reintento por timeout o un doble clic en segundos), no un reinicio.
# Si algún día hace falta que sobreviva, va a tabla.
#
# Cuánto ocupa: solo entran peticiones CON cabecera, y cada una purga lo vencido, así que
# el tamaño es el de las claves distintas de la última hora. Lo único que se queda es lo
# que hubiera dentro cuando dejan de llegar claves, y para eso purgan las dos funciones y
# no solo la de lectura: si solo purgara `_idem()`, un proceso que guarda y luego deja de
# recibir cabeceras se quedaría esas entradas hasta reiniciar.
_VISTAS: dict[str, tuple[float, Any]] = {}
_TTL_IDEMPOTENCIA = 3600.0


def _purgar_idem() -> None:
    ahora = time.time()
    for k, (t, _) in list(_VISTAS.items()):
        if ahora - t > _TTL_IDEMPOTENCIA:
            _VISTAS.pop(k, None)


def _idem(clave: str | None):
    if not clave:
        return None
    _purgar_idem()
    return _VISTAS.get(clave, (None, None))[1]


def _guardar_idem(clave: str | None, resultado: Any) -> None:
    if clave:
        _purgar_idem()
        _VISTAS[clave] = (time.time(), resultado)


def _norm(t: str) -> str:
    """Sin acentos ni mayúsculas: escribir "Alex" debe encontrar "Álex"."""
    return "".join(c for c in unicodedata.normalize("NFD", (t or "").strip().lower())
                   if unicodedata.category(c) != "Mn")


def _casa(objetivo: str, *nombres: str) -> int:
    """0 = no casa · 1 = coincide por dentro · 2 = exacto. El exacto siempre gana."""
    o = _norm(objetivo)
    for n in nombres:
        if _norm(n) == o:
            return 2
    for n in nombres:
        if o and o in _norm(n):
            return 1
    return 0


# Desde este importe hay que repetirlo en `importe_confirmado`. `politica.puede_gastar`
# solo comprueba que quepa en la caja, así que un cero de más (7.100.000 -> 71.000.000)
# pasaría sus filtros sin avisar.
UMBRAL_DOBLE_CONFIRMACION = 10_000_000


class Objetivo(BaseModel):
    jugador: str = Field(..., description="Nombre o apodo; no hace falta el id")
    importe: int | None = Field(None, description="En euros. Opcional según la acción")
    confirmar: bool = Field(
        False, description="Obligatorio en todo lo que mueve dinero, cobros incluidos")
    importe_confirmado: int | None = Field(
        None, description="Repite el importe si llega a "
                          f"{UMBRAL_DOBLE_CONFIRMACION//10**6}M (el umbral entra)")


def _revisa_importe(cuerpo: Objetivo, importe: int, dry: bool = False,
                    pedido: int | None = None) -> None:
    """Exige repetir el importe en `importe_confirmado` si llega a `UMBRAL_DOBLE_CONFIRMACION`.

    Lanza 428 si no coincide. Protege del error de tecleo (un cero de más), no de un
    acceso no autorizado.

    `pedido` es el importe tal como llegó, para los endpoints que lo ajustan después.
    Entonces vale como confirmación cualquiera de los dos: una puja subida al valor de
    mercado se paga con una cifra que nadie ha tecleado, y exigir esa exacta obligaría a
    repetir la llamada solo para enterarse de cuál es. El ajuste está acotado al 5 %, así
    que por ese hueco no cabe un cero de más.

    No se aplica con `dry=True`: en un clausulazo el importe exacto no se conoce hasta que
    se relee la cláusula al euro, así que exigirlo para *preguntar* cuánto es haría
    inservible la simulación. Un `dry` no gasta: corta en `_ejecutar` antes de llamar a la
    API.
    """
    if dry:
        return
    valen = {importe} | ({pedido} if pedido else set())
    if importe >= UMBRAL_DOBLE_CONFIRMACION and cuerpo.importe_confirmado not in valen:
        raise HTTPException(
            428, f"{importe/1e6:.2f}M supera el umbral de doble confirmación: repite el "
                 f"importe exacto en `importe_confirmado` ({importe})")


async def _resolver(nombre: str) -> dict:
    """Busca al jugador en mi plantilla, en las rivales y en el mercado.

    Devuelve todo lo que hace falta para operar: `playerTeamId`, dueño, cláusula, si está
    blindado o protegido, y (si está en el mercado) su `market_id`, `discr` y valor.
    """
    tok = await bearer()
    liga, yo = settings.fantasy_league_id, settings.fantasy_team_id
    objetivo = nombre.strip().lower()

    equipos = await protegido(
        cliente().get(f"{CMP}/leagues/{liga}/teams", ttl=300, auth=tok))
    candidatos = []
    for t in equipos:
        for p in t.get("players", []):
            pm = p.get("playerMaster", {})
            grado = _casa(objetivo, pm.get("nickname"), pm.get("name"))
            if grado:
                candidatos.append((grado, {
                    "nombre": pm.get("nickname"), "player_id": pm.get("id"),
                    "player_team_id": p.get("playerTeamId"),
                    "posicion": pm.get("positionId"),
                    "valor": int(pm.get("marketValue") or 0),
                    "clausula": int(p.get("buyoutClause") or 0),
                    "protegido_hasta": p.get("buyoutClauseLockedEndTime"),
                    "dueno_id": str(t.get("id")),
                    "dueno": (t.get("manager") or {}).get("managerName"),
                    "es_mio": str(t.get("id")) == str(yo),
                }))

    # El grado se compara entre las dos fuentes a la vez. Si se resolviera primero la
    # plantilla, un "Marcos Alonso" encontrado por subcadena ganaría al "Marcos" exacto
    # del mercado, y la acción saldría sobre el jugador equivocado sin dar error.
    fichas = await protegido(api().mercado(tok, liga))
    en_mercado = []
    for it in (fichas if isinstance(fichas, list) else fichas.get("data", [])):
        pm = it.get("playerMaster", {})
        grado = _casa(objetivo, pm.get("nickname"), pm.get("name"))
        if grado:
            en_mercado.append((grado, it, pm))

    grados = [g for g, _ in candidatos] + [g for g, _, _ in en_mercado]
    if not grados:
        raise HTTPException(404, f"no encuentro a «{nombre}» ni en las plantillas ni en el mercado")
    mejor = max(grados)
    candidatos = [(g, c) for g, c in candidatos if g == mejor]
    en_mercado = [x for x in en_mercado if x[0] == mejor]

    # Ambigüedad: en una acción que mueve dinero, elegir al azar es inaceptable.
    ids = {str(c["player_id"]) for _, c in candidatos} | \
          {str(pm.get("id")) for _, _, pm in en_mercado}
    if len(ids) > 1:
        raise HTTPException(409, {
            "error": f"«{nombre}» es ambiguo",
            "candidatos": [{"nombre": c["nombre"], "id": c["player_id"],
                            "dueno": c.get("dueno")} for _, c in candidatos]
                          + [{"nombre": pm.get("nickname"), "id": pm.get("id"),
                              "dueno": "(mercado)"} for _, _, pm in en_mercado]})

    encontrado = candidatos[0][1] if candidatos else None

    # El agregado no trae `isShielded`: para eso hace falta la plantilla individual.
    if encontrado:
        ind = await protegido(api().plantilla(tok, liga, encontrado["dueno_id"]))
        for p in ind.get("players", []):
            if str(p.get("playerTeamId")) == str(encontrado["player_team_id"]):
                encontrado["blindado"] = bool(p.get("isShielded"))
                encontrado["blindado_hasta"] = p.get("shieldedEndDate")
                encontrado["clausula"] = int(p.get("buyoutClause") or 0)

    if en_mercado:
        _, it, pm = en_mercado[0]
        mercado = {
            "market_id": str(it.get("id")), "discr": it.get("discr"),
            "precio_salida": int(it.get("salePrice") or 0),
            "valor": int(pm.get("marketValue") or 0),
            "pujas": it.get("numberOfBids"), "ofertas": it.get("numberOfOffers"),
            "expira": it.get("expirationDate"),
            "es_agente_libre": it.get("discr") == "marketPlayerLeague",
        }
        if encontrado:
            encontrado["mercado"] = mercado
        else:
            encontrado = {"nombre": pm.get("nickname"), "player_id": pm.get("id"),
                          "posicion": pm.get("positionId"), "es_mio": False,
                          "valor": mercado["valor"], "mercado": mercado}

    if not encontrado:
        raise HTTPException(404, f"no encuentro a «{nombre}» ni en las plantillas ni en el mercado")
    return encontrado


def _exige_confirmacion(cuerpo: Any) -> None:
    """En `agente` nada que mueva dinero sale sin un sí explícito.

    Vale para cualquier cuerpo con `confirmar`, no solo `Objetivo`: `aceptar-oferta`
    recibe un `Oferta`.

    No basta con el umbral de `importe_confirmado`, que solo salta por encima de 10 M:
    la venta inmediata cobra la MITAD del valor, así que con una plantilla normal no
    llega casi nunca. Un jugador de 2,92 M se vendería por 1,46 M sin que ninguna de las
    dos guardas dijera nada.
    """
    if modo() != REAL and not cuerpo.confirmar:
        raise HTTPException(
            428, "esta acción mueve dinero: manda `confirmar: true` en el cuerpo "
                 f"(modo actual: {modo()})")


async def _ejecutar(accion: str, objetivo: str, importe: int, motivo: str, coro,
                    dry: bool, idem: str | None):
    previo = _idem(idem)
    if previo is not None:
        return {"idempotente": True, "resultado": previo}
    if dry:
        return {"dry_run": True, "haria": {"accion": accion, "objetivo": objetivo,
                                           "importe": importe, "motivo": motivo}}

    # ¿Manda este dispositivo? Con dos instalaciones (un servidor y el móvil) el riesgo no es
    # que se pisen los datos, es que las dos actúen: dos pujas distintas con la misma
    # caja. Va después del `dry` a propósito: mirar qué haría una operación no es actuar,
    # y poder consultar desde el móvil aunque mande el servidor es lo que hace usable
    # tener dos. Sin repositorio de copia configurado no hay mando y no bloquea nada.
    from ..dispositivo import puedo_actuar
    puedo, porque = await puedo_actuar()
    if not puedo:
        raise HTTPException(423, porque)

    try:
        # Una llamada a `/accion/*` la ha pedido una persona: se ejecuta de verdad
        # aunque `COCONUT_MODO` sea `agente`, que está para frenar al piloto, no a ti.
        with orden_explicita():
            res = await coro
    except PermissionError as e:      # este aparato no tiene el mando (ver `dispositivo.py`)
        raise HTTPException(403, str(e)) from e
    except ErrorDeLaLiga as e:
        # LaLiga rechazó por una regla del juego. Devolverlo como 500 diría "el sistema
        # está roto" y mandaría a mirar el log para nada.
        await registrar(accion, objetivo, importe, motivo, ejecutada=False,
                        resultado=f"[{e.codigo or e.status}] {e}"[:300])
        raise HTTPException(e.status, f"LaLiga lo rechaza: {e}"
                                      + (f" ({e.codigo})" if e.codigo else "")) from e
    if res.get("sombra"):
        # Nunca devolver "hecho" sin haber hecho nada. Una operación registrada como
        # ejecutada que se simuló deja creer que hay una puja viva o un once guardado.
        raise HTTPException(
            503, f"NO se ha ejecutado: quedó simulado (COCONUT_MODO={modo()}). "
                 f"Es un fallo del freno, no una decisión: avísalo.")
    ok = bool(res.get("ok"))
    await registrar(accion, objetivo, importe, motivo, ejecutada=ok,
                    resultado=str(res)[:300])
    salida = {"ok": ok, "status": res.get("status"), "detalle": res}
    _guardar_idem(idem, salida)
    return salida


# ---------------------------------------------------------------- blindaje

@router.post("/blindar", summary="Blinda a un jugador propio (24 h)")
async def blindar(cuerpo: Objetivo, dry: bool = Query(False),
                  autorenovar: bool = Query(False),
                  idempotency_key: str | None = Header(None)):
    """Con `autorenovar` deja creada la siguiente vuelta, igual que `POST /programar`.

    Sin él, blindar ahora es un disparo suelto: el jugador queda libre al caducar y nadie
    se entera. Las dos vías comparten `_reprogramar_auto()`, que es quien sabe leer
    `shieldedEndDate` y aplazar la vuelta si el cupo de la jornada está gastado.

    No encadena si la respuesta no es un blindaje nuevo. En `dry` no se ha puesto nada, y
    con `sin_efecto` el blindaje vivo lo puso otra llamada, que es la que lleva su cadena
    si se la pidieron.
    """
    j = await _resolver(cuerpo.jugador)
    if not j.get("es_mio"):
        raise HTTPException(400, f"{j['nombre']} no es tuyo")
    if j.get("blindado"):
        return {"ok": True, "sin_efecto": True,
                "detalle": f"{j['nombre']} ya está blindado hasta {j.get('blindado_hasta')}"}
    acc = Acciones(cliente(), await bearer())
    try:
        async with lock_jugador(str(j["player_team_id"])):
            salida = await _ejecutar("blindaje", j["nombre"], 0, "blindaje por API",
                                     acc.blindar(j["player_team_id"]), dry, idempotency_key)
    except BloqueoOcupado as e:
        raise HTTPException(409, str(e)) from e

    if autorenovar and not dry and salida.get("ok"):
        # El import va dentro del `if`: `programar` importa este módulo para ejecutar las
        # tareas, así que a nivel de fichero sería circular. Aquí solo se carga cuando de
        # verdad se pide encadenar.
        from .programar import _reprogramar_auto
        try:
            await _reprogramar_auto("una ejecución inmediata", "blindar",
                                    {"jugador": j["nombre"], "autorenovar": True}, salida)
            salida["autorenovar"] = True
        except Exception:
            # El blindaje ya está puesto: que falle la cadena no lo deshace ni convierte
            # la respuesta en un error. Se dice en la respuesta y se sigue.
            log.exception("blindaje de %s puesto, pero no se pudo encadenar", j["nombre"])
            salida["autorenovar"] = "falló al encadenar, mira el log"
    return salida


@router.post("/recompensa", summary="Cobra la recompensa diaria (100.000 €)")
async def recompensa(dry: bool = Query(False),
                     idempotency_key: str | None = Header(None)):
    """No lleva los frenos de `politica.py` (y no es un olvido): esto ingresa dinero,
    no lo gasta, así que no hay tope que aplicar. Lo que gana al pasar por aquí es el
    token bucket compartido y el contador de peticiones, para que el número que enseña
    Coconut sea el de verdad.

    Que el límite diario ya esté consumido no es un error, es el caso normal de la
    segunda pasada del día: se devuelve `sin_efecto` en vez de un 500.
    """
    def ya_cobrada(texto: str) -> bool:
        return "050.01.04" in texto or "limit reached" in texto.lower()

    acc = Acciones(cliente(), await bearer())
    if dry:
        return {"dry_run": True, "haria": {"accion": "recompensa_diaria",
                                           "importe": 100_000}}
    try:
        return await _ejecutar("recompensa_diaria", "recompensa diaria", 0,
                               "100.000 €/día", acc.cobrar_recompensa_diaria(),
                               False, idempotency_key)
    except HTTPException as e:
        # `_ejecutar` ya convirtió el `ErrorDeLaLiga` en HTTPException, y el `errorCode`
        # viaja dentro de `detail`. Hay que mirarlo AQUÍ: relanzar sin comprobarlo deja
        # la rama de abajo inalcanzable y el `sin_efecto` que promete el docstring no se
        # devuelve nunca. Se nota en el otro extremo, donde `job_recompensa` pregunta por
        # una clave que no puede llegar.
        if ya_cobrada(str(e.detail)):
            return {"ok": True, "sin_efecto": True,
                    "detalle": "la recompensa de hoy ya estaba cobrada"}
        raise
    except Exception as e:
        # httpx no mete el cuerpo en el mensaje de la excepción: hay que mirar
        # `response.text` o "ya cobrada" se registra como avería.
        if ya_cobrada(cuerpo_de(e) or str(e)):
            return {"ok": True, "sin_efecto": True,
                    "detalle": "la recompensa de hoy ya estaba cobrada"}
        raise


# ---------------------------------------------------------------- mercado

@router.post("/pujar", summary="Puja por un AGENTE LIBRE")
async def pujar(cuerpo: Objetivo, dry: bool = Query(False),
                idempotency_key: str | None = Header(None)):
    _exige_confirmacion(cuerpo)
    j = await _resolver(cuerpo.jugador)
    mk = j.get("mercado")
    if not mk:
        raise HTTPException(400, f"{j['nombre']} no está en el mercado")
    if not mk["es_agente_libre"]:
        # `/market/{id}/bid` solo existe para agentes libres: sobre una ficha de rival
        # responde 404. La ruta de esa es `direct-offer`, o sea `/accion/ofertar`.
        raise HTTPException(400, f"{j['nombre']} lo vende un manager, no es agente libre: "
                                 "por aquí LaLiga responde 404. Usa /accion/ofertar.")
    importe = cuerpo.importe or mk["valor"]
    # El valor cambia mientras la puja está programada, y la puja tiene que ser mayor
    # o igual que el valor. Unos pocos euros por debajo la rechazan entera y se pierde la
    # ventana del día: redondear a la baja un importe que la API compara al euro es tirar
    # una compra por nada.
    #
    # Si la puja se queda corta por poco, se sube al mínimo válido y se registra. Si el
    # hueco es grande, es que quien pidió la puja tenía otra cifra en la cabeza: ahí se
    # rechaza, porque ajustar en silencio sería gastar más de lo que se pidió.
    ajustada = False
    if importe < mk["valor"]:
        margen = (mk["valor"] - importe) / max(mk["valor"], 1)
        if margen <= 0.05:
            log.warning("puja por %s subida de %d a %d (faltaba un %.2f%%)",
                        j["nombre"], importe, mk["valor"], margen * 100)
            importe = mk["valor"]
            ajustada = True
        else:
            raise HTTPException(
                400, f"la puja ({_mil(importe)} €) se queda un {margen*100:.1f} % por "
                     f"debajo del VALOR ({_mil(mk['valor'])} €), que es el mínimo que "
                     f"acepta la API. El precio de salida "
                     f"({_mil(mk['precio_salida'])} €) NO es el mínimo.")

    saldo = await protegido(api().saldo(await bearer(), settings.fantasy_team_id,
                                        ttl=0))   # autorizar un gasto no se hace con caché
    libre = int(saldo["teamMoney"]) - int(saldo["teamInvestment"])
    if libre < importe:
        raise HTTPException(409, f"caja insuficiente: {libre} < {importe}")
    # El tope por operación se levanta solo si la persona repitió el importe. Ese
    # freno está calibrado para el piloto automático; a una puja decidida a mano no la
    # protege, le impide fichar a cualquiera que valga la pena. La doble confirmación es
    # la que sigue cubriendo lo que de verdad puede salir mal aquí: un cero de más.
    # En `dry` se da por buena para que se pueda *preguntar* qué pasaría: el mismo
    # criterio que usa `_revisa_importe`, y un `dry` no compra nada.
    v = await puede_gastar(importe, libre,
                           orden_humana=dry or cuerpo.importe_confirmado == importe)
    if not v:
        raise HTTPException(409, f"límites: {v.motivo}")

    # La doble confirmación se comprueba TAMBIÉN cuando la puja se ha subido al valor de
    # mercado. Saltarla ahí deja pasar sin confirmar cualquier puja que arranque por
    # debajo del umbral y acabe por encima. Como confirmación vale la cifra tecleada o
    # la que se va a pagar, que es lo que evita exigir un importe imposible de adivinar.
    _revisa_importe(cuerpo, importe, dry, pedido=cuerpo.importe)
    acc = Acciones(cliente(), await bearer())
    try:
        async with lock_caja():
            return await _ejecutar("puja",
                                   f"{j['nombre']} (agente libre)"
                                   + (" [importe subido al valor]" if ajustada else ""),
                                   importe,
                                   "puja vía API", acc.pujar(mk["market_id"], importe),
                                   dry, idempotency_key)
    except BloqueoOcupado as e:
        raise HTTPException(409, str(e)) from e


@router.post("/ofertar", summary="Oferta por un jugador que vende otro manager")
async def ofertar(cuerpo: Objetivo, dry: bool = Query(False),
                  idempotency_key: str | None = Header(None)):
    """Manda una oferta por una ficha `marketPlayerTeam`.

    No garantiza el fichaje: el dueño acepta o rechaza. El dinero queda retenido en
    `teamInvestment` mientras tanto, así que baja la caja disponible sin que baje
    `teamMoney`.
    """
    _exige_confirmacion(cuerpo)
    j = await _resolver(cuerpo.jugador)
    mk = j.get("mercado")
    if not mk:
        raise HTTPException(400, f"{j['nombre']} no está en el mercado")
    if mk["es_agente_libre"]:
        raise HTTPException(400, f"{j['nombre']} es agente libre: `direct-offer` no aplica. "
                                 "Usa /accion/pujar.")
    # Un jugador PROPIO listado también es `marketPlayerTeam`, así que sin esto se puede
    # ofertar por uno mismo: se retendría la caja en `teamInvestment` a cambio de nada.
    if j.get("es_mio"):
        raise HTTPException(400, f"{j['nombre']} ya es tuyo: lo tienes listado en el mercado")
    # `direct-offer` llama `playerId` a un campo que espera el `playerTeamId`. Con el id
    # del maestro responde 400 "the player is not in the team anymore".
    ptid = j.get("player_team_id")
    if not ptid:
        raise HTTPException(409, f"no tengo el playerTeamId de {j['nombre']}: sin él la "
                                 "oferta saldría con el id del maestro y LaLiga la rechaza")

    importe = cuerpo.importe or mk["valor"]
    if importe < mk["valor"]:
        raise HTTPException(
            400, f"la oferta ({_mil(importe)} €) va por debajo del VALOR "
                 f"({_mil(mk['valor'])} €), que es el mínimo que acepta la API. El precio "
                 f"de salida ({_mil(mk['precio_salida'])} €) NO es el mínimo.")

    saldo = await protegido(api().saldo(await bearer(), settings.fantasy_team_id,
                                        ttl=0))   # autorizar un gasto no se hace con caché
    libre = int(saldo["teamMoney"]) - int(saldo["teamInvestment"])
    if libre < importe:
        raise HTTPException(409, f"caja insuficiente: {libre} < {importe}")
    v = await puede_gastar(importe, libre,
                           orden_humana=dry or cuerpo.importe_confirmado == importe)
    if not v:
        raise HTTPException(409, f"límites: {v.motivo}")
    _revisa_importe(cuerpo, importe, dry, pedido=cuerpo.importe)

    acc = Acciones(cliente(), await bearer())
    try:
        async with lock_caja():
            return await _ejecutar("oferta", f"{j['nombre']} (lo vende un manager)",
                                   importe, "oferta vía API",
                                   acc.ofertar(ptid, importe), dry, idempotency_key)
    except BloqueoOcupado as e:
        raise HTTPException(409, str(e)) from e


@router.post("/clausulazo", summary="Paga la cláusula de un jugador rival")
async def clausulazo(cuerpo: Objetivo, dry: bool = Query(False),
                     forzar: bool = Query(
                         False,
                         description="Ejecuta aunque politica.py lo rechace: salta el "
                                     "tope por operación, la reserva y el tope diario"),
                     idempotency_key: str | None = Header(None)):
    _exige_confirmacion(cuerpo)
    j = await _resolver(cuerpo.jugador)
    if j.get("es_mio"):
        raise HTTPException(400, f"{j['nombre']} ya es tuyo")
    if j.get("blindado"):
        raise HTTPException(409, f"{j['nombre']} está BLINDADO hasta {j.get('blindado_hasta')}")
    if j.get("protegido_hasta"):
        import datetime as dt
        try:
            if dt.datetime.fromisoformat(j["protegido_hasta"]) > dt.datetime.now(dt.UTC):
                raise HTTPException(409, f"protegido (14 días) hasta {j['protegido_hasta']}")
        except ValueError:
            # Fecha ilegible: se sigue a propósito, no se aborta. Esta comprobación es
            # una cortesía para fallar antes y gratis; la que manda es LaLiga, que
            # rechaza a un protegido con 400 `030.01.16` sin cobrar nada. Abortar aquí
            # por un formato raro dejaría sin clausulazo una ventana que dura horas.
            log.warning("protegido_hasta ilegible (%r) en %s: decide LaLiga",
                        j.get("protegido_hasta"), j.get("nombre"))

    # El importe se toma de la lectura que acaba de hacerse: la API exige el euro exacto
    # y un valor de hace horas da 409 `030.01.49`.
    importe = j["clausula"]
    saldo = await protegido(api().saldo(await bearer(), settings.fantasy_team_id,
                                        ttl=0))   # autorizar un gasto no se hace con caché
    libre = int(saldo["teamMoney"]) - int(saldo["teamInvestment"])
    if libre < importe:
        raise HTTPException(409, f"caja insuficiente: {libre} < {importe}")
    v = await puede_gastar(importe, libre)
    if not v and not forzar:
        raise HTTPException(409, f"límites: {v.motivo}. Usa ?forzar=true si lo autorizas.")

    _revisa_importe(cuerpo, importe, dry)
    acc = Acciones(cliente(), await bearer())
    try:
        async with lock_caja(), lock_jugador(str(j["player_team_id"])):
            return await _ejecutar("clausulazo", f"{j['nombre']} ({j.get('dueno')})", importe,
                                   "clausulazo vía API" + (" (forzado)" if forzar else ""),
                                   acc.pagar_clausula(j["player_team_id"], importe),
                                   dry, idempotency_key)
    except BloqueoOcupado as e:
        raise HTTPException(409, str(e)) from e


async def _no_esta_en_el_once(player_team_id: str, nombre: str) -> None:
    """Lanza 423 si el jugador es titular del once bloqueado. No devuelve nada si no lo es.

    Venderlo dejaría el once en 10 y la jornada entera puntuaría 0.

    OJO al mover esta función: tiene que quedar fuera del bloque `@router.post` / `vender`,
    nunca entre los dos. Intercalada, FastAPI registraría esta auxiliar como el endpoint y
    `/accion/vender` empezaría a pedir sus argumentos por query.
    """
    from .bloqueo import titulares_bloqueados
    if str(player_team_id) in await titulares_bloqueados():
        raise HTTPException(
            423, f"{nombre} es TITULAR del once bloqueado. Venderlo lo dejaría en 10 "
                 f"jugadores y la jornada no puntuaría. Abre el candado primero "
                 f"(DELETE /bloqueo/once) si de verdad quieres.")


@router.post("/vender", summary="Lista a un jugador propio en el mercado")
async def vender(cuerpo: Objetivo, dry: bool = Query(False),
                 idempotency_key: str | None = Header(None)):
    j = await _resolver(cuerpo.jugador)
    if not j.get("es_mio"):
        raise HTTPException(400, f"{j['nombre']} no es tuyo")
    await _no_esta_en_el_once(j["player_team_id"], j["nombre"])
    precio = cuerpo.importe or j["valor"]
    acc = Acciones(cliente(), await bearer())
    try:
        async with lock_jugador(str(j["player_team_id"])):
            return await _ejecutar("venta_listar", f"{j['nombre']}", precio,
                                   "listado vía API (el cobro lo hace job_ofertas)",
                                   acc.vender_al_mercado(j["player_team_id"], precio),
                                   dry, idempotency_key)
    except BloqueoOcupado as e:
        raise HTTPException(409, str(e)) from e


@router.post("/subir-clausula", summary="Sube la cláusula de un jugador propio")
async def subir_clausula(cuerpo: Objetivo, dry: bool = Query(False),
                         idempotency_key: str | None = Header(None)):
    """Cuesta la mitad del incremento (`factor=2`, el único que existe)."""
    _exige_confirmacion(cuerpo)
    j = await _resolver(cuerpo.jugador)
    if not j.get("es_mio"):
        raise HTTPException(400, f"{j['nombre']} no es tuyo")
    if not cuerpo.importe:
        raise HTTPException(400, "falta `importe`: cuánto subir la cláusula, en euros")
    coste = cuerpo.importe // 2
    acc = Acciones(cliente(), await bearer())
    # El lock abarca desde la lectura del saldo: leerlo fuera deja que dos operaciones
    # vean la misma caja y las
    # dos pasen `puede_gastar`. Con la subida de cláusula el riesgo es mayor que en una
    # puja, porque lo que se paga es la mitad de lo pedido y el descuadre no salta a la
    # vista al leer el importe.
    try:
        async with lock_caja(), lock_jugador(str(j["player_team_id"])):
            saldo = await protegido(api().saldo(await bearer(), settings.fantasy_team_id,
                                                ttl=0))   # un gasto no se autoriza con caché
            libre = int(saldo["teamMoney"]) - int(saldo["teamInvestment"])
            v = await puede_gastar(coste, libre)
            if not v:
                raise HTTPException(409, f"límites: {v.motivo}")
            _revisa_importe(cuerpo, coste, dry)
            return await _ejecutar("subir_clausula", j["nombre"], coste,
                                   f"+{cuerpo.importe} de cláusula (cuesta la mitad)",
                                   acc.subir_clausula(j["player_team_id"], cuerpo.importe),
                                   dry, idempotency_key)
    except BloqueoOcupado as e:
        raise HTTPException(409, str(e)) from e


# ---------------------------------------------------------------- alineación

@router.post("/alineacion", summary="Guarda el once (automático o a mano)")
async def alineacion(payload: dict | None = Body(None), auto: bool = Query(False),
                     dry: bool = Query(False),
                     respetar_candado: bool = Query(True)):
    """Con `?auto=true` calcula el mejor once; si no, guarda el `payload` recibido.

    Valida que sean 11, que todos sigan en plantilla y que cada uno vaya en su línea
    antes de enviar. Un once de 10 anula la jornada entera, así que aquí no se manda nada
    sin comprobarlo.

    `respetar_candado=false` lo usa solo `guardia_once`. Una reparación no es un
    recálculo: el criterio humano que echó el candado ya no se puede cumplir si el
    jugador no es tuyo. El resto de comprobaciones se aplican igual.
    """
    # El defecto es `True` a propósito. Estas funciones son endpoints, así que al
    # llamarlas desde Python sus valores por defecto son objetos `Query`, y un `Query`
    # es *truthy*: quien olvide el parámetro se queda con el candado puesto, no sin él.
    if respetar_candado:
        from .bloqueo import exigir_abierto
        await exigir_abierto("once")

    from ..models.alineacion import a_payload, mejor_once, valido
    if auto:
        once = await mejor_once()
        ok, motivo = valido(once)
        if not ok:
            raise HTTPException(409, f"el mejor once no es válido: {motivo}")
        payload = a_payload(once)
    if not payload:
        raise HTTPException(400, "manda un `payload` o usa `?auto=true`")

    n = 1 + len(payload.get("defender") or []) + len(payload.get("midfield") or []) \
        + len(payload.get("striker") or [])
    if n != 11:
        raise HTTPException(400, f"el once tiene {n} jugadores, no 11: la jornada no puntuaría")

    tok = await bearer()
    # `ttl=0`: aquí se decide si un jugador sigue siendo tuyo, y la copia de 15 min
    # puede ser de antes de que te lo clausularan. Cuesta una petición; el fallo que
    # evita cuesta la jornada entera.
    plantilla = await protegido(
        api().plantilla(tok, settings.fantasy_league_id, settings.fantasy_team_id, ttl=0))
    mios = {str(p.get("playerTeamId")) for p in plantilla.get("players", [])}
    pedidos = {str(payload.get("goalkeeper"))}
    for k in ("defender", "midfield", "striker"):
        pedidos |= {str(x) for x in (payload.get(k) or [])}
    fuera = [p for p in pedidos if p not in mios]
    if fuera:
        raise HTTPException(409, f"esos playerTeamId ya no son tuyos: {fuera}")
    # Tras un cambio de demarcación, LaLiga descarta en silencio a quien va en su línea
    # vieja y el once se queda en diez. La posición se lee de la misma plantilla fresca.
    pos = {str(p.get("playerTeamId")): (p.get("playerMaster") or {}).get("positionId")
           for p in plantilla.get("players", [])}
    nombre = {str(p.get("playerTeamId")): (p.get("playerMaster") or {}).get("nickname")
              for p in plantilla.get("players", [])}
    lineas = {"goalkeeper": 1, "defender": 2, "midfield": 3, "striker": 4}
    descolocados = []
    for k, esperada in lineas.items():
        v = payload.get(k) or []
        for x in (v if isinstance(v, list) else [v]):
            real = pos.get(str(x))
            if real not in (None, "") and int(real) != esperada:
                descolocados.append(nombre.get(str(x)) or str(x))
    if descolocados:
        raise HTTPException(409, f"fuera de su línea (cambio de demarcación): {descolocados}")

    if dry:
        return {"dry_run": True, "payload": payload, "num_jugadores": n}
    acc = Acciones(cliente(), tok)
    res = await acc.guardar_alineacion(payload)

    # `guardar_alineacion` relee y cuenta 11: un 200 de LaLiga no basta, porque descarta
    # en silencio a quien ya no es tuyo. Si el once quedó corto hay que despertar a
    # alguien: no se arregla solo y a la hora de congelarse ya no hay vuelta atrás.
    if res.get("verificado") and res.get("en_la_api") != 11:
        from ..alerts import EMERGENCIA, enviar
        await enviar(
            f"🚨 <b>EL ONCE QUEDÓ EN {res.get('en_la_api')}</b>\n"  # sin-rastro-ok: va a Telegram
            f"   LaLiga aceptó la alineación y descartó a: "
            f"{', '.join(res.get('descartados') or []) or '(sin identificar)'}\n"
            f"   Un once incompleto anula la jornada entera. Revísalo YA.",
            nivel=EMERGENCIA, categoria="once_roto")

    return {"ok": bool(res.get("ok")), "status": res.get("status"), "payload": payload,
            "verificado": res.get("verificado"), "en_la_api": res.get("en_la_api"),
            "descartados": res.get("descartados"), "aviso": res.get("aviso")}


@router.post("/fijar-once", summary="Fija un once para una jornada (manda sobre el modelo)")
async def fijar_once(payload: dict = Body(...), jornada: int = Query(...),
                     motivo: str = Query("fijado a mano")):
    """El bot lo respetará en vez de recalcular, validando antes que sigan en plantilla."""
    from ..once_fijado import fijar
    fijar(payload, jornada, motivo)
    return {"ok": True, "jornada": jornada, "motivo": motivo}


@router.delete("/fijar-once", summary="Suelta el once fijado: vuelve a decidir el modelo")
async def soltar_once():
    from ..once_fijado import soltar
    soltar()
    return {"ok": True}


@router.post("/venta-inmediata", summary="Vende YA, sin esperar oferta (paga el 50 % del valor)")
async def venta_inmediata(cuerpo: Objetivo, dry: bool = Query(False),
                          idempotency_key: str | None = Header(None)):
    """Vende al instante, frente a `vender`, que solo lista y puede tardar días.

    Cobra el 50 % del valor de mercado, así que se pierde la mitad. Es para caja
    inmediata o para un jugador `out_of_league` al que el mercado no hace oferta.

    LaLiga exige exactamente esa cifra, la mitad truncada al euro: con el valor
    completo responde `030.01.02`, cuyo texto habla del "valor de mercado" aunque el
    problema sea la cantidad. Por eso el importe se calcula aquí por defecto. Si el cuerpo
    trae `importe`, se manda ese en su lugar.
    """
    _exige_confirmacion(cuerpo)
    j = await _resolver(cuerpo.jugador)
    if not j.get("es_mio"):
        raise HTTPException(400, f"{j['nombre']} no es tuyo")
    await _no_esta_en_el_once(j["player_team_id"], j["nombre"])
    # `//` (truncado), no `round`: la API exige la mitad del valor truncada al euro.
    precio = cuerpo.importe or (int(j["valor"]) // 2)
    _revisa_importe(cuerpo, precio, dry)
    acc = Acciones(cliente(), await bearer())
    try:
        async with lock_jugador(str(j["player_team_id"])):
            return await _ejecutar("venta_inmediata", j["nombre"], precio,
                                   "venta inmediata: caja al instante",
                                   acc.venta_inmediata(j["player_team_id"], precio),
                                   dry, idempotency_key)
    except BloqueoOcupado as e:
        raise HTTPException(409, str(e)) from e


# ---------------------------------------------------------------- ofertas recibidas

class Oferta(BaseModel):
    market_id: str = Field(..., description="Id de TU ficha en el mercado")
    offer_id: str = Field(..., description="Id de la oferta concreta")
    importe: int | None = Field(None, description="Obligatorio al aceptar: la API lo exige")
    confirmar: bool = False


@router.post("/aceptar-oferta", summary="Cobra una oferta por un jugador tuyo ya listado")
async def aceptar_oferta(cuerpo: Oferta, dry: bool = Query(False),
                         idempotency_key: str | None = Header(None)):
    """Cobra una oferta recibida.

    El porqué de que no pase por `puede_gastar()` está en `Acciones.aceptar_oferta`. Lo
    que añade esta capa: rechaza con 400 si falta el `importe` (LaLiga lo exige como
    confirmación), coge el lock de caja y registra en `decisions` por `_ejecutar`.
    """
    _exige_confirmacion(cuerpo)
    if not cuerpo.importe:
        raise HTTPException(400, "falta el importe: la API lo exige como confirmación")
    acc = Acciones(cliente(), await bearer())
    try:
        # Lock de caja: un cobro y una puja simultáneos leerían el mismo saldo.
        async with lock_caja():
            return await _ejecutar("venta_cobrar", f"oferta {cuerpo.offer_id}",
                                   cuerpo.importe, "cobro de una oferta recibida",
                                   acc.aceptar_oferta(cuerpo.market_id, cuerpo.offer_id,
                                                      cuerpo.importe),
                                   dry, idempotency_key)
    except BloqueoOcupado as e:
        raise HTTPException(409, str(e)) from e


@router.post("/rechazar-oferta", summary="Rechaza una oferta recibida")
async def rechazar_oferta(cuerpo: Oferta, dry: bool = Query(False),
                          idempotency_key: str | None = Header(None)):
    """No mueve dinero: solo cierra la puerta. La ficha sigue listada."""
    acc = Acciones(cliente(), await bearer())
    return await _ejecutar("venta_rechazar", f"oferta {cuerpo.offer_id}", 0,
                           "oferta por debajo del mínimo",
                           acc.rechazar_oferta(cuerpo.market_id, cuerpo.offer_id),
                           dry, idempotency_key)


class Puja(BaseModel):
    market_id: str
    bid_id: str
    confirmar: bool = False


class Ficha(BaseModel):
    market_id: str
    confirmar: bool = False


@router.post("/cancelar-puja", summary="Retira una puja viva por un agente libre")
async def cancelar_puja(cuerpo: Puja, dry: bool = Query(False),
                        idempotency_key: str | None = Header(None)):
    """Libera el dinero retenido en `teamInvestment`.

    Las pujas no bajan de `teamMoney`: se apuntan aparte y no cuentan como
    disponible. Si aparece algo mejor antes de las 13:48, cancelar es la única forma de
    recuperar esa caja: una puja no se rebaja, se retira.
    """
    acc = Acciones(cliente(), await bearer())
    return await _ejecutar("puja_cancelar", f"puja {cuerpo.bid_id}", 0,
                           "liberar la caja retenida",
                           acc.cancelar_puja(cuerpo.market_id, cuerpo.bid_id),
                           dry, idempotency_key)


@router.post("/retirar-mercado", summary="Quita del mercado un jugador que listaste")
async def retirar_mercado(cuerpo: Ficha, dry: bool = Query(False),
                          idempotency_key: str | None = Header(None)):
    """Deja de ofrecer a un jugador propio.

    No deshace una venta ya aceptada. Mientras la ficha siga viva el sistema puede
    mandar su oferta; retirarla a tiempo es lo que evita perder al jugador si cambias de
    idea. No mueve dinero.
    """
    acc = Acciones(cliente(), await bearer())
    return await _ejecutar("venta_retirar", f"ficha {cuerpo.market_id}", 0,
                           "ya no se quiere vender",
                           acc.retirar_del_mercado(cuerpo.market_id),
                           dry, idempotency_key)


# ---------------------------------------------------------------- resolución

@router.get("/resolver/{nombre}", summary="Busca un jugador por nombre (sin ejecutar nada)")
async def resolver(nombre: str):
    """Útil para comprobar antes de actuar: dice de quién es, su cláusula, si está
    blindado o protegido y si está en el mercado."""
    return await _resolver(nombre)
