"""Escrituras contra LaLiga: pujar, clausulazo, venta, blindaje, alineación.

No se reintenta nunca: no son idempotentes, y un reintento duplica la puja o paga la
cláusula dos veces. Ante un fallo se relee el estado.

El freno no vive aquí, lo aplica `api/accion.py` con `politica.puede_gastar()`. Lo que
gasta solo corre en modo real; lo que ingresa o es gratis va con `mueve_dinero=False` y
solo lo frena el modo sombra. Cada trampa de la API está anotada en su método.

`mueve_dinero` separa COMPRAS Y PAGOS de todo lo demás, no "gasta" de "ingresa". Cuatro
métodos que no mueven un euro lo llevan a `True` a propósito, del lado conservador:
`rechazar_oferta`, `cancelar_puja`, `cancelar_oferta` y `vender_al_mercado`, que solo
lista. Por `/accion/*` da igual, porque `orden_explicita()` hace que `es_real()` sea cierto
venga el modo que venga; solo se notaría llamando a esta clase desde fuera de ese camino.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..config import settings
from ..politica import SOMBRA, es_real, modo
from .http import ClienteEducado

log = logging.getLogger("coconut.acciones")

# Los códigos de LaLiga son opacos: `030.01.49` no dice nada al leer un log.
# Todos salen de docs/ERRORES-API.md menos `030.01.20`, que no está en el manual del APK:
# ver la nota junto a su entrada.
ERRORES = {
    "030.01.17": ("los clausulazos están CERRADOS: se desactivan 24 h antes de cada "
                  "jornada y se reanudan cuando esa jornada empieza"),
    "030.01.14": "no tienes dinero para subir tanto la cláusula",
    "030.01.16": "lleva menos de 14 días en su equipo: aún está protegido",
    "030.01.29": "la oferta tiene que ser igual o mayor que la cláusula",
    "030.01.31": "la puja está por debajo del valor de mercado, no del precio de salida",
    "030.01.32": ("supera tu deuda permitida, que es el 20 % del valor del equipo. "
                  "Y el saldo negativo anula la jornada"),
    "030.01.40": "has llegado al máximo de 24 fichas, contando pujas y ofertas vivas",
    "030.01.42": "el dueño ha rechazado la oferta",
    "030.01.48": "la oferta ha caducado",
    "030.02.01": "ese jugador ya está blindado",
    "030.02.03": ("has gastado los blindajes de esta jornada; el cupo no vuelve hasta la "
                  "siguiente, no cuando caduque uno"),
    "050.01.01": "el anuncio no se ha validado",
    "050.01.04": "ya has cobrado la recompensa de hoy",
    "030.01.49": "la cláusula cambió mientras decidías: reléela y reintenta al euro exacto",
    # El texto es el del APK, literal. Ver `pagar_clausula` para lo que NO dice.
    "030.01.62": "está blindado: no se puede clausular",
    # El saldo insuficiente que trae el manual del APK es el `.28`; el `.20` no aparece
    # ahí: el documento salta del `.19` al `.22`, y el `.20` no tiene fuente. Se dejan los
    # dos, porque traducir de más no cuesta nada y quitar el bueno sí.
    "030.01.28": "no tienes saldo suficiente",
    "030.01.20": "no tienes saldo suficiente (código sin confirmar, no está en el APK)",
}


def traducir(texto: str) -> str:
    """Añade la explicación en castellano al código de error. Para el log.

    Conserva el texto crudo porque en el log interesa la URL y el cuerpo entero. Para
    enseñárselo a una persona está `explicar()`, que no arrastra la traza.
    """
    for codigo, explica in ERRORES.items():
        if codigo in (texto or ""):
            return f"{texto}  →  {explica}"
    return texto


def explicar(cuerpo: str) -> str:
    """El mensaje limpio para una persona: solo lo que significa el error.

    `traducir()` añade la explicación al final del texto crudo, que trae la URL entera.
    Eso vale para un log y no para un diálogo o un mensaje de Telegram.
    """
    import json as _json
    import re as _re
    for codigo, explica in ERRORES.items():
        if codigo in (cuerpo or ""):
            return explica
    # Sin código conocido, al menos el `message` que manda LaLiga.
    m = _re.search(r'\{.*\}', cuerpo or "", _re.S)
    if m:
        try:
            return str(_json.loads(m.group(0)).get("message") or cuerpo)[:200]
        except Exception:
            pass
    return (cuerpo or "sin detalle")[:200]

CMP = "/v1/competition/{cid}"


class ErrorDeLaLiga(RuntimeError):
    """Un 4xx de LaLiga: la petición se entendió y se rechazó por una regla del juego.

    No hay nada que arreglar en el código: "ya está blindado" o "has llegado al límite"
    son respuestas correctas. Lleva el `status` para que la API lo devuelva tal cual en
    vez de convertirlo en un 500.
    """

    def __init__(self, status: int, mensaje: str, cuerpo: str = ""):
        super().__init__(mensaje)
        self.status = status
        self.cuerpo = cuerpo
        self.codigo = ""
        import re as _re
        m = _re.search(r'"errorCode"\s*:\s*"([^"]+)"', cuerpo or "")
        if m:
            self.codigo = m.group(1)


def cuerpo_de(e: BaseException) -> str:
    """El cuerpo que devolvió LaLiga, venga la excepción como venga.

    Un fallo llega como `ErrorDeLaLiga` o como `httpx.HTTPStatusError`, y solo la segunda
    trae `.response`. Quien mire `getattr(e, "response")` a secas recibe cadena vacía para
    la primera, se queda sin el `errorCode` y registra como avería una regla del juego.
    """
    if isinstance(e, ErrorDeLaLiga):
        return e.cuerpo or str(e)
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            return resp.text or ""
        except Exception:
            return ""
    return ""


class Acciones:
    def __init__(self, http: ClienteEducado, token: str, liga: str | None = None,
                 equipo: str | None = None):
        self.http = http
        self.token = token
        self.liga = liga or settings.fantasy_league_id
        self.equipo = equipo or settings.fantasy_team_id
        self.cid = settings.competition_id

    def _p(self, sufijo: str) -> str:
        return CMP.format(cid=self.cid) + sufijo

    async def _escribir(self, metodo: str, path: str, cuerpo: Any = None,
                        que: str = "", mueve_dinero: bool = True) -> dict[str, Any]:
        # El freno depende de si la acción gasta: lo que gasta solo corre en modo `real`,
        # lo que no (alineación, blindaje, cobros, retirar del mercado) corre siempre
        # salvo en sombra. Guardar el once es gratis, y no guardarlo son 0 puntos.
        #
        # `es_real()` no mira solo `COCONUT_MODO`: también el `ContextVar` que marca
        # `orden_explicita`, o sea que el resultado depende de QUIÉN llama. Por `/accion/*`
        # y por una tarea programada da true aunque el modo sea `agente`, porque eso lo ha
        # pedido una persona. En un script suelto, no. Quien pruebe esta línea fuera de ese
        # contexto verá otro comportamiento con el mismo `.env`.
        frenado = (not es_real()) if mueve_dinero else (modo() == SOMBRA)
        if frenado:
            log.info("[SIMULADO] %s %s %s", metodo, path, cuerpo or "")
            return {"sombra": True, "metodo": metodo, "path": path, "cuerpo": cuerpo}
        try:
            resp = await self.http.escribe(metodo, path, json_body=cuerpo, auth=self.token)
        except Exception as e:
            # httpx no mete el cuerpo en `str(e)`, solo deja "Client error '400 Bad
            # Request' for url ...". Traducir sobre ese texto nunca encuentra el
            # `errorCode`, así que un 400 explicado saldría crudo y acabaría en un 500.
            # El cuerpo se lee de `e.response.text`.
            cuerpo_err = ""
            resp_err = getattr(e, "response", None)
            if resp_err is not None:
                try:
                    cuerpo_err = resp_err.text or ""
                except Exception:
                    cuerpo_err = ""
            estado = getattr(resp_err, "status_code", None)
            log.error("%s %s -> %s | %s", metodo, path, estado, cuerpo_err[:200] or e)
            if estado and 400 <= estado < 500:
                # Un 4xx de LaLiga es una respuesta, no una avería del cliente: se propaga
                # con su código para que quien llame pueda distinguirlos.
                raise ErrorDeLaLiga(estado, explicar(cuerpo_err), cuerpo_err) from e
            raise
        log.info("EJECUTADO %s %s -> %d", metodo, path, resp.status_code)
        try:
            return {"ok": True, "status": resp.status_code, "datos": resp.json()}
        except Exception:
            return {"ok": True, "status": resp.status_code, "datos": None}

    # ---------- dinero ----------

    async def pagar_clausula(self, player_team_id: str | int,
                             importe: int) -> dict[str, Any]:
        """Clausulazo. Irreversible: el jugador cambia de dueño al instante.

        El id es el `playerTeamId`, el de la ficha DENTRO del equipo, no el del maestro.
        Por eso el parámetro no se llama `player_id`: con ese nombre el piloto le pasa el
        del maestro y el clausulazo no llega a ejecutarse.

        El importe tiene que ser exacto al euro (409 `030.01.49`), así que se relee la
        cláusula justo antes. Un blindado responde 400 `030.01.62` aunque su
        `buyoutClauseLockedEndTime` esté vencido.

        Que un jugador LISTADO en el mercado dé ese mismo error no consta en ninguna
        parte y no se filtra por ello. Lo decide el primer clausulazo que se lance sobre
        una ficha listada: sale, o devuelve `030.01.62` y entonces sí es una regla.
        """
        return await self._escribir(
            "POST", self._p(f"/league/{self.liga}/buyout/{player_team_id}/pay"),
            {"buyoutClauseToPay": int(importe)}, que="clausulazo")

    async def pujar(self, market_id: str | int, importe: int) -> dict[str, Any]:
        """Puja por un agente libre (`marketPlayerLeague`). La subasta resuelve sola.

        Sobre una ficha `marketPlayerTeam`, la que vende otro manager, responde 404: para
        esa va `ofertar()`. El importe se compara contra el valor de mercado, no contra el
        precio de salida (400 `030.01.31`).
        """
        return await self._escribir(
            "POST", self._p(f"/league/{self.liga}/market/{market_id}/bid"),
            {"money": int(importe)}, que="puja")

    async def ofertar(self, player_team_id: str | int, importe: int) -> dict[str, Any]:
        """Oferta por un jugador de otro manager (ficha `marketPlayerTeam`).

        No es una compra: el dueño acepta o rechaza, así que el jugador no está
        garantizado. Y la oferta es pública, o sea que pedir un portero dice que te falta
        uno.

        El campo del cuerpo se llama `playerId` y espera el `playerTeamId`: con el id del
        maestro responde 400 "the player is not in the team anymore".
        """
        return await self._escribir(
            "POST", self._p(f"/league/{self.liga}/market/direct-offer"),
            {"playerId": str(player_team_id), "money": int(importe)}, que="oferta")

    async def modificar_puja(self, market_id: str | int, bid_id: str | int,
                             importe: int) -> dict[str, Any]:
        """Cambia el importe de una puja viva. NUNCA SE HA EJECUTADO.

        La ruta sale del catálogo del APK y el verbo no está sondeado, así que puede dar
        405 como `/cancel` y `/delete`, escritas con POST cuando eran DELETE. Para
        bajar un importe no sirve: `cancelar_puja()` y volver a pujar.
        """
        return await self._escribir(
            "PUT", self._p(f"/league/{self.liga}/market/{market_id}/bid/{bid_id}"),
            {"money": int(importe)}, que="modificar puja")

    async def vender_al_mercado(self, player_team_id: str | int,
                                precio: int) -> dict[str, Any]:
        """LISTA al jugador en el mercado. NO es una venta: no entra dinero.

        El cobro es un segundo paso: el sistema manda su oferta y hay que aceptarla con
        `aceptar_oferta()`. Entre lo uno y lo otro pasan horas o días, y la ficha caduca a
        los 3. Tres horas de margen no bastan: si hace falta caja para una hora concreta,
        hay que tenerla antes, no listar esa mañana.

        El id es el `playerTeamId`, el de la ficha DENTRO del equipo, no el del maestro.
        El campo del cuerpo se llama `playerId` y aun así espera el otro: con el id del
        maestro responde 400.
        """
        return await self._escribir(
            "POST", self._p(f"/league/{self.liga}/market/sell"),
            {"playerId": str(player_team_id), "salePrice": int(precio)}, que="venta")

    async def aceptar_oferta(self, market_id: str | int, offer_id: str | int,
                             importe: int) -> dict[str, Any]:
        """Cobra una oferta por un jugador propio ya listado.

        `mueve_dinero=False` a propósito: esta acción ingresa, no gasta. El freno existe
        para que el bot no compre solo, y bloquear un cobro hace perder ventas porque la
        oferta caduca en el siguiente ciclo de mercado. El importe va en el cuerpo porque
        la API lo exige como confirmación.
        """
        return await self._escribir(
            "POST",
            self._p(f"/league/{self.liga}/market/{market_id}/offer/{offer_id}/accept"),
            {"offerMoney": int(importe)}, que="aceptar oferta", mueve_dinero=False)

    async def rechazar_oferta(self, market_id: str | int,
                              offer_id: str | int) -> dict[str, Any]:
        # Sin cuerpo y sin Content-Type: con cuerpo vacío declarado, la API da 400.
        return await self._escribir(
            "POST",
            self._p(f"/league/{self.liga}/market/{market_id}/offer/{offer_id}/reject"),
            None, que="rechazar oferta")

    # ---------- defensa ----------

    async def subir_clausula(self, player_team_id: str | int, incremento: int,
                             factor: int = 2) -> dict[str, Any]:
        """Blindar a un jugador propio subiéndole la cláusula.

        `factor=1` no existe (400 "Incorrect clause factor"), el mínimo es 2. Con
        `factor=2` la cláusula sube exactamente `incremento` y cuesta la mitad, así que
        blindar por esta vía sale al 50 % de lo que sube. No reinicia
        `buyoutClauseLockedEndTime`, así que no abre ventana.

        El id es el `playerTeamId`, igual que en `pagar_clausula`: el campo del cuerpo se
        llama `playerId` y aun así espera el otro.
        """
        return await self._escribir(
            "PUT", self._p(f"/league/{self.liga}/buyout/player"),
            {"playerId": str(player_team_id), "valueToIncrease": int(incremento),
             "factor": int(factor)}, que="subir cláusula")

    async def blindar(self, player_team_id: str | int) -> dict[str, Any]:
        """Blinda a un jugador propio: 24 h sin poder ser clausulado (48 h en premium).

        Es gratis y se obtiene viendo un anuncio: `rewardedAd: 1` declara haberlo visto y
        LaLiga no lo valida en servidor. El cupo va por jornada, no por día. La vía sin
        ninguna duda es activarlo desde la app oficial.

        Se pregunta antes con `check-shield`, que responde sin gastar el blindaje:
        `030.02.01` si ya está blindado, `030.02.03` si se llegó al límite.
        """
        # La comprobación previa no debe impedir el intento si ella misma falla por otra
        # cosa (red, sesión): solo se aborta cuando LaLiga dice explícitamente que no.
        try:
            await self.http.get(
                self._p(f"/league/{self.liga}/player-team/{player_team_id}/check-shield"),
                ttl=0, auth=self.token)
        except Exception as e:
            cuerpo = getattr(getattr(e, "response", None), "text", "") or ""
            if "030.02." in cuerpo:
                estado = getattr(getattr(e, "response", None), "status_code", 400)
                raise ErrorDeLaLiga(estado, explicar(cuerpo), cuerpo) from e
            if isinstance(e, json.JSONDecodeError):
                # `check-shield` dice que SÍ con un 200 y el cuerpo vacío, y el cliente
                # revienta al parsearlo. Como el `raise_for_status()` va antes del parseo,
                # un fallo de parseo aquí es un 2xx sin cuerpo, o sea que hay cupo. Sin
                # esta rama el camino bueno se registra como "no respondió", y quien lea
                # el log se pone a buscar un problema de red que no existe.
                log.info("check-shield: hay cupo (200 sin cuerpo)")
            else:
                log.warning("check-shield no respondió (%s); se intenta blindar igual",
                            type(e).__name__)

        return await self._escribir(
            "PUT", self._p(f"/league/{self.liga}/shield/player"),
            {"playerId": str(player_team_id), "rewardedAdType": "Blindaje",
             "rewardedAd": 1}, que="blindaje", mueve_dinero=False)

    async def venta_inmediata(self, player_team_id: str | int, precio: int) -> dict[str, Any]:
        """Vende al instante, sin esperar a que el sistema haga oferta.

        Listar no es vender: una ficha puede pasar días sin recibir ni una oferta, así que
        el mercado normal no sirve para conseguir caja a una hora fija. Paga el 50 % del
        valor y exige exactamente esa cifra: con el valor entero responde `030.01.02`, con
        la mitad truncada al euro, 201. El cálculo lo hace `/accion/venta-inmediata`.
        """
        return await self._escribir(
            "POST", self._p(f"/league/{self.liga}/market/immediate-sale"),
            {"playerId": str(player_team_id), "salePrice": int(precio)},
            que="venta inmediata", mueve_dinero=False)

    async def cancelar_puja(self, market_id: str | int,
                            bid_id: str | int) -> dict[str, Any]:
        """Retira una puja viva por un agente libre.

        Libera el dinero, que hasta entonces está retenido en `teamInvestment` y no cuenta
        como disponible. Una puja no se modifica a la baja: se cancela, y antes de las
        13:48.
        """
        # DELETE, no POST: con POST responde 405. Lo que quita algo (`/cancel`,
        # `/delete`) va por DELETE; lo que actúa (`/accept`, `/reject`, `/bid`, `/pay`)
        # por POST. Sin cuerpo: declarar uno vacío da 400.
        return await self._escribir(
            "DELETE",
            self._p(f"/league/{self.liga}/market/{market_id}/bid/{bid_id}/cancel"),
            None, que="cancelar puja")

    async def cancelar_oferta(self, market_id: str | int,
                              offer_id: str | int) -> dict[str, Any]:
        """Retira una oferta hecha a otro manager, antes de que la acepte o la rechace."""
        return await self._escribir(
            # DELETE, no POST: ver la nota de `cancelar_puja`.
            "DELETE",
            self._p(f"/league/{self.liga}/market/{market_id}/offer/{offer_id}/cancel"),
            None, que="cancelar oferta")

    async def retirar_del_mercado(self, market_id: str | int) -> dict[str, Any]:
        """Quita del mercado un jugador propio que habías listado.

        Sirve para arrepentirse: mientras la ficha esté viva el sistema puede mandar su
        oferta. No deshace una venta ya aceptada.
        """
        # No mueve dinero: solo deja de ofrecer al jugador.
        # DELETE, no POST: ver la nota de `cancelar_puja`.
        return await self._escribir(
            "DELETE", self._p(f"/league/{self.liga}/market/{market_id}/delete"),
            None, que="retirar del mercado", mueve_dinero=False)

    async def cobrar_recompensa_diaria(self) -> dict[str, Any]:
        """Cobra los 100.000 €/día de la liga. Ingresa dinero, no lo gasta.

        La ruta lleva `team` en medio (`/league/{lg}/team/daily-reward`);
        `{CMP}/daily-reward` a secas existe pero solo devuelve la tarifa. El tipo de
        anuncio se escribe `dailyreward`, en minúsculas y sin separar: cualquier otra
        forma da `400 RewardedAd not valid` (`050.01.01`), y aquí el servidor sí valida
        ese campo, a diferencia del blindaje.

        Al segundo intento del día responde 400 `050.01.04` y llega como `ErrorDeLaLiga`:
        es el caso normal de la segunda pasada, no una avería.
        """
        return await self._escribir(
            "POST", self._p(f"/league/{self.liga}/team/daily-reward"),
            {"teamId": int(self.equipo), "rewardedAdType": "dailyreward", "rewardedAd": 1},
            que="recompensa diaria", mueve_dinero=False)

    # ---------- sin dinero ----------

    async def guardar_alineacion(self, alineacion: dict[str, Any]) -> dict[str, Any]:
        """No mueve dinero, y es la que evita sacar 0 puntos.

        Un 200 en este PUT no significa que el once esté bien: LaLiga descarta en silencio
        a los que ya no son tuyos y responde que todo ha ido bien. Un once de diez anula la
        jornada entera, así que aquí se relee y se cuentan 11.

        Va en el cliente y no en `/accion/alineacion` porque es el único punto por el que
        pasan las dos vías, incluido el respaldo directo del scheduler.
        """
        res = await self._escribir(
            "PUT", self._p(f"/teams/{self.equipo}/lineup"), alineacion,
            que="alineación", mueve_dinero=False)
        if res.get("sombra") or not res.get("ok"):
            return res
        return {**res, **await self._verificar_once(alineacion)}

    async def _verificar_once(self, enviado: dict[str, Any]) -> dict[str, Any]:
        """Relee el once y comprueba que LaLiga se ha quedado con los 11.

        Con `ttl=0`: justo después de escribir es cuando más falta hace la verdad, y una
        copia cacheada devolvería el once de antes, dando por buena una escritura que
        falló. Si no puede comprobarlo devuelve `verificado: False`, que no es lo mismo
        que estar bien.
        """
        pedidos = {str(enviado.get("goalkeeper"))}
        for k in ("defender", "midfield", "striker"):
            pedidos |= {str(x) for x in (enviado.get(k) or [])}
        try:
            actual = await self.http.get(self._p(f"/teams/{self.equipo}/lineup"),
                                         ttl=0, auth=self.token)
        except Exception as e:
            log.warning("once guardado pero NO verificado (%s)", type(e).__name__)
            return {"verificado": False,
                    "aviso": "no se pudo releer el once: compruébalo a mano"}

        f = (actual or {}).get("formation") or {}
        puestos: set[str] = set()
        for k in ("goalkeeper", "defender", "midfield", "striker"):
            v = f.get(k) or []
            for j in (v if isinstance(v, list) else [v]):
                ptid = j.get("playerTeamId") if isinstance(j, dict) else j
                if ptid:
                    puestos.add(str(ptid))

        faltan = sorted(pedidos - puestos)
        if len(puestos) == 11 and not faltan:
            return {"verificado": True, "en_la_api": 11}

        # Un once de 10 anula la jornada entera.
        log.error("el once guardado tiene %d jugadores, no 11. LaLiga descartó: %s",
                  len(puestos), faltan or "(no identificados)")
        return {"ok": False, "verificado": True, "en_la_api": len(puestos),
                "descartados": faltan,
                "aviso": (f"LaLiga aceptó el PUT pero el once quedó en {len(puestos)}: "
                          f"descartó {', '.join(faltan) or 'a alguien'}. "
                          f"Un once incompleto anula la jornada entera.")}
