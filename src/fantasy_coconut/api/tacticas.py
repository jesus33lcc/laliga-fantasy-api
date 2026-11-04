"""`/tacticas/*`: el catálogo de jugadas de mercado, en modo PROPUESTA.

Nada de aquí ejecuta. Cada endpoint devuelve qué haría, con cuánto gana, cuánto cuesta
y qué lo falsaría; mover ficha sigue pasando por `/accion/*`, con sus frenos.
Implementar una táctica no es adoptarla: está aquí para que quien quiera usarla pueda,
con el número delante.

Cada respuesta se explica sola

Los endpoints están escritos para entenderse sin leer el código. Las dos tácticas que
proponen operaciones (`escaparate` y `clausulas-baratas`) traen el *porqué*, los
descartados con su motivo y un campo `que_lo_falsaria`: quien solo recibe una lista de
candidatos tiene que rehacer el análisis para saber si fiarse. El catálogo y la
radiografía de rivales son lectura y no los llevan.

`GET /tacticas` enumera el catálogo: es el punto de entrada.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from ..config import settings
from ..models import tacticas as T
from .deps import bearer, cliente, protegido

log = logging.getLogger("coconut.api.tacticas")
router = APIRouter(prefix="/tacticas", tags=["tácticas"])

CMP = "/v1/competition/1"


async def _plantilla(tok: str, team_id: str) -> list[dict]:
    d = await protegido(cliente().get(
        f"{CMP}/leagues/{settings.fantasy_league_id}/teams/{team_id}", ttl=120, auth=tok))
    return d.get("players") or []


async def _mercado(tok: str) -> list[dict]:
    m = await protegido(cliente().get(
        f"{CMP}/league/{settings.fantasy_league_id}/market", ttl=120, auth=tok))
    return m if isinstance(m, list) else []


@router.get("", summary="El catálogo: qué tácticas hay y qué hace cada una")
async def catalogo():
    """Punto de entrada. Enumera las tácticas con su ventaja medida y su endpoint.

    Deliberadamente sin llamadas a LaLiga: sirve para orientarse, y orientarse no debería
    costar cuota.
    """
    return {
        "aviso": ("Todo esto PROPONE. Ejecutar pasa por /accion/*, que tiene los frenos. "
                  "Que una táctica esté implementada no significa que se use."),
        "tacticas": [
            {
                "id": "escaparate",
                "endpoint": "GET /tacticas/escaparate",
                "titulo": "Listar y esperar la oferta buena",
                "idea": ("LaLiga oferta por lo que tengas en venta, aleatorio entre el "
                         "valor y ±10 %, y la oferta caduca al siguiente ciclo. Como "
                         "puedes rechazar, listar es quedarse una opción GRATIS y "
                         "ejercerla solo cuando sale cara."),
                "ventaja_medida": ("+6,4 pp por venta: +0,93 % aceptando la primera oferta "
                                   "frente a +7,36 % esperando a una por encima del valor"),
                "quien_la_usa": ("se reconoce por tener casi toda la plantilla listada; "
                                 "GET /tacticas/rivales lo mide en tu liga"),
                "muestra": "n=8 ofertas medidas, ordena, no promete",
            },
            {
                "id": "clausulas_baratas",
                "endpoint": "GET /tacticas/clausulas-baratas",
                "titulo": "Subir cláusula solo donde sale barato",
                "idea": ("Con factor=2 la subida cuesta la MITAD de lo que sube. Proteger "
                         "a un jugador de valor bajo es calderilla; a uno caro, inviable. "
                         "Y a quien ya está fuera del alcance de todos no se le sube nada."),
                "ventaja_medida": "coste por punto protegido, calculado por jugador",
                "quien_la_usa": "recomendación estándar de las guías de la comunidad",
            },
            {
                "id": "radiografia_rivales",
                "endpoint": "GET /tacticas/rivales",
                "titulo": "Qué táctica usa cada rival",
                "idea": ("La plantilla y el mercado rival son públicos: cuánto lista cada "
                         "uno y con qué prima dice qué está jugando, gratis."),
                "ventaja_medida": "(es lectura, no operación)",
            },
        ],
    }


@router.get("/escaparate", summary="T1 · A quién listar para recoger la oferta buena")
async def escaparate(umbral_pct: float = Query(
        T.UMBRAL_ACEPTAR, ge=-10, le=10,
        description="Prima mínima sobre el valor a partir de la cual compensa aceptar.")):
    """Los jugadores que conviene tener listados de forma permanente, y los descartes.

    Cómo se usa la respuesta: listar con `POST /accion/vender`, y dejar que el
    seguimiento `venta` vigile la ficha. Ese cobra solo las ofertas del sistema; la de
    otro manager la deja pasar y avisa, porque a quién se le vende es una decisión y no
    un cobro de trámite.

    Los titulares del once y quien haga falta para cubrir el mínimo de su posición
    salen en `descartados`, no en `candidatos`: proponer algo que la API va a rechazar con
    un 423 es ruido.
    """
    tok = await bearer()
    plantilla = await _plantilla(tok, settings.fantasy_team_id)
    mercado = await _mercado(tok)
    hist = await protegido(cliente().get(
        f"{CMP}/league/{settings.fantasy_league_id}/market/history", ttl=600, auth=tok))

    # El histórico crudo no trae el % sobre el valor: eso lo calcula /informe. Aquí se
    # pide ya cocinado para no duplicar el cruce con la serie de precios.
    from .informe import mercado_historico
    try:
        ops = (await mercado_historico()).get("operaciones") or []
    except Exception:
        log.warning("sin histórico cocinado: se usan los valores de referencia")
        ops = []

    titulares = set()
    try:
        ln = await protegido(cliente().get(
            f"{CMP}/teams/{settings.fantasy_team_id}/lineup", ttl=120, auth=tok))
        f = ln.get("formation") or {}
        for k in ("goalkeeper", "defender", "midfield", "striker"):
            v = f.get(k) or []
            for p in (v if isinstance(v, list) else [v]):
                titulares.add(str(p.get("playerTeamId")))
    except Exception:
        log.warning("no se pudo leer el once: no se filtran titulares")

    res = T.escaparate(plantilla, mercado, ops, mi_team_id=settings.fantasy_team_id,
                       titulares=titulares, umbral_pct=umbral_pct)
    res["historico_crudo_n"] = len(hist) if isinstance(hist, list) else 0
    return res


@router.get("/clausulas-baratas", summary="T2 · Dónde sale rentable subir la cláusula")
async def clausulas_baratas(
        modo: str = Query("disuasorio", pattern="^(disuasorio|inalcanzable)$",
                          description="`disuasorio`: objetivo = valor × ratio, que es lo "
                                      "que recomiendan las guías y sale barato en los "
                                      "jugadores de valor bajo. `inalcanzable`: objetivo "
                                      "= caja del rival más rico + 1 €, que cuesta lo "
                                      "mismo en euros para todos y es desorbitado en los "
                                      "baratos."),
        objetivo_ratio: float = Query(
            1.6, ge=1.0, le=5.0,
            description="Solo en modo `disuasorio`: cláusula objetivo como múltiplo del "
                        "valor de mercado.")):
    """Ordenado por coste por punto protegido, no por puntos.

    Dos filtros cambian la respuesta entera, y son los mismos que aplica el blindaje: se
    descarta a quien ningún rival puede pagar (para clausular no se puede uno endeudar)
    y a los `out_of_league` (si se lo llevan, te hacen un favor).

    Ejecutar con `POST /accion/subir-clausula`, pasando `value_to_increase` como
    `importe`: ese endpoint solo lee `jugador` e `importe`. El `factor: 2` lo pone
    `Acciones.subir_clausula()` y no se manda; `factor=1` y `factor=3` no existen.
    """
    tok = await bearer()
    plantilla = await _plantilla(tok, settings.fantasy_team_id)

    from ..models.saldos import estimar
    saldos, _ = await estimar()
    caja_max = max((s.estimado or 0 for s in saldos if not s.es_mio), default=0)

    res = T.clausulas_baratas(plantilla, caja_max, objetivo_ratio=objetivo_ratio,
                              modo=modo)
    res["aviso_caja"] = ("La caja rival es una COTA SUPERIOR: no se ven sus subidas de "
                         "cláusula. El real es igual o menor, así que esto protege de más.")
    return res


@router.get("/rivales", summary="T3 · Qué táctica de mercado usa cada rival")
async def rivales():
    """Deducido de lo que es público: cuánto lista cada uno y con qué prima.

    Un `pct_listado` del 100 % es la firma del escaparate. Y una `prima_media_pct`
    negativa no es un descuento: el precio de salida no baja el suelo, porque una oferta
    de otro manager tiene que ser >= valor de mercado igual.
    """
    tok = await bearer()
    mercado = await _mercado(tok)
    st_ = await protegido(cliente().get(
        f"{CMP}/leagues/{settings.fantasy_league_id}/standing", ttl=300, auth=tok))

    plantillas: dict[str, int] = {}
    for t in (st_ if isinstance(st_, list) else st_.get("data", [])):
        eq = t.get("team") or {}
        nombre = (eq.get("manager") or {}).get("managerName") or eq.get("name")
        try:
            jug = await _plantilla(tok, str(eq.get("id")))
            plantillas[nombre] = len(jug)
        except Exception:
            log.warning("sin plantilla de %s", nombre)

    return T.radiografia_rivales(mercado, plantillas)
