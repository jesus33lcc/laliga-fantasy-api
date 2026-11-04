"""Política de venta por jugador: decide si una oferta recibida se cobra o se espera.

La política no decide si la venta está permitida (eso lo resuelven las guardas de
`ingest/ofertas.py`, ver abajo): solo elige entre "cobrar" y "esperar".

La mecánica de la que salen los modos

LaLiga oferta por lo que tengas listado un importe aleatorio entre el valor de mercado y
±10 %, y esa oferta caduca en el siguiente ciclo de mercado. Como rechazar no cuesta
nada, cada ciclo es una tirada nueva sobre el mismo jugador.

Lo que limita la espera es que la ficha de venta dura 3 días: si caduca y nadie la
vuelve a listar, se acaban las tiradas. Por eso `relistar` forma parte de la política.

Medido sobre 8 ofertas de este equipo: aceptar la primera rinde +0,93 % sobre el
valor de mercado y esperar a una que lo supere, +7,36 % (salió en 3 de los 8 ciclos).
Diferencia de +6,4 puntos porcentuales. Es una muestra pequeña y de una sola liga: da
el orden de magnitud, no una rentabilidad garantizada.

Los cuatro modos

| modo | cobra cuando | para qué |
|---|---|---|
| `avisar` | nunca | lo decide una persona, con el aviso delante |
| `prima` (por defecto) | la oferta supera el valor en `prima_min` % | esperar a una tirada alta |
| `valor` | la oferta llega al valor de mercado | salir sin perder, sin hilar fino |
| `inmediata` | llega cualquier oferta del sistema | hace falta caja ya y se asume el descuento |

`inmediata` no es la venta inmediata al 50 %: eso es `/accion/venta-inmediata`. Aquí
sigue aplicándose el suelo de `LIMITES.min_precio_venta` (90 %); lo que cambia es que no
se espera a una oferta mejor.

Dos condiciones que se cruzan con el calendario

- `no_antes_de_congelar`: no cobrar mientras la jornada en curso no se haya congelado.
  Vender a un titular antes deja el once en diez y la jornada entera puntúa 0. Después
  ya no afecta a esa jornada, porque puntúa el once guardado al abrirse.
- `hasta`: a partir de esa fecha se avisa de que el plazo se acaba. No baja el listón
  por su cuenta.

Guardas que van por delante y esta política no puede saltarse

En `ingest/ofertas.py`: solo se cobran solas las ofertas del sistema (la de otro manager
se avisa y la decide una persona), no se baja del 90 % del valor de mercado, y no se vende
si el equipo queda sin margen para alinear.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import text

from .db import Session, ddl

log = logging.getLogger("coconut.ventas")

MODOS = ("avisar", "prima", "valor", "inmediata")

# Se aplica a todo jugador listado que no tenga política propia. El por defecto espera a
# una prima del 3 %: cobrar la primera oferta que pase hay que pedirlo con modo `valor` o
# `inmediata`.
POR_DEFECTO: dict[str, Any] = {
    "modo": "prima",
    "prima_min": 3.0,
    "no_antes_de_congelar": True,
    "relistar": True,
    "hasta": None,
}

DDL = """
CREATE TABLE IF NOT EXISTS politicas_venta (
    player_team_id       text PRIMARY KEY,
    nombre               text,
    modo                 text        NOT NULL DEFAULT 'prima',
    prima_min            double precision NOT NULL DEFAULT 3.0,
    no_antes_de_congelar boolean     NOT NULL DEFAULT true,
    relistar             boolean     NOT NULL DEFAULT true,
    hasta                timestamptz,
    precio               bigint,
    creada               timestamptz NOT NULL DEFAULT (now()),
    nota                 text
)
"""


async def crear_tabla() -> None:
    async with Session() as s:
        for sentencia in ddl(DDL).split(";"):
            if sentencia.strip():
                await s.execute(text(sentencia))
        await s.commit()


async def guardar(player_team_id: str, nombre: str, **campos: Any) -> dict:
    """Crea o actualiza la política de un jugador. Solo toca lo que se le pasa."""
    modo = str(campos.get("modo") or POR_DEFECTO["modo"]).lower()
    if modo not in MODOS:
        raise ValueError(f"modo desconocido: {modo}. Se acepta: {', '.join(MODOS)}")
    fila = {
        "p": str(player_team_id), "n": nombre, "m": modo,
        "pm": float(campos.get("prima_min", POR_DEFECTO["prima_min"])),
        "nc": bool(campos.get("no_antes_de_congelar", POR_DEFECTO["no_antes_de_congelar"])),
        "rl": bool(campos.get("relistar", POR_DEFECTO["relistar"])),
        "h": campos.get("hasta"),
        "pr": campos.get("precio"),
        "nt": campos.get("nota") or "",
    }
    await crear_tabla()
    async with Session() as s:
        await s.execute(text("""
            INSERT INTO politicas_venta (player_team_id, nombre, modo, prima_min,
                                         no_antes_de_congelar, relistar, hasta, precio, nota)
            VALUES (:p, :n, :m, :pm, :nc, :rl, :h, :pr, :nt)
            ON CONFLICT (player_team_id) DO UPDATE SET
                nombre = EXCLUDED.nombre, modo = EXCLUDED.modo,
                prima_min = EXCLUDED.prima_min,
                no_antes_de_congelar = EXCLUDED.no_antes_de_congelar,
                relistar = EXCLUDED.relistar, hasta = EXCLUDED.hasta,
                precio = EXCLUDED.precio, nota = EXCLUDED.nota"""), fila)
        await s.commit()
    log.warning("política de venta de %s: modo=%s prima_min=%.1f%% relistar=%s",
                nombre, modo, fila["pm"], fila["rl"])
    return await politica_de(player_team_id)


async def borrar(player_team_id: str) -> bool:
    await crear_tabla()
    async with Session() as s:
        r = await s.execute(text("DELETE FROM politicas_venta WHERE player_team_id = :p"),
                            {"p": str(player_team_id)})
        await s.commit()
        return bool(r.rowcount)


async def politica_de(player_team_id: str) -> dict:
    """La política de ese jugador, o `POR_DEFECTO` si no tiene una propia.

    Nunca devuelve `None`, así que el llamante no tiene que distinguir los dos casos. El
    campo `por_defecto` dice de dónde salió.
    """
    await crear_tabla()
    async with Session() as s:
        fila = (await s.execute(text(
            "SELECT * FROM politicas_venta WHERE player_team_id = :p"),
            {"p": str(player_team_id)})).mappings().first()
    if not fila:
        return {**POR_DEFECTO, "player_team_id": str(player_team_id), "por_defecto": True}
    d = dict(fila)
    d["por_defecto"] = False
    return d


async def listar() -> list[dict]:
    await crear_tabla()
    async with Session() as s:
        return [dict(r) for r in (await s.execute(text(
            "SELECT * FROM politicas_venta ORDER BY nombre"))).mappings()]


def decidir(politica: dict, *, importe: int, valor: int,
            jornada_congelada: bool) -> tuple[str, str]:
    """Qué hacer con una oferta recibida, según la política del jugador.

    Devuelve `("cobrar" | "esperar" | "avisar", motivo)`. El motivo va redactado para
    mostrarse tal cual en el aviso.

    `no_antes_de_congelar` se comprueba antes que el modo, así que con la jornada sin
    congelar se espera sea cual sea el modo. Función pura: no consulta ni escribe nada.
    """
    prima = (importe / valor - 1) * 100 if valor else 0.0
    modo = str(politica.get("modo") or "prima")

    if politica.get("no_antes_de_congelar") and not jornada_congelada:
        return ("esperar", "la jornada aún no se ha congelado: vender ahora puede dejar "
                           "el once en diez")

    hasta = politica.get("hasta")
    vencido = bool(hasta) and _aware(hasta) <= dt.datetime.now(dt.UTC)

    if modo == "avisar":
        return ("avisar", f"oferta de {prima:+.1f} % sobre el valor; la apruebas tú")
    if modo == "inmediata":
        return ("cobrar", f"modo inmediata: se cobra {prima:+.1f} % sin esperar")
    if modo == "valor":
        if importe >= valor:
            return ("cobrar", f"llega al valor de mercado ({prima:+.1f} %)")
        return ("esperar", f"{prima:+.1f} %, por debajo del valor de mercado")

    # modo prima
    minimo = float(politica.get("prima_min") or 0)
    if prima >= minimo:
        return ("cobrar", f"{prima:+.1f} % supera la prima pedida ({minimo:+.1f} %)")
    if vencido:
        return ("avisar", f"{prima:+.1f} % no llega a {minimo:+.1f} %, pero el plazo venció")
    return ("esperar", f"{prima:+.1f} % no llega a la prima pedida ({minimo:+.1f} %)")


def _aware(v: Any) -> dt.datetime:
    """Una fecha con zona, venga como `datetime` o como texto ISO.

    En SQLite un `timestamptz` puede volver como texto, y comparar texto con `datetime`
    revienta. Un valor ingenuo se toma como UTC, que es como se guarda.
    """
    t = v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(
        str(v).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)
