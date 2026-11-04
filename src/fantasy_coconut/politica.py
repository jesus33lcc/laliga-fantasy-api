"""Los límites del piloto automático. Este fichero es el freno de mano.

Cualquier operación con dinero pasa por aquí antes de ejecutarse. Si algo no encaja, no
se hace: no hay ruta alternativa ni modo insistir. El riesgo que cubre no es el de una
mala decisión, es el de un bug que gasta solo mientras nadie mira.

Modo de trabajo (`COCONUT_MODO`):
    sombra  -> decide y lo registra todo, pero no toca la API. Modo de pruebas puro: ni
               siquiera guarda la alineación.
    agente  -> el modo por defecto. Vigila, ingiere datos y guarda la alineación (no
               cuesta dinero, y no guardarla son 0 puntos), pero no mueve un euro solo.
    real    -> ejecuta sin preguntar. Pensado para procesos puntuales, no continuos.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from dataclasses import dataclass

from sqlalchemy import bindparam, text

from . import entorno as _env
from .db import Session

log = logging.getLogger("coconut.politica")

SOMBRA, AGENTE, REAL = "sombra", "agente", "real"

# `confirmar` no es un modo válido: exigiría un bot de botones que pidiera permiso por
# cada propuesta. El nombre se conserva solo para caer a `agente` en vez de fallar si
# alguien lo deja puesto en el `.env`.
CONFIRMAR = "confirmar"


@dataclass(frozen=True)
class Limites:
    max_por_operacion: int = 20_000_000

    # Caja que el piloto no puede tocar. Es deliberadamente baja, y el motivo importa:
    # una reserva no protege de que te clausulen a un jugador (cuando lo hacen, te pagan
    # la cláusula), solo sirve para tener con qué reaccionar. Puesta demasiado alta, el
    # sistema descarta todo refuerzo y cambia un daño cierto (una plaza rindiendo cero)
    # por cobertura de uno probable.
    reserva_intocable: int = 3_000_000

    # Tope de gasto al día. Frena al piloto, no a una orden dada por una persona: eso
    # lo decide `orden_explicita()`.
    max_gasto_diario: int = 25_000_000
    max_operaciones_dia: int = 3

    # Nunca vender por debajo de este % del valor de mercado.
    min_precio_venta: float = 0.90
    # Tope de sobrecoste en un CLAUSULAZO. Las pujas no pasan por aquí.
    max_ratio_compra: float = 1.05
    # Declarado y sin cablear: hoy no lo lee nadie.
    min_muestras_precio: int = 3
    # Nota mínima de calidad (0..1) para fichar. La lee `piloto.py` al filtrar objetivos
    # de clausulazo.
    #
    # No equivale a 100 puntos, aunque `calidad.evaluar()` parta de `puntos / 200`: la
    # nota se multiplica después por la titularidad. Lo que pide 0,5 de verdad:
    #   sin dato de titularidad -> 100 pts
    #   con 0,80                -> 125
    #   con 0,70                -> 143
    #   con 0,50                -> solo pasa quien llegue al tope de 200
    # Subir este número aprieta mucho más de lo que sugiere su escala.
    min_nota_calidad: float = 0.5

    # Declarado y sin cablear: hoy no lo lee nadie.
    intocables: frozenset[str] = frozenset()


LIMITES = Limites()

# Lo único que se respeta incluso tapando un incendio: quedarse literalmente sin caja te
# deja indefenso ante el siguiente, y siempre hay un siguiente.
COLCHON_MINIMO = 2_000_000


def modo() -> str:
    m = _env.var("MODO", AGENTE).strip().lower()
    if m == CONFIRMAR:
        # Caer a `agente` en vez de fallar: quedarse sin modo es quedarse sin alinear.
        log.warning("COCONUT_MODO=confirmar no es un modo válido. Se usa 'agente': "
                    "el bot vigila y alinea, pero no mueve dinero solo.")
        return AGENTE
    return m if m in (SOMBRA, AGENTE, REAL) else AGENTE


# ---------- órdenes explícitas ----------
#
# `COCONUT_MODO=agente` existe para que el piloto automático no gaste solo. No para
# frenar una orden que ha dado una persona: si programas una venta o pulsas el botón en
# la interfaz, esa decisión ya está tomada y frenarla no protege de nada; solo consigue
# que el sistema diga "hecho" sin hacer nada.
#
# `contextvars` y no una global: cada petición y cada tarea tienen el suyo, así que dos
# cosas a la vez no se pisan.
_ORDEN_EXPLICITA: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "coconut_orden_explicita", default=False)


@contextlib.contextmanager
def orden_explicita():
    """Dentro de esto, lo que escriba se escribe de verdad. Solo lo usan `/accion/*` y
    las tareas programadas: los dos caminos por los que pasa una decisión humana."""
    testigo = _ORDEN_EXPLICITA.set(True)
    try:
        yield
    finally:
        _ORDEN_EXPLICITA.reset(testigo)


def es_real() -> bool:
    return modo() == REAL or _ORDEN_EXPLICITA.get()


@dataclass
class Veredicto:
    permitido: bool
    motivo: str

    def __bool__(self) -> bool:
        return self.permitido


# Solo cuentan las acciones que GASTAN: un ingreso no es un gasto, y un tope que se llena
# vendiendo deja al sistema sin poder fichar teniendo más caja que antes.
_ACCIONES_GASTO = ("puja", "refuerzo_puja", "emergencia_puja", "clausulazo",
                   "emergencia_clausula", "oferta", "emergencia_oferta",
                   "subir_clausula")

# El tope diario cuenta lo del modo actual y nunca lo mezcla: en sombra, las simulaciones,
# para que el ensayo sienta los mismos límites; operando de verdad, solo lo ejecutado. Si
# se mezclaran, unas pruebas en sombra bloquearían una compra real.
#
# A qué alcanza en modo `agente`, que es donde corre esto: la rama que aplica es
# `ejecutada`, y se marca en lo que sale por `/accion/*` bajo `orden_explicita()`. O sea
# que el tope mide el gasto pedido por una persona, que es el único que mueve dinero en
# este modo. Lo que el piloto propone y no ejecuta queda con `ejecutada=false` y no suma.
_GASTO_HOY = text("""
    SELECT COALESCE(SUM(importe), 0), COUNT(*)
    FROM decisions
    WHERE importe > 0 AND ts >= date_trunc('day', now())
      AND accion IN :acciones
      AND ((:sombra AND modo = 'sombra' AND resultado LIKE '%sombra%')
           OR (NOT :sombra AND ejecutada))
""").bindparams(bindparam("acciones", expanding=True))
# `= ANY(:acciones)` es de Postgres. `IN :acciones` con `expanding=True` lo expande
# SQLAlchemy a tantos marcadores como elementos, y funciona en los dos motores.


async def gasto_de_hoy() -> tuple[int, int]:
    async with Session() as s:
        fila = (await s.execute(_GASTO_HOY, {"sombra": modo() == SOMBRA,
                                             "acciones": list(_ACCIONES_GASTO)})).first()
        return (int(fila[0] or 0), int(fila[1] or 0)) if fila else (0, 0)


async def puede_gastar(importe: int, saldo: int,
                       emergencia: bool = False,
                       orden_humana: bool = False) -> Veredicto:
    """El portero. Todo gasto pasa por aquí.

    `emergencia=True` es para cuando falta un jugador de una posición y no se puede
    alinear un once válido. Ahí los topes normales juegan en contra: están calibrados
    para compras de oportunidad, y aplicarlos a un incendio significa quedarse sin
    portero por respetar una reserva que existe justamente para poder reaccionar. Una
    jornada sin once cuesta más que pagar caro.

    `orden_humana=True` levanta el tope por operación y el diario, y solo lo levanta quien
    ya ha repetido el importe a mano (`importe_confirmado`). Mismo principio que
    `orden_explicita()`: ese tope protege del automatismo, no del usuario. Está
    calibrado para compras de oportunidad del piloto; aplicarlo a una decisión tomada por
    una persona no protege de nada, solo le impide fichar a quien valga la pena.

    Lo que no se relaja nunca, ni en emergencia ni por orden humana: no gastar dinero que
    no se tiene, y dejar un colchón mínimo para no quedarse en cero absoluto.
    """
    if importe <= 0:
        return Veredicto(False, "importe no positivo")

    if emergencia:
        if importe > saldo - COLCHON_MINIMO:
            return Veredicto(False, f"ni en emergencia: {importe/1e6:.1f}M dejaría "
                                    f"menos de {COLCHON_MINIMO/1e6:.1f}M de colchón "
                                    f"(saldo {saldo/1e6:.1f}M)")
        return Veredicto(True, "emergencia: topes ordinarios relajados")

    if importe > LIMITES.max_por_operacion:
        if not orden_humana:
            return Veredicto(False, f"{importe/1e6:.1f}M supera el tope por operación "
                                    f"({LIMITES.max_por_operacion/1e6:.0f}M)")
        # Se deja rastro: una operación que pasa por encima de un freno tiene que poder
        # explicarse después, y el log es lo único que queda cuando alguien pregunta por
        # qué salió ese importe.
        log.warning("tope por operación (%.0fM) levantado por orden humana: %.2fM",
                    LIMITES.max_por_operacion / 1e6, importe / 1e6)

    if saldo - importe < LIMITES.reserva_intocable:
        return Veredicto(False, f"dejaría el saldo en {(saldo-importe)/1e6:.1f}M, "
                                f"por debajo de la reserva "
                                f"({LIMITES.reserva_intocable/1e6:.0f}M)")

    gastado, n = await gasto_de_hoy()
    if n >= LIMITES.max_operaciones_dia:
        return Veredicto(False, f"ya van {n} operaciones hoy (tope "
                                f"{LIMITES.max_operaciones_dia})")
    if gastado + importe > LIMITES.max_gasto_diario:
        # Mismo razonamiento que el tope por operación: es el presupuesto del piloto para
        # un día, no un límite de la liga. El contador de operaciones de arriba sí sigue
        # aplicando siempre, porque ese no mide dinero: frena un bucle que se repite.
        if not orden_humana:
            return Veredicto(False, f"con {gastado/1e6:.1f}M ya gastados hoy, esto pasaría "
                                    f"del tope diario ({LIMITES.max_gasto_diario/1e6:.0f}M)")
        log.warning("tope diario (%.0fM) levantado por orden humana: %.2fM sobre %.2fM "
                    "ya gastados", LIMITES.max_gasto_diario / 1e6, importe / 1e6,
                    gastado / 1e6)

    return Veredicto(True, "dentro de los límites")


async def datos_fiables() -> Veredicto:
    """Sin datos frescos y coherentes no se toca nada. Reusa los invariantes de salud."""
    from .salud import revisar
    inf = await revisar()
    if not inf.ok:
        return Veredicto(False, "chequeos en rojo: " +
                         "; ".join(c.nombre for c in inf.fallos))
    return Veredicto(True, "datos al día")


async def registrar(accion: str, objetivo: str, importe: int, motivo: str,
                    ejecutada: bool, resultado: str = "") -> None:
    """Todo queda escrito, se ejecute o no. En modo sombra esto es el entregable."""
    async with Session() as s:
        await s.execute(text("""
            INSERT INTO decisions (ts, modo, accion, objetivo, importe, motivo,
                                   ejecutada, resultado)
            VALUES (now(), :modo, :accion, :objetivo, :importe, :motivo,
                    :ejecutada, :resultado)
        """), {"modo": modo(), "accion": accion, "objetivo": objetivo,
               "importe": importe, "motivo": motivo, "ejecutada": ejecutada,
               "resultado": resultado})
        await s.commit()
