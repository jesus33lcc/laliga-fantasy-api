"""La conexión a la base de datos. Habla Postgres y SQLite.

Expone el `engine`, la factoría `Session` y los adaptadores de SQL que dependen del
motor (`ddl`, `json_param`). El motor lo elige `DATABASE_URL`, y los dos caminos se
mantienen vivos: SQLite para correr sin servidor, Postgres para un despliegue con
varios procesos.

    DATABASE_URL=postgresql+asyncpg://coconut:…@db:5432/coconut
    DATABASE_URL=sqlite+aiosqlite:////data/coconut.db             # CUATRO barras

Las cuatro barras no son una errata: tres delimitan el esquema y la cuarta es la raíz de
una ruta absoluta. Con tres, la ruta se interpreta como relativa al directorio de trabajo,
así que la base aparece en un sitio distinto según desde dónde se arranque el proceso.
No da ningún error: sale una base vacía.

Lo que SQLite necesita para comportarse como Postgres (la función `now()`, las fechas con
zona horaria, los booleanos, los PRAGMA) vive en `sqlite_compat`. Aquí solo se engancha al
evento `connect`, porque eso se registra por conexión y hay que aplicarlo a cada una
del pool.
"""

from __future__ import annotations

import logging
import pathlib
import re
import sqlite3

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import settings

log = logging.getLogger("coconut.db")

ES_SQLITE = settings.database_url.startswith("sqlite")

if ES_SQLITE:
    from . import sqlite_compat

    sqlite_compat.registrar_tipos()

    # `pool_size` no aplica: para un fichero local, SQLAlchemy usa un pool que no lo
    # acepta. Y `detect_types` es lo que hace que una columna TIMESTAMPTZ vuelva como
    # `datetime` en vez de como texto: sin él, medio código que compara fechas
    # dejaría de funcionar sin dar error.
    engine = create_async_engine(
        settings.database_url,
        connect_args={
            "detect_types": sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            # El bloqueo de escritura se espera, no se falla. Duplica al PRAGMA
            # `busy_timeout` a propósito: este cubre la ventana anterior a que se
            # ejecute ningún PRAGMA.
            "timeout": 10,
        },
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _preparar(dbapi_con, _registro):
        """Cada conexión nueva del pool necesita sus funciones y sus PRAGMA.

        Las funciones de usuario (`now()`…) son por conexión, no por base de
        datos: registrarlas una sola vez al arrancar dejaría al resto del pool sin
        ellas, y el fallo saldría de forma intermitente: solo cuando a una consulta
        le tocara una conexión distinta.
        """
        sqlite_compat.preparar_conexion(dbapi_con)

    log.info("base de datos: SQLite (%s)", settings.database_url.split("///")[-1])
else:
    engine = create_async_engine(settings.database_url, pool_size=5, max_overflow=5)

Session = async_sessionmaker(engine, expire_on_commit=False)


def ddl(sql: str) -> str:
    """Adapta un `CREATE TABLE` de Postgres a SQLite. En Postgres lo devuelve tal cual.

    Varios módulos crean sus tablas al arrancar con `CREATE TABLE IF NOT EXISTS`, y usan
    tipos de Postgres. Traducir aquí (en un punto, sobre un conjunto de construcciones
    pequeño y cerrado) es preferible a mantener dos versiones de cada DDL.

    Lo que cambia y por qué:

    | Postgres | SQLite | |
    |---|---|---|
    | `bigserial PRIMARY KEY` | `INTEGER PRIMARY KEY AUTOINCREMENT` | es la única forma que SQLite autoincrementa |
    | `jsonb` · `json` | `JSONTEXT` | crítico por dos motivos: `jsonb` no es un tipo conocido en SQLite y cae en afinidad NUMÉRICA (guardaría `0` en vez del objeto); y el nombre `JSONTEXT` es el que engancha el conversor que lo devuelve como `dict`, como hace asyncpg |
    | `DEFAULT now()` | `DEFAULT (now())` | SQLite exige paréntesis para llamar a una función en un DEFAULT |

    `timestamptz` se deja como está a propósito: SQLite conserva el nombre declarado
    y es justo lo que hace que `PARSE_DECLTYPES` devuelva un `datetime` en vez de texto.
    Traducirlo a `TEXT` rompería todas las comparaciones de fechas sin dar un error.
    """
    if not ES_SQLITE:
        return sql
    import re as _re
    s = _re.sub(r"\b(big)?serial\b(\s+PRIMARY\s+KEY)", r"INTEGER\2 AUTOINCREMENT",
                sql, flags=_re.I)
    # El orden importa: primero se quitan los casts, después se renombra el tipo.
    # Al revés, `'{}'::jsonb` pasa a `'{}'::JSONTEXT` y el limpiador de abajo
    # (`::\s*TEXT\b`) ya no lo reconoce, porque "TEXT" viene pegado a "JSON" y el `\b` no
    # casa: queda un `::` suelto que SQLite rechaza con "unrecognized token". Solo se
    # manifiesta creando las tablas en una base nueva, porque con las tablas ya creadas
    # el `CREATE TABLE IF NOT EXISTS` no llega a ejecutar la sentencia.
    s = _re.sub(r"::\s*jsonb?\b", "", s, flags=_re.I)
    s = _re.sub(r"\bjsonb?\b", "JSONTEXT", s, flags=_re.I)
    # Dos pasadas: primero se desenvuelve y luego se envuelve, en vez de envolver solo lo
    # que no lo esté ya. Mirar lo que viene detrás con un `(?!\s*\))` para no duplicar
    # paréntesis también rechaza el caso normal: en
    # `actualizado timestamptz NOT NULL DEFAULT now()\n)` lo que sigue es el paréntesis
    # que cierra el CREATE TABLE, así que la última columna de la tabla (justo donde suele
    # ir la fecha) se quedaría sin convertir. Normalizar y envolver no depende del contexto.
    s = _re.sub(r"DEFAULT\s+\(\s*now\(\)\s*\)", "DEFAULT now()", s, flags=_re.I)
    s = _re.sub(r"DEFAULT\s+now\(\)", "DEFAULT (now())", s, flags=_re.I)
    s = _re.sub(r"::\s*TEXT\b", "", s, flags=_re.I)      # por si algún DDL trae ::text
    if _re.search(r"\b(big)?serial\b", s, _re.I):
        raise ValueError(f"DDL con serial que no es PRIMARY KEY, revísalo a mano:\n{sql}")
    return s


def json_param(marcador: str) -> str:
    """Cómo se escribe un parámetro JSON en un INSERT/UPDATE, según el motor.

        text(f"INSERT INTO players (raw) VALUES ({json_param(':raw')})")

    Por qué no puede quedarse `CAST(:raw AS jsonb)` para los dos

    Postgres lo necesita: sin el cast, meter texto en una columna `jsonb` es un error de
    tipos. Pero en SQLite `jsonb` no es un tipo conocido, así que las reglas de
    afinidad lo tratan como NUMÉRICO… y castear un objeto JSON a número da `0`:

        sqlite> SELECT CAST('{"a":1}' AS jsonb);
        0

    No falla: guarda un cero. Con ese cast puesto, la ingesta habría vaciado
    `players.raw` y `league_events.raw` (el payload crudo de la API, que es la red de
    seguridad ante cambios de LaLiga) y ninguna consulta habría dado error: los
    `raw->>'campo'` simplemente empezarían a devolver NULL.

    En SQLite el JSON se guarda como TEXT tal cual, que es lo que esperan `->>`, `->` y
    las funciones `json_*`.
    """
    return marcador if ES_SQLITE else f"CAST({marcador} AS jsonb)"


# ---------- las tablas que no están en `db/*.sql` ----------
#
# `db/001_schema.sql` declara solo una parte de las tablas del sistema: el resto las crea
# cada módulo al arrancar con su propio `CREATE TABLE IF NOT EXISTS`. Funciona porque el
# arranque normal importa todos esos módulos.
#
# Pero no sirve en el momento en que más importa: restaurar una copia en una base
# nueva. Ahí cargar `db/*.sql` crea la mitad del sistema y el resto no existe todavía,
# así que la restauración muere con un "no such table".
#
# Esto lo cierra: un sitio que las crea todas, y que se puede llamar antes de restaurar.
# Los `import` van DENTRO a propósito: varios de esos módulos importan `db`, y a nivel de
# fichero sería un ciclo.
# `locks` no está aquí, y no es un olvido: en Postgres los locks son advisory
# (`pg_try_advisory_lock`, sin tabla) y en SQLite la crea el propio camino de SQLite la
# primera vez. Crearla aquí dejaría una tabla muerta en toda instalación de Postgres.
#
# Un módulo solo entra en la lista si expone su DDL como `DDL`: se busca con
# `getattr(m, "DDL")`, así que uno que la llame `_DDL` devuelve `None` y se ignora sin
# avisar.
MODULOS_CON_TABLAS = (
    "ajustes", "seguimiento", "trabajos", "ventas",
    "api.bloqueo", "api.programar", "api.sesion",
    "models.trading", "ingest.precios_oficiales",
)


def _modulos_con_ddl_sin_declarar() -> list[str]:
    """Módulos que declaran un `DDL` y no están en la lista de arriba.

    Por qué hace falta

    `MODULOS_CON_TABLAS` se mantiene a mano. Un módulo nuevo que declare su `DDL` y no se
    añada aquí no crea su tabla en una instalación nueva, y el fallo no aparece al
    arrancar: aparece el día que alguien usa esa función, con un "no such table" lejos de
    la causa.

    Se mira el árbol sintáctico en vez de importar: importar 80 módulos al arrancar
    ejecutaría su código, y aquí solo hace falta saber si la constante existe.
    """
    import ast
    raiz = pathlib.Path(__file__).parent
    declarados = set(MODULOS_CON_TABLAS)
    sueltos = []
    for f in sorted(raiz.rglob("*.py")):
        try:
            arbol = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for n in arbol.body:                       # solo el nivel de módulo
            objetivos = (n.targets if isinstance(n, ast.Assign)
                         else [n.target] if isinstance(n, ast.AnnAssign) else [])
            if any(isinstance(t, ast.Name) and t.id == "DDL" for t in objetivos):
                nombre = f.relative_to(raiz).with_suffix("").as_posix().replace("/", ".")
                if nombre not in declarados:
                    sueltos.append(nombre)
    return sueltos

_RE_COMENTARIO = re.compile(r"--[^\n]*")


async def asegurar_tablas() -> list[str]:
    """Ejecuta el DDL de los módulos que crean sus propias tablas. Devuelve cuáles."""
    import importlib

    from sqlalchemy import text as _text

    sueltos = _modulos_con_ddl_sin_declarar()
    if sueltos:
        # No se crea a ciegas lo que no está declarado: puede ser un DDL que no toca
        # ejecutar desde aquí. Pero callarlo deja la tabla sin crear y sin rastro de por
        # qué, así que se registra.
        #
        # Aquí no puede salir `locks`: llama a su DDL `_DDL` y la búsqueda pide `DDL`,
        # o sea que ni siquiera llega a la lista.
        log.error(" estos módulos declaran un DDL y NO están en MODULOS_CON_TABLAS, "
                  "así que sus tablas NO se crean en una instalación nueva: %s",
                  ", ".join(sueltos))

    hechos = []
    async with Session() as s:
        for mod in MODULOS_CON_TABLAS:
            m = importlib.import_module(f".{mod}", __package__)
            ddl_mod = getattr(m, "DDL", None)
            if not ddl_mod:
                continue
            # `DDL` es una cadena en ocho módulos y una lista en `trabajos.py`. Se
            # aceptan las dos formas en vez de uniformarlo: unificarlo obligaría a tocar
            # nueve ficheros por una diferencia que aquí no cuesta nada absorber.
            trozos = ddl_mod if isinstance(ddl_mod, (list, tuple)) else [ddl_mod]
            for sentencia in trozos:
                # Los comentarios `--` se quitan antes de partir por `;`: el DDL de
                # `usuarios` lleva un punto y coma DENTRO de un comentario y
                # partir en crudo trocea la sentencia por la mitad. El error que da
                # ("incomplete input") no menciona ni el comentario ni el punto y coma.
                limpio = _RE_COMENTARIO.sub("", str(sentencia))
                for parte in limpio.split(";"):
                    if parte.strip():
                        await s.execute(_text(ddl(parte)))
            hechos.append(mod)
        await s.commit()
    return hechos
