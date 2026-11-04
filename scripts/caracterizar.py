#!/usr/bin/env python3
"""Fotografía la FORMA de todo lo que devuelve Coconut, y avisa si cambia.

Para qué: la red antes de refactorizar

No hay tests unitarios (sí pruebas de extremo a extremo: `probar_web.mjs`,
`probar_respaldo.py`, `comparar_motores.py`). Tocar un sistema que mueve dinero sin nada
que diga "esto sigue devolviendo lo mismo" es la forma habitual de romper algo en
silencio.

Lo que hace falta no es un test por función (eso es meses) sino una alarma barata: se
guarda cómo es hoy la respuesta de cada endpoint y de cada modelo, y después de tocar el
código se vuelve a mirar. Si algo dejó de devolver un campo, o empezó a devolver texto
donde había un número, sale aquí.

Por qué la FORMA y no los valores

`comparar_motores.py` compara valores, y para lo suyo está bien: los dos motores leen los
mismos datos en el mismo instante. Aquí no sirve: los precios cambian a las 00:15, la
caja cambia con cada operación y la jornada avanza. Comparar valores daría una alarma cada
día y en dos semanas nadie la miraría.

La forma, en cambio, solo cambia cuando cambia el código. Y es justo la familia de
fallo que sale cara aquí:

- un campo que se renombra en la API (`tuyo.puntos` → `tu_puntos`) y que la vista sigue
  leyendo por el nombre viejo: `undefined ?? '-'` no da error, así que la pantalla se
  pinta entera y solo le faltan los números.
- un campo que cambia de vocabulario (`"goalkeeper"` donde se espera `"POR"`) y deja el
  chip de posición como "?".
- un campo que cambia de tipo según el momento: `weekPoints` viene `null` durante la
  jornada y `int` al cerrarla, y tratarlo como 0 pinta un once entero a cero.

Cómo se usa

    # Antes de tocar nada
    docker compose run --rm --no-deps worker python scripts/caracterizar.py --guardar
    # Después del cambio
    docker compose run --rm --no-deps worker python scripts/caracterizar.py --comparar

El fichero de referencia vive en `scripts/caracterizacion.json` y se versiona: un cambio
de forma a propósito se ve en el diff del commit, que es donde debe verse.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

# Leer y escribir no resuelven a la misma ruta, y es a propósito.
#
# `scripts/` se monta en el contenedor de solo LECTURA (`./scripts:/app/scripts:ro`), para
# que nada de lo que corra dentro pueda reescribir el código que lo lanzó. Así que
# `--guardar` cae a `/data/` y avisa de que hay que copiar el resultado al repositorio.
#
# Comparar usa siempre la del repositorio si existe, que es la versionada y la que se
# revisa. Con dos referencias, la que no se compara envejece en silencio y la comprobación
# sigue dando verde. `COCONUT_CARACT` apunta a otro sitio sin tocar el código.
REPO = pathlib.Path(__file__).with_name("caracterizacion.json")
CAIDA = pathlib.Path("/data/caracterizacion.json")

_forzada = os.environ.get("COCONUT_CARACT")
BASE = pathlib.Path(_forzada) if _forzada else (REPO if REPO.exists() else CAIDA)
DESTINO = (pathlib.Path(_forzada) if _forzada
           else (REPO if os.access(REPO.parent, os.W_OK) else CAIDA))
API = os.environ.get("COCONUT_API_INTERNA", "http://api:8000")

# Rutas que no se fotografían porque su forma no la decide este código. Hoy no descarta
# nada: ninguna de las cuatro aparece en `paths` del OpenAPI. A los que escriben los filtra
# el `"get" not in metodos` de más abajo, no esta tupla.
SALTAR = ("/openapi.json", "/docs", "/redoc", "/app")


def forma(o, prof: int = 0):
    """La estructura de un dato, sin sus valores.

    De una lista se mira solo el primer elemento, y es una decisión consciente: en
    una respuesta de 800 jugadores, comparar los 800 multiplica el ruido sin añadir
    información: todos tienen la misma forma. Lo que sí se guarda es si la lista venía
    vacía, porque "lista vacía" y "lista con cosas" son estados distintos del sistema y
    una que se vacía de golpe es exactamente el síntoma de un filtro roto.
    """
    if prof > 6:
        return "…"
    if o is None:
        return "nulo"
    if isinstance(o, bool):
        return "bool"
    if isinstance(o, int):
        return "int"
    if isinstance(o, float):
        return "float"
    if isinstance(o, str):
        return "texto"
    if isinstance(o, dict):
        # `sorted` por `str(k)`: hay diccionarios con claves ENTERAS (los tipos del
        # tablón, las posiciones) y al pasar por JSON vuelven como texto. Ordenar sin
        # normalizar revienta con "'<' not supported between int and str", y además la
        # foto no sería comparable con la que se guardó.
        return {str(k): forma(v, prof + 1) for k, v in sorted(o.items(), key=lambda kv: str(kv[0]))}
    if isinstance(o, (list, tuple)):
        return ["vacía"] if not o else [forma(o[0], prof + 1)]
    if hasattr(o, "__dict__"):
        return forma(vars(o), prof + 1)
    return type(o).__name__


async def _endpoints() -> dict:
    """Todos los GET sin parámetros, más unos cuantos con parámetro fijo.

    Los de parámetro se incluyen a mano porque son justo los que más lógica tienen (la
    ficha de un equipo, el óptimo de una jornada) y dejarlos fuera sería fotografiar la
    parte fácil.
    """
    import httpx

    rutas: list[str] = []
    async with httpx.AsyncClient(base_url=API, timeout=120) as cli:
        spec = (await cli.get("/openapi.json")).json()
        for ruta, metodos in spec.get("paths", {}).items():
            if "get" not in metodos or "{" in ruta:
                continue
            if any(ruta.startswith(s) for s in SALTAR):
                continue
            rutas.append(ruta)

        # Con parámetro, fijos para que la foto sea comparable entre ejecuciones.
        from fantasy_coconut.config import settings
        rutas += [f"/informe/equipo/{settings.fantasy_team_id}",
                  "/informe/optimo/4", "/informe/clasificacion/4",
                  "/fantasy/calendar/4", "/fantasy/lineup/week/4"]

        out: dict = {}
        for ruta in sorted(set(rutas)):
            try:
                r = await cli.get(ruta)
                out[f"GET {ruta}"] = ({"__status__": r.status_code}
                                      if r.status_code >= 400
                                      else forma(r.json()))
            except Exception as e:
                out[f"GET {ruta}"] = {"__error__": type(e).__name__}
    return out


async def _modelos() -> dict:
    """Lo que deciden los modelos, por debajo de la API.

    Se fotografían aparte porque un refactor puede dejar el endpoint igual y romper el
    modelo que hay detrás: o al revés.
    """
    from fantasy_coconut import salud, trabajos
    from fantasy_coconut.models import (
        alineacion,
        clausulas,
        dificultad,
        emergencia,
        precio,
        saldos,
        trading,
        vigilancia,
    )

    pruebas = {
        "saldos.estimar":           saldos.estimar(),
        "saldos.presupuesto_rival": saldos.presupuesto_rival_maximo(),
        "vigilancia.mi_saldo":      vigilancia.mi_saldo(),
        "vigilancia.exposicion":    vigilancia.mi_exposicion(100_000_000),
        "vigilancia.proximas":      vigilancia.proximas_liberaciones(),
        "vigilancia.objetivos":     vigilancia.objetivos(50_000_000),
        "clausulas.objetivos":      clausulas.objetivos_baratos(50_000_000, 10),
        "clausulas.exposicion":     clausulas.exposicion_propia(100_000_000),
        "dificultad.fuerzas":       dificultad.fuerzas(),
        "alineacion.mejor_once":    alineacion.mejor_once(),
        "emergencia.revisar":       emergencia.revisar_plantilla(),
        "precio.momentum":          precio.momentum(),
        "precio.oportunidades":     precio.oportunidades(),
        "trading.cartera":          trading.cartera(),
        "trading.balance":          trading.balance(),
        "emergencia.debiles":       emergencia.posiciones_debiles(),
        "trabajos.resumen":         trabajos.resumen(),
        "trabajos.leer_config":     trabajos.leer_config(),
        "salud.revisar":            salud.revisar(),
    }
    out = {}
    for nombre, coro in pruebas.items():
        try:
            out[nombre] = forma(await coro)
        except Exception as e:
            # Un error también es una forma: si una función empieza o deja de fallar,
            # eso es exactamente lo que hay que ver.
            out[nombre] = {"__error__": type(e).__name__}
    return out


async def fotografiar() -> dict:
    endpoints, modelos = await _endpoints(), await _modelos()
    return {"endpoints": endpoints, "modelos": modelos}


def _diferencias(base, ahora, ruta: str = "") -> list[tuple[str, str]]:
    """`(gravedad, mensaje)`. Un campo que DESAPARECE es lo grave: quien lo leía sigue
    compilando y empieza a recibir `undefined`, que no da error y da respuestas falsas."""
    fallos: list[tuple[str, str]] = []
    if isinstance(base, dict) and isinstance(ahora, dict):
        for k in sorted(set(base) | set(ahora), key=str):
            sub = f"{ruta}.{k}" if ruta else k
            if k not in ahora:
                fallos.append(("grave", f"{sub}: DESAPARECE (era {json.dumps(base[k], ensure_ascii=False)[:60]})"))
            elif k not in base:
                fallos.append(("nuevo", f"{sub}: campo nuevo"))
            else:
                fallos += _diferencias(base[k], ahora[k], sub)
        return fallos
    if isinstance(base, list) and isinstance(ahora, list):
        if base == ["vacía"] or ahora == ["vacía"]:
            if base != ahora:
                # Puede ser dato del día, no código: se avisa flojo y se dice por qué.
                fallos.append(("flojo", f"{ruta}: la lista pasa de "
                                     f"{'vacía' if base == ['vacía'] else 'con datos'} a "
                                     f"{'vacía' if ahora == ['vacía'] else 'con datos'} "
                                     f"(puede ser el dato del día, no el código)"))
            return fallos
        return _diferencias(base[0], ahora[0], f"{ruta}[]")
    if base != ahora:
        # `nulo` ↔ tipo es lo más común y casi siempre es dato, no código.
        #
        # `int` ↔ `float` también. JSON tiene UN solo tipo numérico, así que un campo
        # llega entero cuando el valor es exacto y decimal cuando no: en una misma
        # respuesta de LaLiga conviven `averagePoints: 2` y `averagePoints: 5.8`, así que
        # marcarlo como cambio grave da una falsa alarma en cada pasada.
        #
        # Y hay una causa concreta que explica la mayoría de los avisos: de una lista
        # solo se mira el primer elemento. Si el primero es atípico (una operación
        # recién hecha que todavía no cruza con la serie de precios, por ejemplo) salta
        # esto aunque los otros 29 estén bien. Por eso el mensaje dice dónde mirar.
        # Un cambio de FORMA (un objeto o una lista que pasa a escalar, o al revés) es lo
        # más grave que sale de aquí, y hay que atajarlo ANTES de comparar tipos: la línea
        # de abajo construye el conjunto `{base, ahora}`, y con un dict dentro eso lanza
        # `TypeError: unhashable type: 'dict'`: la herramienta revienta justo con el
        # cambio que existe para cazar, y sale como traza en vez de como aviso.
        if isinstance(base, dict | list) or isinstance(ahora, dict | list):
            def _forma(x):
                return "objeto" if isinstance(x, dict) else "lista" if isinstance(x, list) else x
            fallos.append(("grave", f"{ruta}: {_forma(base)} → {_forma(ahora)}, cambia la "
                                    f"forma: quien leía dentro se queda sin nada"))
            return fallos

        flojo = "nulo" in (base, ahora) or {base, ahora} == {"int", "float"}
        pista = " (de una lista solo se mira el PRIMER elemento)" if "[]" in ruta else ""
        fallos.append(("flojo" if flojo else "grave", f"{ruta}: {base} → {ahora}{pista}"))
    return fallos


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--guardar", action="store_true", help="Guarda la foto de referencia")
    p.add_argument("--comparar", action="store_true", help="Compara con la referencia")
    args = p.parse_args()

    # `--comparar` tiene que comparar o no hacer nada. No se leía en ninguna parte, y la
    # rama de abajo escribe con `not BASE.exists()`: pedir una comparación sobre un
    # repositorio sin referencia la CREA, y el script contesta "referencia guardada" a
    # quien pregunta si algo ha cambiado.
    if args.comparar and args.guardar:
        p.error("--guardar y --comparar piden cosas contrarias: elige una")
    if args.comparar and not BASE.exists():
        print(f"no hay con qué comparar: falta {BASE}\n"
              f"   Créala primero con --guardar, revisa que la foto sea la buena, y "
              f"vuelve a ejecutar esto.")
        return 2

    foto = await fotografiar()
    n = len(foto["endpoints"]) + len(foto["modelos"])

    if args.guardar or (not BASE.exists() and not args.comparar):
        DESTINO.write_text(
            json.dumps(foto, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        if DESTINO != REPO:
            # Si no se copia, el siguiente `--comparar` mide contra la referencia vieja y
            # enseña como "nuevos" cambios ya aceptados.
            print(f"escrita en {DESTINO} porque scripts/ va montado de solo lectura.\n"
                  f"   Cópiala al repositorio para que sea la que se compara:\n"
                  f"   docker compose cp worker:{DESTINO} scripts/caracterizacion.json")
        print(f"referencia guardada: {n} cosas fotografiadas {DESTINO}")
        return 0

    base = json.loads(BASE.read_text())
    graves, flojos, nuevos = [], [], []
    for grupo in ("endpoints", "modelos"):
        a, b = base.get(grupo, {}), foto.get(grupo, {})
        for k in sorted(set(a) | set(b)):
            if k not in b:
                graves.append(f"{grupo}/{k}: YA NO EXISTE")
                continue
            if k not in a:
                nuevos.append(f"{grupo}/{k}: nuevo")
                continue
            for gravedad, msg in _diferencias(a[k], b[k]):
                destino = {"grave": graves, "flojo": flojos, "nuevo": nuevos}[gravedad]
                destino.append(f"{grupo}/{k} · {msg}")

    print(f"{n} cosas comparadas contra la referencia\n")
    for titulo, lista in (("CAMBIOS DE FORMA", graves),
                          ("posiblemente dato del día", flojos),
                          ("añadidos", nuevos)):
        if lista:
            print(f"-- {titulo} ({len(lista)}) --")
            for x in lista[:40]:
                print(f"  - {x}")
            if len(lista) > 40:
                print(f"  … y {len(lista) - 40} más")
            print()
    if not graves:
        print("ninguna forma ha cambiado" + (" (hay avisos flojos arriba)" if flojos else ""))
    return 1 if graves else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
