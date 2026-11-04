#!/usr/bin/env python3
"""Empaqueta `web/` en un único HTML con los datos dentro.

Entra la interfaz tal como vive en el repo (un CSS por responsabilidad y módulos
ES) y sale un solo fichero autocontenido, con una foto de los datos reales
incrustada y las imágenes de los jugadores en `data:` URI. Sirve para enseñar la
interfaz donde no se puede cargar CSS externo ni llamar a la API, que está detrás
de una VPN.

Cómo empaqueta el JavaScript

Sin bundler: concatena los módulos en orden de dependencia, borra los `import` y
convierte los `export` en declaraciones normales. Como todo cae en un mismo
ámbito, cada módulo recrea al final su objeto de espacio de nombres (`U`, `V`…)
para que `U.esc(...)` siga funcionando igual que con módulos de verdad. Un
`import` que no sepa resolver aborta el empaquetado en vez de escribir un fichero
roto.

    python scripts/build_web.py [salida.html]

Sin argumento escribe en `web/dist/coconut.html`, y ese es el fichero que
lee `scripts/probar_web.mjs`. Pasarle una salida temporal comprueba que el paquete
se genera, pero deja el bundle del harness sin actualizar: la siguiente pasada de
pruebas mediría código viejo y daría verde.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
WEB = RAIZ / "web"

CSS = ["tokens", "base", "layout", "components", "pitch"]

# Orden de dependencia: cada módulo solo puede usar los de arriba.
# El ORDEN importa: el empaquetador aplana los módulos en un solo ámbito, así que
# cada uno tiene que ir DESPUÉS de los que usa. `bienvenida` usa `ui`, así que va detrás.
MODULOS = [("equipos", "EQ"), ("ui", "U"), ("once", "E"), ("api", "api"),
           ("login", "L"), ("acciones", "A"), ("ajustes", "AJ"), ("buscador", "B"),
           ("bienvenida", "BV"), ("vistas", "V"), ("app", None)]

RE_IMPORT = re.compile(r"^\s*import\s+.*?from\s+['\"]\./(\w+)\.js['\"];?\s*$", re.M)
RE_EXPORT = re.compile(r"^export\s+(?=(?:const|let|var|function|async function|class)\b)", re.M)
RE_NOMBRE = re.compile(
    r"^export\s+(?:async\s+)?(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)", re.M)


RE_TOP = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)", re.M)

# Comentarios y cadenas 'simples'/"dobles". Los template literals se conservan a
# propósito: dentro de `${...}` hay código de verdad, y ahí es donde vive la
# mayoría de las llamadas a `U.esc(...)`.
RE_RUIDO = re.compile(r"/\*.*?\*/|//[^\n]*|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", re.S)


def solo_codigo(src: str) -> str:
    """Quita comentarios y cadenas: si no, una URL como `fantasy-api.llt-...`
    dentro de un texto parece el uso de un espacio de nombres llamado `api`."""
    return RE_RUIDO.sub(" ", src)


def empaquetar_js() -> str:
    partes: list[str] = []
    # Al aplanar, TODO cae en el mismo ámbito. Dos módulos con una declaración del mismo
    # nombre se pisan en silencio, y con `function` además gana la última por
    # hoisting, así que un espacio de nombres acaba apuntando a la función de otro
    # fichero. Servido con módulos de verdad no ocurre: el fallo solo vive en el paquete,
    # y por eso se comprueba aquí.
    vistos: dict[str, str] = {}
    for nombre, espacio in MODULOS:
        ruta = WEB / "js" / f"{nombre}.js"
        src = ruta.read_text(encoding="utf-8")

        importados = {m.group(1) for m in RE_IMPORT.finditer(src)}
        for mod in importados:
            if mod not in {n for n, _ in MODULOS}:
                raise SystemExit(f" {ruta.name} importa '{mod}', que no está en MODULOS")

        # Un espacio de nombres usado SIN su import funciona en el paquete (todo cae
        # junto) pero revienta en la web servida, que sí usa módulos. Solo se vería
        # abriendo el navegador, así que se comprueba aquí.
        codigo = solo_codigo(src)
        for otro, alias in MODULOS:
            if (alias and otro != nombre and otro not in importados
                    and re.search(rf"(?<![\w$.]){re.escape(alias)}\.", codigo)):
                raise SystemExit(
                    f" {ruta.name} usa «{alias}.» pero no importa ./{otro}.js. "
                    f"En el paquete colaría; servido con módulos, no.")
        src = RE_IMPORT.sub("", src)

        exportados = RE_NOMBRE.findall(src)
        if suelto := re.search(r"^export\s+(?!(?:async\s+)?(?:const|let|var|function|class)\b)",
                               src, re.M):
            raise SystemExit(f" {ruta.name}: export que no sé traducir en {suelto.start()}")
        src = RE_EXPORT.sub("", src)

        for declarado in RE_TOP.findall(src):
            if (previo := vistos.get(declarado)) and previo != nombre:
                raise SystemExit(
                    f" colisión de nombre: «{declarado}» se declara en {previo}.js y en "
                    f"{nombre}.js. Al aplanar se pisarían. Renombra uno de los dos.")
            vistos[declarado] = nombre

        partes.append(f"/* ══ {nombre}.js ══ */\n{src}")
        if espacio and exportados:
            partes.append(f"const {espacio} = {{ {', '.join(exportados)} }};\n")

    return "\n".join(partes)


def empaquetar_css() -> str:
    return "\n".join((WEB / "css" / f"{n}.css").read_text(encoding="utf-8") for n in CSS)


def comprobar_sintaxis(js: str) -> None:
    """El JS aplanado tiene que PARSEAR. Si no, se publica una web en blanco.

    El fallo que caza: un acento grave dentro de un template literal lo cierra, así
    que basta con escribir `así` en un comentario metido en un literal para que el resto
    del fichero se lea como código. Revienta con un "Unexpected identifier" que no señala
    ni de lejos al comentario.

    Se comprueba como cuerpo de función, no como script suelto, porque así es como
    se ejecuta el paquete (dentro de su IIFE) y como lo evalúa el harness. Un script
    suelto en modo permisivo trata "<!--" como comentario de línea, o sea que taparía
    justo esta familia de fallos.
    """
    try:
        r = subprocess.run(
            ["node", "-e",
             "const s=require('fs').readFileSync(0,'utf8');"
             "try{new Function(s)}catch(e){console.error(e.message);process.exit(1)}"],
            input=js, text=True, capture_output=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("sin node: no se ha comprobado la sintaxis del paquete")
        return
    if r.returncode:
        raise SystemExit(f" el JS del paquete no parsea: {r.stderr.strip()}\n"
                         "   Sospecha de un acento grave dentro de un template literal.")


def construir(datos: dict | None, salida: Path) -> None:
    # El contenedor del worker monta `src/` y `scripts/`, no `web/`, así que lanzar esto
    # ahí dentro da un FileNotFoundError sobre una ruta que no dice qué hacer. Esto va
    # en el HOST.
    if not (WEB / "index.html").is_file():
        raise SystemExit(
            f"no encuentro {WEB}/index.html, así que no hay interfaz que empaquetar.\n"
            f"Esto se ejecuta en el HOST, desde la raíz del repositorio:\n"
            f"    python3 scripts/build_web.py\n"
            f"Dentro del contenedor del worker no vale: no monta `web/`.")
    html = (WEB / "index.html").read_text(encoding="utf-8")

    # El artefacto aporta su propio <!doctype>, <head> y <body>: se envía solo el interior.
    cuerpo = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    cabeza = re.search(r"<head>(.*)</head>", html, re.S).group(1)
    titulo = re.search(r"<title>(.*?)</title>", cabeza, re.S).group(1)
    fuentes = "\n".join(re.findall(r'<link rel="(?:preconnect|stylesheet)" href="https://[^>]+>', cabeza))
    cuerpo = re.sub(r'<script type="module"[^>]*></script>', "", cuerpo)

    trozos = [f"<title>{titulo}</title>", fuentes, f"<style>\n{empaquetar_css()}\n</style>", cuerpo]
    if datos is not None:
        trozos.append(f"<script>window.DATOS_DEMO = {json.dumps(datos, ensure_ascii=False)};</script>")
    js = empaquetar_js()
    comprobar_sintaxis(js)
    trozos.append(f"<script>\n(() => {{\n{js}\n}})();\n</script>")

    salida.write_text("\n".join(trozos), encoding="utf-8")
    kb = salida.stat().st_size / 1024
    print(f"{salida} · {kb:.0f} KB" + (f" · {len(datos)} claves de datos" if datos else " · sin datos"))


if __name__ == "__main__":
    destino = Path(sys.argv[1]) if len(sys.argv) > 1 else WEB / "dist" / "coconut.html"
    destino.parent.mkdir(parents=True, exist_ok=True)
    # La foto de datos se busca en el mismo sitio dé donde dé la salida, y en este orden:
    # `COCONUT_DATOS_DEMO` si apunta a un fichero, luego `web/dist/datos_demo.json` y
    # luego `web/demo/datos.json`.
    # `web/dist/` está en `.gitignore` porque la maqueta de una instalación real lleva
    # plantillas, saldos y el chat de Telegram dentro. La versión ANONIMIZADA vive en
    # `web/demo/`, que sí se versiona: es la que permite abrir el paquete sin instalar
    # nada y la que la CI necesita para ejecutar el harness.
    fichero_datos = Path(os.environ.get("COCONUT_DATOS_DEMO") or "")
    if not fichero_datos.is_file():
        for candidato in (WEB / "dist" / "datos_demo.json", WEB / "demo" / "datos.json"):
            if candidato.is_file():
                fichero_datos = candidato
                break
    # `is_file()` y no `exists()`: sin ninguno de los candidatos, `fichero_datos` se queda
    # con el `Path("")` de arriba, que es `Path(".")`. Un directorio existe, así que con
    # `exists()` la rama `None` no se alcanza nunca y el paquete muere con
    # IsADirectoryError en vez de construirse sin datos de maqueta, que es lo previsto.
    construir(json.loads(fichero_datos.read_text()) if fichero_datos.is_file() else None,
              destino)
