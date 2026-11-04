"""Arranca la API de Coconut DENTRO del móvil, en `127.0.0.1`.

Un servidor local y no un puente JS Kotlin Python porque la interfaz ya existe y habla
HTTP: las nueve pantallas piden `/estado`, `/informe/hoy`, `/mercado`, y apuntando el
WebView a `localhost` se reutilizan sin tocar una línea. El coste es poco: uvicorn sin
`[standard]` es Python puro y el tráfico no sale del dispositivo.

Kotlin tiene que dejar tres rutas en el entorno ANTES de importar nada de
`fantasy_coconut`, porque `config.py` las lee al importarse y ponerlas después no da error,
solo deja la base en otro sitio:

    DATABASE_URL   sqlite+aiosqlite:////data/user/0/<paquete>/files/coconut.db
    COCONUT_WEB    donde se hayan extraído los ficheros de la interfaz
    TOKEN_CACHE    donde guardar el refresh token de LaLiga

`con_scheduler=True` levanta los trabajos (precios, liga, mercado, alineación) dentro del
proceso de la app. Sin ellos la base se queda vacía y las pantallas en blanco.

Lo arranca `ServicioCoconut`, no la Activity: ahí dentro el scheduler moriría al cerrar la
app. Y un servicio en primer plano es lo que el sistema respeta, no una garantía: algunas
capas siguen siendo agresivas si no se excluye la app de la optimización de batería, y con
el teléfono apagado no corre nada.
"""

from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("coconut.android")

# Puerto fijo y alto, solo en loopback. No se expone a la red: el WebView es el único
# cliente, y abrir esto a la wifi publicaría la API sin autenticación.
PUERTO = 8777
HOST = "127.0.0.1"

_hilo: threading.Thread | None = None
_listo = threading.Event()


def _preparar_entorno(dir_datos: str, dir_web: str | None) -> None:
    """Fija las rutas ANTES de que se importe nada del paquete."""
    # Los nombres son los de los CAMPOS de `Settings`, sin prefijo: pydantic-settings
    # mapea `token_cache` a la variable `TOKEN_CACHE`. Escribirlos como `COCONUT_*` no da
    # error: simplemente se ignoran, y el token acabaría en `/data/token.json`, que en
    # Android no existe.
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{dir_datos}/coconut.db"
    os.environ.setdefault("TOKEN_CACHE", f"{dir_datos}/token.json")
    os.environ.setdefault("STOP_FILE", f"{dir_datos}/STOP")
    # En el móvil el bot no gasta solo: las compras las autoriza una persona tocando.
    os.environ.setdefault("COCONUT_MODO", "agente")
    if dir_web:
        os.environ["COCONUT_WEB"] = dir_web


def _crear_esquema_si_hace_falta(ruta_db: str, dir_esquema: str | None) -> None:
    """Aplica el esquema si la base está vacía. Imprescindible en un móvil nuevo.

    SQLite crea el fichero solo, pero vacío: sin las tablas, la app arranca y revienta en la
    primera consulta, con el error lejos de la causa («no such table: players»).

    Se comprueba por el número de tablas, no por si el fichero existe: un arranque
    interrumpido a medias deja el fichero creado y el esquema a medio poner.
    """
    import sqlite3

    if not dir_esquema:
        log.warning("sin directorio de esquema: si la base está vacía, fallará")
        return

    con = sqlite3.connect(ruta_db)
    try:
        n = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchone()[0]
        if n > 0:
            log.info("base de datos con %d tablas, no se toca", n)
            return

        # `now()` no existe todavía (la registra `sqlite_compat` en cada conexión del
        # pool) pero el esquema la usa en los DEFAULT, así que hay que darla aquí para
        # que las sentencias compilen.
        from fantasy_coconut.sqlite_compat import preparar_conexion
        preparar_conexion(con)

        base = os.path.dirname(ruta_db)
        # Solo los `*_sqlite.sql`. En `db/` conviven los dos esquemas, y el de
        # Postgres no compila aquí (`near "(": syntax error` en el primer `DEFAULT now()`).
        # El mismo filtro que aplica Gradle al empaquetar, para que lo que se prueba en el
        # servidor sea exactamente lo que viaja en el APK.
        ficheros = sorted(n for n in os.listdir(dir_esquema) if n.endswith("_sqlite.sql"))
        if not ficheros:
            raise RuntimeError(
                f"no hay ningún *_sqlite.sql en {dir_esquema}: la base quedaría vacía")
        for nombre in ficheros:
            with open(os.path.join(dir_esquema, nombre), encoding="utf-8") as fh:
                con.executescript(fh.read())
            log.info("esquema aplicado: %s", nombre)
        con.commit()
        n = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchone()[0]
        log.info("base de datos creada con %d tablas en %s", n, base)
    finally:
        con.close()


async def _scheduler_tolerante() -> None:
    """Arranca el scheduler, y si falla no se lleva por delante a la API.

    En un teléfono recién instalado no hay credenciales de LaLiga, así que la primera
    toma de datos va a fallar. Eso no puede impedir que la interfaz se abra: justo ahí es
    donde el usuario tiene que ir a conectar su cuenta. Un fallo de ingesta deja la app
    utilizable y vacía; un fallo que tumba la API la deja inservible.
    """
    try:
        from fantasy_coconut.ingest.scheduler import main as arrancar_jobs
        await arrancar_jobs()
    except Exception:                                                # noqa: BLE001
        log.exception("el scheduler no arrancó; la API sigue en pie")


# Lo que haya matado al hilo del servidor, para poder contarlo: una excepción dentro de
# un hilo se pierde con él, y desde fuera solo se vería «no llegó a escuchar».
_fallo: BaseException | None = None


def _servir(con_scheduler: bool) -> None:
    global _fallo
    import asyncio

    import uvicorn

    from fantasy_coconut.api.main import app

    # `loop="asyncio"` explícito: sin el extra `[standard]` no hay uvloop, y dejar que
    # uvicorn lo busque suelta un aviso confuso en cada arranque.
    config = uvicorn.Config(app, host=HOST, port=PUERTO, loop="asyncio",
                            log_level="info", access_log=False)
    servidor = uvicorn.Server(config)

    async def todo():
        # El scheduler va en el MISMO bucle que la API, no en otro hilo: comparten el pool
        # de SQLAlchemy y la caché del cliente HTTP, y dos bucles serían dos de cada cosa.
        # Con dos clientes, cada uno tiene su cubo y el límite de peticiones no se cumple
        # entre ellos; en el servidor eso lo evita `COCONUT_WORKER_VIA_API`.
        if con_scheduler:
            asyncio.create_task(_scheduler_tolerante())
        _listo.set()
        await servidor.serve()

    try:
        asyncio.run(todo())
    except BaseException as e:                                       # noqa: BLE001
        # Se guarda Y se registra: el log llega a logcat y la excepción al llamante, que
        # es quien puede enseñarla en pantalla.
        _fallo = e
        log.exception("el hilo del servidor murió")
        raise


def arrancar(dir_datos: str, dir_web: str | None = None,
             dir_esquema: str | None = None, con_scheduler: bool = True) -> str:
    """Levanta la API en segundo plano y devuelve la URL para el WebView.

    Es idempotente: si ya está en marcha, devuelve la misma URL sin arrancar otra.
    Llamarla dos veces pasa de verdad: Android recrea la Activity al girar la pantalla.
    """
    global _hilo
    if _hilo is not None and _hilo.is_alive():
        return f"http://{HOST}:{PUERTO}/app/"

    _preparar_entorno(dir_datos, dir_web)
    _crear_esquema_si_hace_falta(f"{dir_datos}/coconut.db", dir_esquema)

    # `daemon=True` para que el hilo no impida cerrar la app. El servidor no tiene nada
    # que guardar al salir: lo que importa ya está en SQLite.
    _hilo = threading.Thread(target=_servir, args=(con_scheduler,),
                             name="coconut-api", daemon=True)
    _hilo.start()

    # Se espera al PUERTO, no a la bandera: `_listo` se marca justo antes de
    # `servidor.serve()`, o sea que dice «voy a arrancar», no «ya escucho». Devolver ahí la
    # URL hace que el WebView cargue antes de tiempo y enseñe un error de conexión.
    _listo.wait(timeout=30)
    # Margen amplio a propósito: el emulador de CI tiene 2 núcleos y GPU por software, y en
    # el primer arranque Chaquopy además extrae y compila los `.py` del paquete entero. Un
    # margen corto convierte «va lento» en «falla», que es un diagnóstico falso.
    if not _esperar_puerto(timeout=120):
        # Los dos casos se distinguen porque piden arreglos opuestos: si el hilo murió, hay
        # una excepción que leer; si sigue vivo, va lento y lo que sobra es margen.
        if _fallo is not None:
            raise RuntimeError(
                f"el servidor falló al arrancar: "
                f"{type(_fallo).__name__}: {_fallo}") from _fallo
        vivo = _hilo is not None and _hilo.is_alive()
        raise RuntimeError(
            f"la API no llegó a escuchar en {HOST}:{PUERTO} en 120 s "
            f"(el hilo {'SIGUE vivo va lento' if vivo else 'MURIÓ sin excepción'}). "
            "Mira el log con la etiqueta 'python.stderr'.")
    log.info("API de Coconut escuchando en http://%s:%d", HOST, PUERTO)
    return f"http://{HOST}:{PUERTO}/app/"


def _esperar_puerto(timeout: float) -> bool:
    """Sondea el puerto hasta que acepte una conexión. Es la única señal fiable."""
    import socket
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if _hilo is not None and not _hilo.is_alive():
            return False                     # el hilo murió: no tiene sentido esperar
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex((HOST, PUERTO)) == 0:
                return True
        time.sleep(0.25)
    return False


def esta_viva() -> bool:
    return _hilo is not None and _hilo.is_alive()
