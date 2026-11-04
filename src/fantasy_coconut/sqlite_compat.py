"""Hace que SQLite se comporte como Postgres en lo que este código da por hecho.

Registra en cada conexión las funciones de usuario que SQLite no trae (`now()` la
primera), los adaptadores y conversores de fecha, JSON y booleanos, y los PRAGMA. Con
eso, el SQL escrito contra Postgres corre en SQLite sin reescribirlo: el grueso de las
diferencias son llamadas a función, y una función de usuario las intercepta todas.

Versión mínima

SQLite 3.38, que es cuando aparecen los operadores `->` y `->>` sobre JSON. El resto
de lo que usa el código es anterior: `ON CONFLICT … DO UPDATE` (3.24), funciones de
ventana (3.25), `FILTER (WHERE …)` (3.30) y `RETURNING` (3.35).

Lo que hay que reescribir a mano

Lo que es *sintaxis* y no una llamada a función, porque una función de usuario no puede
interceptarlo:

- `valor::tipo` -> `CAST(valor AS tipo)`
- `now() - interval '2 hours'` -> pasar la fecha de corte como parámetro desde Python
- `EXTRACT(EPOCH FROM x)` -> `strftime('%s', x)`
- `DISTINCT ON (a)` -> `row_number() OVER (PARTITION BY a ORDER BY …)`
- `UNIQUE NULLS NOT DISTINCT` -> índice único sobre `coalesce(col, centinela)`

La trampa de las fechas: léela antes de tocar nada

Con `PARSE_DECLTYPES`, `SELECT ts FROM t` devuelve un `datetime` con zona horaria, igual
que asyncpg. Pero un agregado pierde el tipo declarado: `SELECT max(ts)` devuelve
texto, no `datetime`.

No rompe las comparaciones (se guarda ISO-8601 en UTC, así que el orden alfabético *es*
el cronológico), pero sí rompe a cualquiera que haga `fila.ts.strftime(...)` o reste dos
fechas. Cuando un agregado devuelva algo que Python vaya a usar como fecha, hay que
anotarlo:

    SELECT max(ts) AS "ultimo [TIMESTAMPTZ]" FROM squad_snapshots
                          ^^^^^^^^^^^^^^^^^ esto dispara el conversor

`fecha()` de este módulo acepta las dos formas y es la vía segura para leer una fecha
venga como venga.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3

log = logging.getLogger("coconut.sqlite")

UTC = dt.UTC

# Nombres de tipo que disparan el conversor a `datetime`. El primero es el que emite
# el generador de esquema; los demás están por si una tabla vieja los trae.
TIPOS_FECHA = ("TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP", "DATETIME")


# ---------- adaptadores: python -> sqlite ----------

def _adaptar_datetime(d: dt.datetime) -> str:
    """Siempre ISO-8601 en UTC y con microsegundos.

    Las tres cosas importan y ninguna es cosmética:
    - UTC: si se mezclaran zonas, dos instantes iguales darían textos distintos.
    - Formato fijo: el orden alfabético solo coincide con el cronológico si todas
      las filas tienen la misma anchura. Un `2026-9-5` entre `2026-09-04` y
      `2026-09-06` se ordenaría mal y sin dar error.
    - Microsegundos siempre: `timespec="microseconds"` los escribe aunque sean 0,
      para que no haya filas de dos anchuras distintas.
    """
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC).isoformat(sep=" ", timespec="microseconds")


def _adaptar_date(d: dt.date) -> str:
    return d.isoformat()


# ---------- conversores: sqlite -> python ----------

def _convertir_datetime(b: bytes | None) -> dt.datetime | None:
    if not b:
        return None
    return _texto_a_fecha(b.decode())


def _convertir_date(b: bytes | None) -> dt.date | None:
    if not b:
        return None
    return dt.date.fromisoformat(b.decode()[:10])


def _convertir_json(b: bytes | None):
    """Una columna JSON tiene que volver como `dict`/`list`, igual que con asyncpg.

    Sin esto la diferencia es silenciosa y se cuela en todas partes. Postgres
    devuelve un `dict` de una columna `jsonb`; SQLite devolvería el texto. Todo el
    código que hace `fila.raw["marketValue"]` o `{config}` deja de funcionar, y no
    con un error claro sino con un `TypeError: 'str' object is not a mapping` lejos de
    la causa.

    El truco está en el nombre del tipo declarado: `JSONTEXT` contiene "TEXT", así
    que SQLite le da afinidad de texto y guarda el JSON tal cual (y `->>` sigue
    funcionando sobre él), pero es un nombre distinto de `TEXT`, que es lo que permite
    engancharle este conversor y no a las columnas de texto normales.
    """
    if b is None or b == b"":
        return None
    try:
        return json.loads(b.decode())
    except (ValueError, UnicodeDecodeError):
        # Un JSON corrupto se devuelve como texto en vez de reventar: es preferible
        # que el llamante vea el valor raro a que se caiga la consulta entera.
        log.warning("columna JSON con contenido no válido: %.80s", b)
        return b.decode(errors="replace")


def _convertir_bool(b: bytes | None) -> bool | None:
    """SQLite guarda 0/1; asyncpg devuelve `True`/`False` de verdad.

    Sin esto, un `if fila.is_me is True` deja de funcionar sin dar error.
    """
    if b is None or b == b"":
        return None
    return b not in (b"0", b"false", b"FALSE", b"f")


def _texto_a_fecha(t: str) -> dt.datetime:
    """Lee una fecha venga como venga, y siempre la devuelve con zona horaria.

    Devolver una fecha *naive* sería la peor salida posible: compararla con una que sí
    tiene zona lanza `TypeError`, pero restarlas no: da un resultado desplazado en
    silencio.
    """
    t = t.strip()
    try:
        d = dt.datetime.fromisoformat(t)
    except ValueError:
        # Último recurso: solo fecha, o con 'Z' al final (Python <3.11 no la acepta).
        d = dt.datetime.fromisoformat(t.replace("Z", "+00:00")[:26])
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def fecha(valor) -> dt.datetime | None:
    """Lee como fecha lo que devuelva la BD, sea `datetime` (columna) o texto (agregado).

    Es la vía segura: `fecha(fila.ultimo)` funciona con Postgres y con SQLite, y con
    un `max(ts)` sin anotar. Ver la trampa de las fechas arriba.
    """
    if valor is None or isinstance(valor, dt.datetime):
        return valor if valor is None or valor.tzinfo else valor.replace(tzinfo=UTC)
    if isinstance(valor, dt.date):
        return dt.datetime.combine(valor, dt.time.min, tzinfo=UTC)
    if isinstance(valor, (bytes, bytearray)):
        valor = valor.decode()
    if isinstance(valor, str):
        return _texto_a_fecha(valor)
    raise TypeError(f"no sé leer {valor!r} ({type(valor).__name__}) como fecha")


def registrar_tipos() -> None:
    """Adaptadores y conversores. Es global al módulo `sqlite3`, así que se hace una vez.

    Python 3.12 marcó como obsoletos los adaptadores de fecha por defecto, no la
    posibilidad de registrar los tuyos. Registrarlos aquí es justo lo que quita el
    aviso, además de fijar el formato que necesitamos.
    """
    sqlite3.register_adapter(dt.datetime, _adaptar_datetime)
    sqlite3.register_adapter(dt.date, _adaptar_date)
    for nombre in TIPOS_FECHA:
        sqlite3.register_converter(nombre, _convertir_datetime)
    sqlite3.register_converter("DATE", _convertir_date)
    sqlite3.register_converter("BOOLEAN", _convertir_bool)
    sqlite3.register_converter("JSONTEXT", _convertir_json)


# ---------- funciones de usuario: lo que el sql llama y sqlite no tiene ----------

def _now() -> str:
    return _adaptar_datetime(dt.datetime.now(UTC))


_UNIDADES = {
    "second": "%Y-%m-%d %H:%M:%S", "minute": "%Y-%m-%d %H:%M:00",
    "hour": "%Y-%m-%d %H:00:00", "day": "%Y-%m-%d 00:00:00",
    "month": "%Y-%m-01 00:00:00", "year": "%Y-01-01 00:00:00",
}


def _date_trunc(unidad: str, valor) -> str | None:
    """`date_trunc('day', ts)` de Postgres."""
    if valor is None:
        return None
    fmt = _UNIDADES.get(str(unidad).lower())
    if fmt is None:
        raise ValueError(f"date_trunc: unidad no soportada: {unidad!r}")
    return _adaptar_datetime(_texto_a_fecha(fecha(valor).strftime(fmt)))


def _greatest(*vals):
    v = [x for x in vals if x is not None]
    return max(v) if v else None


def _least(*vals):
    v = [x for x in vals if x is not None]
    return min(v) if v else None


def _jsonb_array_length(v):
    """`jsonb_array_length` de Postgres. SQLite lo llama `json_array_length`.

    Registrarlo con el nombre de Postgres evita tocar el SQL que ya lo usa. Devuelve
    NULL (no 0) si el valor no es una lista: un 0 diría "array vacío", que es una
    respuesta distinta y se propagaría como un dato falso.
    """
    if v is None:
        return None
    try:
        datos = json.loads(v) if isinstance(v, (str, bytes)) else v
    except (ValueError, TypeError):
        return None
    return len(datos) if isinstance(datos, list) else None


_TIPOS_JSON = {list: "array", dict: "object", str: "string",
               bool: "boolean", type(None): "null"}


def _jsonb_typeof(v):
    """`jsonb_typeof` de Postgres, con los mismos nombres de tipo que él."""
    if v is None:
        return None
    try:
        datos = json.loads(v) if isinstance(v, (str, bytes)) else v
    except (ValueError, TypeError):
        return None
    if isinstance(datos, bool):          # antes que int: en Python bool ES int
        return "boolean"
    if isinstance(datos, (int, float)):
        return "number"
    return _TIPOS_JSON.get(type(datos))


def _jsonb_build_object(*pares):
    if len(pares) % 2:
        raise ValueError("jsonb_build_object necesita un número par de argumentos")
    return json.dumps({str(pares[i]): pares[i + 1] for i in range(0, len(pares), 2)})


def preparar_conexion(con: sqlite3.Connection) -> None:
    """Ajusta una conexión recién abierta. Se llama en el evento `connect` del engine.

    Los PRAGMA no son opcionales y cada uno evita un fallo concreto:

    - `journal_mode=WAL`: `api` y `worker` son procesos distintos sobre el mismo
      fichero. Sin WAL, un lector bloquea al escritor y salta `database is locked`.
      Es persistente (se guarda en el fichero), pero se pone en cada conexión porque
      cuesta nada y una base recreada a mano no lo traería.
    - `busy_timeout`: con WAL sigue habiendo un instante de escritura exclusiva.
      Sin timeout, coincidir con él es un error inmediato en vez de una espera de
      milisegundos.
    - `foreign_keys=ON`: SQLite las ignora por omisión, y el esquema tiene
      `REFERENCES` que Postgres sí aplica. Sin esto se pueden colar precios huérfanos,
      que es uno de los invariantes de `salud.py`.
    - `synchronous=NORMAL`: con WAL es seguro ante caída del proceso (solo se
      arriesga a la caída del sistema entero) y evita un `fsync` por transacción, que
      en la tarjeta SD de una Pi es la diferencia entre usable y no usable.
    """
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=NORMAL")

    con.create_function("now", 0, _now)
    con.create_function("date_trunc", 2, _date_trunc, deterministic=True)
    for n in (2, 3, 4):
        con.create_function("greatest", n, _greatest, deterministic=True)
        con.create_function("least", n, _least, deterministic=True)
    con.create_function("jsonb_build_object", -1, _jsonb_build_object, deterministic=True)
    # Con el nombre de Postgres, para no tener que tocar el SQL que ya los usa.
    # `jsonb_array_elements` no se puede registrar así: devuelve FILAS, y una función
    # de usuario de SQLite solo puede devolver un valor. Ese caso se resuelve con dos
    # versiones de la consulta (ver `models/dificultad.py`).
    con.create_function("jsonb_array_length", 1, _jsonb_array_length, deterministic=True)
    con.create_function("jsonb_typeof", 1, _jsonb_typeof, deterministic=True)
