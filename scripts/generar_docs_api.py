#!/usr/bin/env python3
"""Genera `docs/API.md` a partir del OpenAPI que la propia API publica.

Por qué generada y no escrita a mano

Una lista de endpoints escrita a mano empieza siendo verdad y acaba siendo una trampa: se
añade una ruta y no se documenta, se borra otra y se queda en el documento describiendo
algo que ya no existe.

Generada del `openapi.json` no puede mentir: si una ruta no está, es que no existe.

    docker compose exec -T api python - < scripts/generar_docs_api.py   # o:
    python3 scripts/generar_docs_api.py http://TU_SERVIDOR:8140 > docs/API.md
    python3 scripts/generar_docs_api.py --local                   # sin levantar el servidor
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

# El orden en que se presentan los grupos. No es alfabético a propósito: se empieza por lo
# que alguien quiere hacer el primer día (mirar su equipo) y se acaba en lo interno.
# Un grupo que no figure aquí se presenta al final, así que una etiqueta nueva aparece en
# el documento sin tocar esta lista: lo que pierde es el sitio que le toca.
ORDEN = ["estado", "informe", "acciones", "bloqueo", "programar", "seguimiento",
         "trabajos", "respaldo", "ajustes", "mercado", "tácticas", "trading",
         "ventas", "histórico", "puente", "login LaLiga", "sesión"]

# Qué es cada grupo que lo necesita, en una línea. Sin esto, su tabla queda sin nada que
# diga para qué sirve.
QUE_ES = {
    # Seis de las quince escrituras exigen `confirmar: true`, no todas. La cabecera del
    # documento las enumera, así que decir aquí «exigen» a secas la contradice.
    "acciones": "Ejecutan operaciones reales. Las seis que mueven dinero exigen "
                "`confirmar: true` y pasan por los límites de gasto.",
    "informe": "Lo que el sistema ha deducido: exposición, ventanas, momentum, saldos "
                "estimados. Es donde vive el análisis.",
    "programar": "Dejar una acción lista para una hora concreta. Sobrevive a reinicios.",
    "seguimiento": "Vigilancias de larga duración: miran hasta que algo pasa, o caduca.",
    "respaldo": "Copia de seguridad a un repositorio privado y quién opera si hay varios "
                "dispositivos.",
    "trabajos": "Los procesos automáticos: qué hacen, cuándo y con qué configuración.",
    "puente": "Paso directo a la API de LaLiga por el cliente compartido, para no abrir "
              "un segundo canal con su propio ritmo de peticiones. **Solo GET** y con "
              "lista blanca: un proxy de escrituras permitiría saltarse los límites.",
    "bloqueo": "El candado del once: impide que nada (ni el modelo ni una venta) rompa "
               "una alineación que has decidido a mano.",
    "histórico": "Consultas sobre la serie de precios que acumula tu propia instalación.",
    "login LaLiga": "Conectar la cuenta por PKCE. LaLiga no expone ROPC, así que no hay "
                    "forma de hacerlo con usuario y contraseña desde aquí.",
    "estado": "La foto operativa y la salud del sistema. Es por donde se empieza.",
    "ajustes": "Qué avisos llegan al móvil, con cuánto ruido y por qué canal. Se aplican "
               "en 15 segundos, sin reiniciar nada.",
    "mercado": "Filtrar el mercado de hoy, y guardar la búsqueda para que avise sola "
               "cuando salga la baraja del día.",
    "tácticas": "Jugadas de mercado medidas, cada una con su muestra al lado. Todas "
                "PROPONEN: ninguna ejecuta.",
    "trading": "Comprar para revender. Incluye el retorno REALIZADO, que es lo que dice "
               "si la señal sigue funcionando.",
    "sesión": "La sesión de Coconut, que no es la de LaLiga. Con `COCONUT_AUTH=0` la VPN "
              "es la única puerta y esto no hace falta.",
    "ventas": "Qué hacer cuando llegue una oferta por un jugador tuyo, decidido ANTES de "
              "listarlo. El modo por defecto cobra solo a partir de un 3 % de prima.",
}


def cargar(base: str) -> dict:
    with urllib.request.urlopen(f"{base.rstrip('/')}/openapi.json", timeout=30) as r:
        return json.load(r)


def cargar_local() -> dict:
    """El OpenAPI sin levantar el servidor: se construye la app y se le pregunta.

    Es lo que permite comprobar en CI que `docs/API.md` sigue al día, así que una ruta
    nueva sin documentar no pasa desapercibida.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
    from fantasy_coconut.api.main import app
    return app.openapi()


def generar(spec: dict) -> str:
    grupos: dict[str, list[tuple[str, str, str]]] = {}
    for ruta, metodos in sorted(spec.get("paths", {}).items()):
        for metodo, definicion in metodos.items():
            if metodo not in ("get", "post", "put", "delete", "patch"):
                continue
            etiquetas = definicion.get("tags") or ["sin clasificar"]
            resumen = (definicion.get("summary")
                       or (definicion.get("description") or "").split("\n")[0]
                       or "")
            grupos.setdefault(etiquetas[0], []).append(
                (metodo.upper(), ruta, resumen.strip()))

    total = sum(len(v) for v in grupos.values())
    orden = [g for g in ORDEN if g in grupos] + sorted(set(grupos) - set(ORDEN))

    out = [
        "# La API de Coconut",
        "",
        "> [!NOTE]",
        "> Generado del `openapi.json` con `scripts/generar_docs_api.py`. **No se edita a "
        "mano**: si una ruta no está aquí, no existe. CI comprueba que esté al día.",
        "",
        f"**{total} operaciones** en {len(grupos)} grupos. La versión interactiva, con el "
        "esquema de cada respuesta y un botón para probarla, está en **`/docs`** de tu "
        "propia instalación.",
        "",
        "```mermaid",
        "---",
        "config:",
        "  themeVariables:",
        "    xyChart:",
        "      plotColorPalette: '#FF4B44'",
        "---",
        "xychart-beta horizontal",
        '    title "Operaciones por grupo"',
        "    x-axis [" + ", ".join(f'"{g}"' for g in orden) + "]",
        f'    y-axis "Operaciones" 0 --> {max(len(v) for v in grupos.values()) + 2}',
        "    bar [" + ", ".join(str(len(grupos[g])) for g in orden) + "]",
        "```",
        "",
        "## Antes de empezar",
        "",
        "- Todo es **relativo a tu servidor**: `http://TU_SERVIDOR:8140`.",
        "- Las lecturas no necesitan nada.",
        "- **Las seis acciones que mueven dinero exigen `confirmar: true`** en el cuerpo, "
        "salvo con `COCONUT_MODO=real`: `pujar`, `ofertar`, `clausulazo`, "
        "`subir-clausula`, `venta-inmediata` y `aceptar-oferta`. Las dos últimas cobran "
        "en vez de gastar, y lo piden igual porque el jugador se va. Las demás escrituras "
        "no lo piden: ni mueven dinero ni son irreversibles.",
        "- Desde 10 M hay que repetir el importe en `importe_confirmado`. Protege del cero "
        "de más.",
        "- **`?dry=true`** devuelve lo que haría la operación sin tocar nada. Lo aceptan "
        "todas las acciones menos `fijar-once` (en POST y en DELETE) y `resolver`, que "
        "no llaman a LaLiga.",
        "- Un rechazo de LaLiga vuelve **con su propio código** (400, 409, 423), no como "
        "un 500: una regla del juego no es una avería. Los códigos de LaLiga están en "
        "[ERRORES-API.md](ERRORES-API.md).",
        "",
        "## Índice",
        "",
        "| grupo | operaciones | para qué |",
        "|---|:---:|---|",
    ]
    out += [f"| [{g}](#{g.lower().replace(' ', '-').replace('(', '').replace(')', '')}) "
            f"| {len(grupos[g])} | {QUE_ES.get(g, '').split('. ')[0].rstrip('.')} |"
            for g in orden]
    out.append("")

    for g in orden:
        out += ["---", "", f"## {g}", ""]
        if g in QUE_ES:
            out += [QUE_ES[g], ""]
        out += ["| | ruta | qué hace |", "|---|---|---|"]
        for metodo, ruta, resumen in sorted(grupos[g], key=lambda x: x[1]):
            out.append(f"| `{metodo}` | `{ruta}` | {resumen or '-'} |")
        out.append("")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    if "--local" in sys.argv:
        spec = cargar_local()
    else:
        args = [a for a in sys.argv[1:] if not a.startswith("--")]
        spec = cargar(args[0] if args else "http://api:8000")
    sys.stdout.write(generar(spec))
