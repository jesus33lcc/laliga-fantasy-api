"""Salud del sistema e invariantes de los datos.

Dos cosas distintas, las dos necesarias:

  - Salud: ¿sigue entrando información? Un sistema parado no da error, simplemente
    deja de servir: y en este juego eso se traduce en perder una ventana de cláusula
    sin enterarte.
  - Invariantes: ¿los datos tienen sentido? Un supuesto equivocado sobre un campo de
    la API no lanza ninguna excepción: produce una lista de objetivos falsa y el sistema
    sigue funcionando como si nada. Estas comprobaciones existen para que eso se vea.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import text

from .clients.auth import SinCredenciales, tokens
from .db import Session
from .tiempo import ahora, dentro_de, horas_entre

log = logging.getLogger("coconut.salud")

# Umbrales: el maestro se recoge 4 veces al día y la liga cada 30 min. Se da margen
# generoso para no avisar por un retraso normal, pero no tanto como para dormirse.
MAX_HORAS_PRECIOS = 10.0
MAX_HORAS_LIGA = 5.0


@dataclass
class Chequeo:
    nombre: str
    ok: bool
    detalle: str


@dataclass
class Informe:
    chequeos: list[Chequeo] = field(default_factory=list)

    def añadir(self, nombre: str, ok: bool, detalle: str) -> None:
        self.chequeos.append(Chequeo(nombre, ok, detalle))

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.chequeos)

    @property
    def fallos(self) -> list[Chequeo]:
        return [c for c in self.chequeos if not c.ok]

    def texto(self) -> str:
        return "\n".join(f"[{'ok' if c.ok else 'FALLO'}] {c.nombre}: {c.detalle}"
                         for c in self.chequeos)


# La edad se calcula en Python, no en SQL: `EXTRACT(epoch FROM …)` es de Postgres y no
# tiene forma equivalente en SQLite. Se pide la fecha y `horas_entre()` hace la resta.
#
# La anotación `[TIMESTAMPTZ]` del alias hace falta porque un agregado como `max(ts)`
# pierde el tipo declarado de la columna, así que en SQLite volvería como texto.
# Con ella vuelve como `datetime`. (`horas_entre` acepta las dos formas de todos modos,
# pero es mejor que el tipo sea correcto desde la consulta.)
_EDAD = text('SELECT max(ts) AS "u [TIMESTAMPTZ]" FROM market_values')
_EDAD_LIGA = text('SELECT max(ts) AS "u [TIMESTAMPTZ]" FROM squad_snapshots')

# --- invariantes ---
_INV = {
    "precios sin jugadores":
        "SELECT count(*) FROM market_values mv "
        "LEFT JOIN players p ON p.id=mv.player_id WHERE p.id IS NULL",
    "jugador en dos plantillas a la vez":
        "SELECT count(*) FROM (SELECT player_id FROM squad_snapshots "
        "WHERE ts=(SELECT max(ts) FROM squad_snapshots) "
        "GROUP BY player_id HAVING count(DISTINCT manager_id) > 1) x",
    # Faltan dos invariantes a propósito, y por el mismo motivo: "cláusula por debajo del
    # valor" es lo normal cuando un fichaje se revaloriza (la cláusula no sigue al valor
    # hacia arriba), y "saldo negativo" lo permite el juego hasta el 20 % del valor del
    # equipo. `politica.datos_fiables()` devuelve falso si cualquier chequeo está en rojo,
    # así que un invariante que salta ante algo legal no protege: paraliza al piloto.
    "deuda por encima del 20 % del equipo":
        "SELECT count(*) FROM managers "
        "WHERE balance_est < 0 AND team_value IS NOT NULL "
        "AND balance_est < -0.20 * team_value",
    # El corte llega como parámetro (`:tope_proteccion`) en vez de `now() + interval
    # '15 days'`: el `interval` de Postgres no existe en SQLite y no hay forma de
    # escribirlo que valga en los dos. Se calcula en Python, en el bucle de abajo.
    "protección de más de 15 días":
        "SELECT count(*) FROM squad_snapshots "
        "WHERE ts=(SELECT max(ts) FROM squad_snapshots) "
        "AND clause_lock_until > :tope_proteccion",
    "valores de mercado a cero":
        "SELECT count(*) FROM v_latest_values WHERE value <= 0",
    # El supuesto del que cuelgan todas las consultas de plantilla.
    #
    # Ocho consultas repartidas por `models/`, `radar.py` y `piloto.py` seleccionan el
    # último snapshot con `ts = (SELECT max(ts) FROM squad_snapshots)`: un máximo
    # global, no por manager, y solo es correcto porque la ingesta escribe a los cuatro
    # con el mismo `ts`. Si uno se escribe un segundo más tarde, ese máximo lo encuentra
    # solo a él y los demás desaparecen de todas esas consultas: la exposición sale vacía
    # y nada da error.
    "managers fuera del último snapshot":
        "SELECT count(*) FROM managers m WHERE NOT EXISTS ("
        "  SELECT 1 FROM squad_snapshots s"
        "   WHERE s.manager_id = m.id"
        "     AND s.ts = (SELECT max(ts) FROM squad_snapshots))",
}


async def revisar() -> Informe:
    inf = Informe()

    # Con el circuito abierto no sale NINGUNA petición: el cliente se para en seco ante
    # un 403. Si `/health` no lo mirara, devolvería 200 con todo en verde mientras el bot
    # está parado, y el vigilante externo callaría con él.
    try:
        from .api.deps import _cliente as _cli
        if _cli is not None and getattr(_cli, "bloqueado", False):
            inf.añadir("transporte a LaLiga", False,
                       f"🚨 CIRCUITO ABIERTO: {getattr(_cli, 'motivo_bloqueo', '403')}. "
                       "Ninguna petición sale. Comprueba si LaLiga responde y reactiva.")
        elif _cli is not None:
            inf.añadir("transporte a LaLiga", True, "circuito cerrado")
    except ImportError:
        # Fuera del proceso de la API no hay cliente compartido que mirar. Se captura
        # solo el import: con `except Exception` un error de tecleo aquí dejaría el
        # chequeo del circuito en silencio, y es justo el que avisa de un WAF.
        pass

    # 1) ¿Entran datos?
    try:
        async with Session() as s:
            h_precios = horas_entre(ahora(), (await s.execute(_EDAD)).scalar())
            h_liga = horas_entre(ahora(), (await s.execute(_EDAD_LIGA)).scalar())
    except Exception as e:
        inf.añadir("base de datos", False, f"no responde: {e}")
        return inf

    if h_precios is None:
        inf.añadir("precios", False, "no hay ni una muestra")
    else:
        inf.añadir("precios", float(h_precios) < MAX_HORAS_PRECIOS,
                   f"última hace {float(h_precios):.1f} h")

    if h_liga is None:
        inf.añadir("liga", False, "sin snapshots de plantillas")
    else:
        inf.añadir("liga", float(h_liga) < MAX_HORAS_LIGA,
                   f"último hace {float(h_liga):.1f} h")

    # 2) ¿Sigue viva la sesión?
    #
    # Lo que importa es si el refresh sigue sirviendo, que es lo único que obliga a un
    # login humano. Pedir un bearer lo responde: si toca renovar renueva, y si el refresh
    # está muerto salta. Con el token en vigor no cuesta ni una petición.
    #
    # El `exp` del id_token no vale: dura ~1 h y se renueva solo. Y `tokens._expira` está
    # cacheado por proceso, así que el contenedor `api` reportaría su copia rancia aunque
    # el `worker` lleve horas renovando.
    if not tokens._refresh:
        inf.añadir("sesión", False, "sin refresh token hace falta scripts/login.py")
    else:
        try:
            await tokens.bearer()
            inf.añadir("sesión", True, "sesión viva (el id_token se renueva solo)")
        except SinCredenciales as e:
            inf.añadir("sesión", False, f"refresh rechazado → scripts/login.py ({e})")
        except Exception as e:
            inf.añadir("sesión", False, f"no se pudo renovar la sesión: {e}")

    # 3) Invariantes: si alguno salta, hay un supuesto roto.
    # Los parámetros van todos juntos: SQLAlchemy ignora los que una consulta no use,
    # así que no hace falta emparejarlos uno a uno con su invariante.
    params = {"tope_proteccion": dentro_de(dias=15)}
    async with Session() as s:
        for nombre, sql in _INV.items():
            try:
                n = int((await s.execute(text(sql), params)).scalar() or 0)
                inf.añadir(f"invariante: {nombre}", n == 0,
                           "ninguno" if n == 0 else f"{n} casos ⚠️")
            except Exception as e:
                inf.añadir(f"invariante: {nombre}", False, f"no se pudo comprobar: {e}")

    return inf
