"""Sube la copia de seguridad a un repositorio PRIVADO de GitHub.

Publica cada copia como un commit, así que queda historial: el fallo típico de una
copia no es perder el fichero, es guardar encima una ya corrupta y no tener a dónde
volver. El formato que produce `respaldo.py` (texto ordenado por clave) hace además que
el diff de cada copia marque solo lo que cambió, en vez de un blob entero por día.

Cliente HTTP PROPIO, y no es un descuido

Aquí no se usa `ClienteEducado`: ese cliente habla con LaLiga y para en seco ante un
403, porque allí un 403 puede ser el WAF y reintentar convierte un bloqueo temporal en
permanente. GitHub también responde 403 (cuota, token sin permisos), así que compartir
cliente haría que un token de GitHub mal puesto dejase el sistema entero sin leer LaLiga.
Son dos servicios distintos y no comparten frenos ni contadores.

Un solo commit por copia

Se usa la Git Data API (blob, árbol, commit, ref) en vez de la de contenidos, que solo
escribe un fichero por llamada y dejaría un commit por fichero. Además permite subir
solo los ficheros que han cambiado: el resto se hereda del árbol anterior con
`base_tree`.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("coconut.respaldo.git")

API = "https://api.github.com"
CABECERAS = {"Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}


class ErrorGitHub(RuntimeError):
    """Fallo de GitHub, con su código y su mensaje. Se distingue de un fallo propio."""

    def __init__(self, status: int, detalle: str):
        self.status, self.detalle = status, detalle
        super().__init__(f"GitHub {status}: {detalle}")


@dataclass(frozen=True)
class Destino:
    repo: str                      # "usuario/repositorio"
    token: str
    rama: str = "main"

    @property
    def valido(self) -> bool:
        return bool(self.token and "/" in (self.repo or ""))


async def _pedir(cli: httpx.AsyncClient, metodo: str, ruta: str, **kw):
    r = await cli.request(metodo, f"{API}{ruta}", **kw)
    if r.status_code >= 400:
        # El cuerpo de GitHub dice de verdad qué pasa ("Bad credentials", "Resource not
        # accessible by personal access token"), y `str(e)` de httpx no lo trae.
        try:
            detalle = r.json().get("message", r.text[:200])
        except ValueError:
            detalle = r.text[:200]
        raise ErrorGitHub(r.status_code, detalle)
    return r.json() if r.content else {}


async def comprobar(d: Destino) -> dict:
    """¿El token sirve y el repo existe? Se llama antes de guardar nada.

    Enterarse de que el token no vale el día que hace falta restaurar es exactamente lo
    que esto evita: el mismo criterio que `POST /ajustes/probar` con Telegram.
    """
    if not d.valido:
        raise ErrorGitHub(400, "faltan el repositorio (usuario/nombre) o el token")
    async with httpx.AsyncClient(timeout=30, headers={
            **CABECERAS, "Authorization": f"Bearer {d.token}"}) as cli:
        info = await _pedir(cli, "GET", f"/repos/{d.repo}")
        if not info.get("private"):
            # No se bloquea, pero se dice: la copia lleva ajustes, decisiones y la serie
            # de precios. En un repositorio público eso es la estrategia entera a la
            # vista de los rivales.
            log.warning("el repositorio %s es PÚBLICO", d.repo)
        return {"repo": info.get("full_name"), "privado": bool(info.get("private")),
                "permiso_escritura": bool((info.get("permissions") or {}).get("push"))}


async def subir(d: Destino, ficheros: dict[str, str], mensaje: str) -> dict:
    """Sube la copia como UN commit. Devuelve qué se subió y qué se heredó."""
    if not d.valido:
        raise ErrorGitHub(400, "faltan el repositorio o el token")

    async with httpx.AsyncClient(timeout=120, headers={
            **CABECERAS, "Authorization": f"Bearer {d.token}"}) as cli:

        # 1· Dónde está la rama.
        #
        # Un repositorio sin commits responde 409 "Git Repository is empty", no
        # 404. Los dos son el mismo caso (la primera copia) pero solo el 404 es obvio, y
        # un repositorio recién creado (la situación de cualquiera que instale esto)
        # devuelve el 409. Contemplar solo el 404 hace fallar la primera copia siempre.
        SIN_COMMITS = (404, 409)
        padre, base_tree = None, None
        try:
            ref = await _pedir(cli, "GET", f"/repos/{d.repo}/git/ref/heads/{d.rama}")
            padre = ref["object"]["sha"]
            commit = await _pedir(cli, "GET", f"/repos/{d.repo}/git/commits/{padre}")
            base_tree = commit["tree"]["sha"]
        except ErrorGitHub as e:
            if e.status not in SIN_COMMITS:
                raise
            log.info("el repositorio no tiene la rama %s todavía: primera copia", d.rama)
            # Y en un repositorio SIN NINGÚN COMMIT no se puede ni crear un blob: la
            # API de objetos de git contesta 409 "Git Repository is empty" también ahí.
            # Hay que sembrarlo antes con la API de contenidos, que sí sabe crear el
            # primer commit. La alternativa (pedir que se cree un README a mano) mete un
            # paso manual justo al configurar la copia por primera vez.
            padre, base_tree = await _sembrar(cli, d)

        # 2· Qué ficheros han cambiado de verdad. Heredar el resto del árbol anterior es
        #    lo que hace que una copia diaria cueste kilobytes: sin esto se subirían los
        #    30 MB de la serie de precios cada día aunque no se hubiera movido una fila.
        previos: dict[str, str] = {}
        if base_tree:
            arbol = await _pedir(
                cli, "GET", f"/repos/{d.repo}/git/trees/{base_tree}?recursive=1")
            previos = {n["path"]: n["sha"] for n in arbol.get("tree", [])
                       if n["type"] == "blob"}

        cambios, iguales = [], 0
        for ruta, contenido in sorted(ficheros.items()):
            sha_local = _sha_blob(contenido)
            if previos.get(ruta) == sha_local:
                iguales += 1
                continue
            blob = await _pedir(cli, "POST", f"/repos/{d.repo}/git/blobs", json={
                "content": base64.b64encode(contenido.encode()).decode(),
                "encoding": "base64"})
            cambios.append({"path": ruta, "mode": "100644", "type": "blob",
                            "sha": blob["sha"]})

        if not cambios:
            log.info("respaldo: nada que subir, %d ficheros iguales", iguales)
            return {"subido": False, "motivo": "sin cambios", "iguales": iguales}

        # 3· Árbol, commit y mover la rama. Tres llamadas, un commit.
        cuerpo_arbol: dict = {"tree": cambios}
        if base_tree:
            cuerpo_arbol["base_tree"] = base_tree
        arbol_nuevo = await _pedir(cli, "POST", f"/repos/{d.repo}/git/trees",
                                   json=cuerpo_arbol)
        commit_nuevo = await _pedir(cli, "POST", f"/repos/{d.repo}/git/commits", json={
            "message": mensaje, "tree": arbol_nuevo["sha"],
            "parents": [padre] if padre else []})

        if padre:
            await _pedir(cli, "PATCH", f"/repos/{d.repo}/git/refs/heads/{d.rama}",
                         json={"sha": commit_nuevo["sha"]})
        else:
            await _pedir(cli, "POST", f"/repos/{d.repo}/git/refs",
                         json={"ref": f"refs/heads/{d.rama}", "sha": commit_nuevo["sha"]})

        log.warning("respaldo subido: %d fichero(s), commit %s",
                    len(cambios), commit_nuevo["sha"][:8])
        return {"subido": True, "commit": commit_nuevo["sha"][:8],
                "ficheros": [c["path"] for c in cambios], "iguales": iguales}


SEMILLA = """# Copia de seguridad de LaLiga Fantasy Coconut

Este repositorio lo escribe **Coconut** solo. No hace falta tocarlo a mano.

## Qué hay aquí

- `datos/*.jsonl`: una línea por fila, ordenadas por su clave. Están así para que cada
  copia sea, para git, unas pocas líneas nuevas: el historial sale casi gratis.
- `meta.json`: qué se copió y cuántas filas.

## Qué NO hay aquí, a propósito

- **El token de LaLiga.** Es la llave de la cuenta y opera sin contraseña ni 2FA.
- Los tokens de Telegram y GitHub y la clave de las cuotas.
- Usuarios y sesiones de Coconut.
- El maestro de jugadores y la temporada anterior: se vuelven a descargar solos.

Al restaurar hay que volver a conectar la cuenta de LaLiga y pegar los tokens. Es un
minuto, y a cambio una copia filtrada no sirve para nada.

## Restaurar

En Coconut **Ajustes Copia de seguridad Restaurar**. Por defecto **solo añade** lo
que falte; pisar lo que ya hay hay que pedirlo expresamente.
"""


async def leer_fichero(d: Destino, ruta: str) -> str | None:
    """Un solo fichero del repositorio, o `None` si no está.

    Va por la API de contenidos y no por la de objetos: **una petición** en vez de tres
    (referencia, commit, árbol). El candado de dispositivo se lee con frecuencia, y esa
    diferencia es lo que lo hace asumible.
    """
    if not d.valido:
        return None
    async with httpx.AsyncClient(timeout=30, headers={
            **CABECERAS, "Authorization": f"Bearer {d.token}"}) as cli:
        try:
            r = await _pedir(cli, "GET",
                             f"/repos/{d.repo}/contents/{ruta}?ref={d.rama}")
        except ErrorGitHub as e:
            # 404 = no existe · 409 = el repositorio aún no tiene commits. Los dos
            # significan «todavía no hay nada», que no es un error.
            if e.status in (404, 409):
                return None
            raise
        return base64.b64decode(r["content"]).decode()


async def escribir_fichero(d: Destino, ruta: str, contenido: str, mensaje: str) -> dict:
    """Crea o actualiza un fichero suelto. Un commit, una o dos peticiones.

    Para ACTUALIZAR hay que mandar el `sha` del fichero que se sustituye; sin él
    GitHub responde 422. Y ese `sha` es además el control de concurrencia: si otro
    dispositivo lo cambió mientras tanto, la escritura falla en vez de pisarlo: que es
    justo lo que se quiere de un candado.
    """
    if not d.valido:
        raise ErrorGitHub(400, "faltan el repositorio o el token")
    async with httpx.AsyncClient(timeout=30, headers={
            **CABECERAS, "Authorization": f"Bearer {d.token}"}) as cli:
        sha = None
        try:
            actual = await _pedir(cli, "GET",
                                  f"/repos/{d.repo}/contents/{ruta}?ref={d.rama}")
            sha = actual.get("sha")
        except ErrorGitHub as e:
            if e.status not in (404, 409):
                raise
        cuerpo = {"message": mensaje, "branch": d.rama,
                  "content": base64.b64encode(contenido.encode()).decode()}
        if sha:
            cuerpo["sha"] = sha
        return await _pedir(cli, "PUT", f"/repos/{d.repo}/contents/{ruta}", json=cuerpo)


async def _sembrar(cli: httpx.AsyncClient, d: Destino) -> tuple[str, str]:
    """Crea el primer commit de un repositorio vacío. Devuelve `(commit, árbol)`.

    El commit sale de **la respuesta del PUT**, no de releer la referencia. Preguntar
    por la rama justo después de crearla vuelve a dar «Git Repository is empty», porque
    GitHub tarda un momento en publicarla. La respuesta ya trae el commit y el árbol, así
    que releer es una llamada de más que además contesta peor.
    """
    r = await _pedir(cli, "PUT", f"/repos/{d.repo}/contents/README.md", json={
        "message": "Copia de seguridad de Coconut",
        "content": base64.b64encode(SEMILLA.encode()).decode(),
        "branch": d.rama})
    commit = r.get("commit") or {}
    sha, arbol = commit.get("sha"), (commit.get("tree") or {}).get("sha")
    if not sha or not arbol:
        raise ErrorGitHub(500, "GitHub no devolvió el commit al sembrar el repositorio")
    log.info("repositorio %s sembrado con su primer commit", d.repo)
    return sha, arbol


async def descargar(d: Destino) -> dict[str, str]:
    """Trae la última copia del repositorio: el lado de la restauración."""
    if not d.valido:
        raise ErrorGitHub(400, "faltan el repositorio o el token")
    async with httpx.AsyncClient(timeout=120, headers={
            **CABECERAS, "Authorization": f"Bearer {d.token}"}) as cli:
        ref = await _pedir(cli, "GET", f"/repos/{d.repo}/git/ref/heads/{d.rama}")
        commit = await _pedir(cli, "GET",
                              f"/repos/{d.repo}/git/commits/{ref['object']['sha']}")
        arbol = await _pedir(
            cli, "GET", f"/repos/{d.repo}/git/trees/{commit['tree']['sha']}?recursive=1")
        out: dict[str, str] = {}
        for n in arbol.get("tree", []):
            if n["type"] != "blob":
                continue
            blob = await _pedir(cli, "GET", f"/repos/{d.repo}/git/blobs/{n['sha']}")
            out[n["path"]] = base64.b64decode(blob["content"]).decode()
        return out


def _sha_blob(contenido: str) -> str:
    """El SHA-1 que git le daría a este contenido.

    Es lo que permite preguntar «¿ha cambiado?» **sin subir nada**: git calcula el hash
    de un blob sobre `blob <bytes>\\0<contenido>`, así que se puede reproducir aquí y
    comparar con el árbol remoto. Sin esto habría que subir cada fichero para descubrir
    que era idéntico: 30 MB diarios para no cambiar nada.
    """
    import hashlib
    datos = contenido.encode()
    return hashlib.sha1(b"blob " + str(len(datos)).encode() + b"\0" + datos).hexdigest()
