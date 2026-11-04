#!/usr/bin/env python3
"""Saca todo el SQL del código y lo compila contra SQLite, listando lo que no pasa.

La sintaxis propia de Postgres (`valor::tipo`, `interval '2 hours'`,
`EXTRACT(EPOCH FROM …)`) hay que reescribirla a mano, y el trabajo está en localizarla:
el SQL va dentro de `text("…")` repartido por decenas de ficheros y a veces construido
por trozos. Esto lo devuelve como una lista con fichero y línea.

Por qué EXPLAIN y no ejecutar

`EXPLAIN <sql>` hace que SQLite compile la consulta (análisis sintáctico, resolución
de tablas, columnas y funciones) pero no la ejecuta. O sea que caza un `::`, una
tabla mal escrita o una función que no existe, sin tocar un solo dato y sin necesidad de
que los parámetros sean realistas.

Lo que no caza: una consulta que compila y devuelve algo distinto que en Postgres.
Para eso está la comparación de resultados; esto es el primer filtro, no el único.

Uso:
    python scripts/auditar_sql_sqlite.py --esquema  # sin datos: lo que corre en CI
    python scripts/auditar_sql_sqlite.py --db RUTA  # contra una base de verdad
    python scripts/auditar_sql_sqlite.py --todo     # lista también lo que pasa
    python scripts/auditar_sql_sqlite.py --ci       # solo falla por lo que no esté en ADREDE
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import sqlite3
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
BASE = pathlib.Path(sys.argv[sys.argv.index("--db") + 1]) if "--db" in sys.argv \
    else pathlib.Path("/destino/coconut.db")

# Con `--esquema` no hace falta ninguna base: se levanta una en memoria desde
# `db/*_sqlite.sql`. Es lo que permite correr esto en CI, donde no hay datos ni
# credenciales: y esta comprobación tiene que correr sola.
#
# El esquema es imprescindible, no un adorno: SQLite resuelve las TABLAS antes que las
# FUNCIONES, así que contra una base vacía `SELECT GREATEST(x,0) FROM managers` se queja
# de que no existe `managers` y el `GREATEST` no llega a verse. Sin las tablas
# delante, el auditor no puede cazar lo único que busca.
SIN_DATOS = "--esquema" in sys.argv

# Ficheros cuyo SQL no tiene por qué compilar contra SQLite, cada uno por un motivo
# distinto. Con `--ci` solo se falla por lo que caiga FUERA de esta lista: si no, el paso
# estaría siempre en rojo y acabaría ignorándose.
#
# La lista es para lo que NO PUEDE compilar, no para tapar fallos. Las cuatro consultas
# contra `politicas_venta` no caen aquí: su causa es que el esquema en memoria no cree
# las tablas que declaran los módulos, y el arreglo es crearlas.
ADREDE = {
    # Análisis sobre la serie histórica; se lanzan a mano contra Postgres y no son
    # parte de la app que corre en el móvil.
    "scripts/efecto_recalculo.py": "análisis, solo Postgres",
    "scripts/medir_traspasos.py": "análisis, solo Postgres",
    # Desplegar un array JSON en FILAS es lo único sin forma neutra: `jsonb_array_elements`
    # frente a `json_each`. Este módulo lleva las dos versiones a propósito.
    "src/fantasy_coconut/models/dificultad.py": "dos versiones, una por motor",
    # En SQLite no hay advisory locks: `locks.py` tiene su propio camino con tabla.
    "src/fantasy_coconut/locks.py": "advisory lock de Postgres, con alternativa",
    # Estos tres no fallan: el auditor no puede reconstruir el SQL que se arma con
    # f-string, así que compila un texto con huecos. Es una limitación de la herramienta,
    # no un fallo del código, y conviene dejarlo escrito: una marca de fallo sin explicar
    # invita a arreglar algo que está bien.
    "src/fantasy_coconut/models/trading.py": "SQL armado con f-string, no reconstruible",
    "src/fantasy_coconut/models/vigilancia.py": "fragmento suelto (la constante ULTIMO)",
    "src/fantasy_coconut/respaldo.py": "SQL armado con f-string, no reconstruible",
}

# Un texto es SQL si empieza por una de estas. Se buscan en las cadenas literales del
# árbol sintáctico, no con una expresión regular sobre el fichero: así se recogen las
# multilínea y no se cuelan los comentarios.
ARRANQUES = ("select ", "insert ", "update ", "delete ", "with ", "create ", "drop ",
             "alter ", "explain ")

# Marca de un hueco de f-string, y las formas con las que se intenta rellenar.
#
# Una consulta construida con f-string es una PLANTILLA, no SQL: `f"SET {campos}"` espera
# una lista de asignaciones y `f"WHERE ts = {ULTIMO}"` una subconsulta. No existe un
# relleno que valga para todos, así que se prueban varios y basta con que UNO compile:
# lo que se comprueba es que el resto de la consulta sea válido, no el hueco.
HUECO = "«‡»"
RELLENOS = ("1", "(SELECT 1)", "col = 1", "'x'", "col")

# Lo que se sabe que hay que reescribir, para poder clasificar el fallo en vez de
# soltar el error crudo de SQLite.
PISTAS = [
    (r"::\s*\w+", "cast `::tipo` CAST(x AS tipo)"),
    (r"\binterval\s+'", "`interval '…'` pasar la fecha de corte como parámetro"),
    (r"\bmake_interval\s*\(", "`make_interval` pasar la fecha de corte como parámetro"),
    (r"EXTRACT\s*\(\s*EPOCH", "`EXTRACT(EPOCH FROM x)` strftime('%s', x)"),
    (r"DISTINCT\s+ON\s*\(", "`DISTINCT ON` row_number() OVER (PARTITION BY …)"),
    (r"\bjsonb_\w+\s*\(", "función `jsonb_*` equivalente `json_*` de SQLite"),
    (r"NULLS\s+NOT\s+DISTINCT", "`NULLS NOT DISTINCT` índice único sobre coalesce()"),
    (r"pg_advisory", "advisory lock de Postgres tabla de bloqueos"),
    (r"\bILIKE\b", "`ILIKE` LIKE (SQLite ya ignora mayúsculas en ASCII)"),
    (r"=\s*ANY\s*\(", "`= ANY (ARRAY[…])` IN (…)"),
    (r"\bstring_agg\s*\(", "`string_agg` group_concat"),
    (r"\bBIGSERIAL\b", "`BIGSERIAL` INTEGER PRIMARY KEY AUTOINCREMENT"),
]


def _sql_del_fichero(f: pathlib.Path) -> list[tuple[int, str]]:
    """Cadenas que parecen SQL, con su número de línea.

    Los f-strings hay que tratarlos aparte. Un `text(f"… {ULTIMO} …")` es un
    `JoinedStr`, no un `Constant`: recorriendo el árbol sin más se recogen los *trozos*
    literales sueltos, que casi nunca son SQL válido. Recogerlos marca como rotas
    consultas que están perfectas, y un falso positivo manda a arreglar lo que ya
    funciona.

    Aquí se reconstruye el f-string sustituyendo cada hueco por un marcador neutro
    (`1`). No es el SQL exacto que se ejecutará, pero sí es analizable, que es lo que
    hace falta para detectar un `::` o una función inexistente.
    """
    try:
        arbol = ast.parse(f.read_text(errors="replace"))
    except SyntaxError:
        return []

    def _texto(nodo) -> str | None:
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
            return nodo.value
        if isinstance(nodo, ast.JoinedStr):
            # El hueco se marca y se rellena luego con varias formas distintas: ver
            # RELLENOS. No hay una sola que encaje en todos los sitios.
            return "".join(str(p.value) if isinstance(p, ast.Constant) else HUECO
                           for p in nodo.values)
        return None

    # Los Constant que viven dentro de un f-string se excluyen: ya van contados en el
    # JoinedStr entero. Sin esto se auditaría dos veces, y la segunda con trozos sueltos.
    dentro_de_fstring = {id(hijo)
                        for n in ast.walk(arbol) if isinstance(n, ast.JoinedStr)
                        for hijo in ast.walk(n) if hijo is not n}

    salida = []
    for nodo in ast.walk(arbol):
        if id(nodo) in dentro_de_fstring:
            continue
        if not isinstance(nodo, (ast.Constant, ast.JoinedStr)):
            continue
        t = (_texto(nodo) or "").strip()
        if len(t) > 25 and t.lower().lstrip("(").startswith(ARRANQUES):
            salida.append((nodo.lineno, t))
    return salida


def normalizar(sql: str) -> str:
    """Deja el SQL en una forma compilable, deshaciendo lo que se resuelve al ejecutar.

    Dos construcciones legítimas del código no son SQL válido tal como se escriben, y
    sin esto salen como fallos:

    - `IN:ids`, lo expande SQLAlchemy (`bindparam(..., expanding=True)`) a tantos
      marcadores como elementos tenga la lista. Antes de expandirse, ningún motor lo
      acepta. Se sustituye por `IN (NULL)`, que compila y es equivalente para comprobar
      la sintaxis del resto.
    - `{RAW}` / `{C}`: marcadores que `.replace()` rellena al importar el módulo,
      según el motor (ver `db.json_param`). Se sustituyen por `NULL`.
    """
    sql = re.sub(r"\bIN\s+:(\w+)", "IN (NULL)", sql, flags=re.I)
    return re.sub(r"\{[A-Z_]+\}", "NULL", sql)


def _compilar(con, sql: str) -> None:
    """Compila la consulta. Si lleva huecos de f-string, basta con que UN relleno pase."""
    candidatos = ([sql.replace(HUECO, r) for r in RELLENOS] if HUECO in sql else [sql])
    ultimo = None
    for c in candidatos:
        params = dict.fromkeys(set(re.findall(r"(?<![:\w]):(\w+)", c)))
        try:
            con.execute("EXPLAIN " + c, params)
            return
        except Exception as e:
            ultimo = e
    raise ultimo


def _pista(sql: str) -> str | None:
    for patron, texto in PISTAS:
        if re.search(patron, sql, re.I):
            return texto
    return None


def _ddl_de_los_modulos(con: sqlite3.Connection) -> None:
    """Crea las tablas que cada módulo declara en su propio `DDL`.

    Se lee `MODULOS_CON_TABLAS` de `db.py`, la misma lista que usa `asegurar_tablas()` al
    arrancar, para que las dos no puedan divergir: un módulo nuevo entra aquí solo, sin
    acordarse de este fichero.

    Un módulo que no se pueda importar no para la auditoría. Lo que se audita es el SQL,
    y quedarse sin auditar nada por una dependencia que falta es peor que auditar con una
    tabla de menos, que sale igualmente como "no such table".
    """
    sys.path.insert(0, str(RAIZ / "src"))
    import importlib

    from fantasy_coconut.db import MODULOS_CON_TABLAS, ddl

    for mod in MODULOS_CON_TABLAS:
        try:
            m = importlib.import_module(f"fantasy_coconut.{mod}")
        except Exception as e:
            print(f"  (no se pudo importar {mod} para su DDL: {type(e).__name__})")
            continue
        bruto = getattr(m, "DDL", None)
        if not bruto:
            continue
        for sentencia in (bruto if isinstance(bruto, (list, tuple)) else [bruto]):
            try:
                con.executescript(ddl(sentencia))
            except sqlite3.Error as e:
                print(f"  (DDL de {mod} no aplicable: {e})")


def _base_en_memoria() -> sqlite3.Connection:
    """Una base vacía con el esquema puesto, para auditar sin datos ni Postgres."""
    con = sqlite3.connect(":memory:")
    ficheros = sorted((RAIZ / "db").glob("*_sqlite.sql"))
    # Sin ficheros no es que el esquema esté mal: es que `db/` no se ve desde aquí. Son dos
    # causas distintas y el mensaje tiene que decir cuál, o se busca el fallo donde no está.
    if not ficheros:
        raise SystemExit(
            f" no hay ningún *_sqlite.sql en {RAIZ / 'db'}.\n"
            f"   Si esto corre en un contenedor, monta el directorio:\n"
            f"   docker compose run --rm --no-deps -v \"$PWD/db:/app/db:ro\" worker …")
    for f in ficheros:
        con.executescript(f.read_text(encoding="utf-8"))

    # Los `.sql` declaran menos de la mitad de las tablas: el resto las crea cada módulo
    # con su `CREATE TABLE IF NOT EXISTS` al arrancar. Sin ellas, sus consultas fallan con
    # "no such table" y la auditoría sale en rojo por tablas que en una instalación de
    # verdad existen. Es el caso de las cuatro de `politicas_venta`.
    _ddl_de_los_modulos(con)

    n = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    if n < 10:
        raise SystemExit(
            f" los {len(ficheros)} fichero(s) de db/ solo crearon {n} tablas; falta esquema")
    print(f"(esquema en memoria: {n} tablas, sin datos)")
    return con


def main() -> None:
    if not SIN_DATOS and not BASE.exists():
        raise SystemExit(f" no encuentro la base SQLite en {BASE}. "
                         f"Créala con scripts/migrar_a_sqlite.py, pasa --db RUTA, "
                         f"o usa --esquema para auditar sin datos.")

    sys.path.insert(0, str(RAIZ / "src"))
    # `DATABASE_URL` apunta a SQLite para que `db.ddl()` traduzca; el módulo solo se
    # importa para eso, no se abre ninguna conexión por aquí.
    os.environ["DATABASE_URL"] = ("sqlite+aiosqlite://" if SIN_DATOS
                                  else f"sqlite+aiosqlite:///{BASE}")
    from fantasy_coconut import sqlite_compat
    from fantasy_coconut.db import ddl as adaptar_ddl

    sqlite_compat.registrar_tipos()
    con = _base_en_memoria() if SIN_DATOS else sqlite3.connect(BASE)
    sqlite_compat.preparar_conexion(con)

    # Los scripts de migración hablan con Postgres a propósito (`pg_tables`,
    # `information_schema`): no tiene sentido auditarlos contra SQLite.
    solo_postgres = {"generar_esquema_sqlite.py", "migrar_a_sqlite.py",
                     "convertir_sql_neutro.py", "auditar_sql_sqlite.py"}
    ficheros = sorted(p for r in ("src", "scripts") for p in (RAIZ / r).rglob("*.py")
                      if p.name not in solo_postgres)
    total = malas = 0
    por_pista: dict[str, list[str]] = {}
    sin_clasificar: list[tuple[str, str, str]] = []
    esperados: list[str] = []

    for f in ficheros:
        rel = f.relative_to(RAIZ)
        for linea, sql in _sql_del_fichero(f):
            total += 1
            sql = normalizar(sql)
            # El DDL de los módulos no se ejecuta tal cual: pasa por `db.ddl()`, que lo
            # adapta al motor. Auditar la cadena cruda marcaría como rotos los
            # `bigserial` y `jsonb` que ese paso ya traduce. Se audita lo que de verdad
            # se va a ejecutar. Y se parte por `;` porque varios módulos meten varias
            # sentencias en una sola constante.
            es_ddl = sql.lower().lstrip("(").startswith(("create ", "drop ", "alter "))
            if es_ddl:
                # Quitar los comentarios antes de partir por `;`: hay DDL con un punto y
                # coma dentro de un `--`, y partir en crudo trocea la sentencia.
                sin_comentarios = re.sub(r"--[^\n]*", "", adaptar_ddl(sql))
                trozos = [x for x in sin_comentarios.split(";") if x.strip()]
            else:
                trozos = [sql]
            # Los :parametros de SQLAlchemy son los mismos que los de sqlite3, así que
            # basta con darles un valor. NULL sirve: solo se va a compilar.
            try:
                for trozo in trozos:
                    _compilar(con, trozo)
            except Exception as e:
                malas += 1
                pista = _pista(sql) or ""
                donde = f"{rel}:{linea}"
                if rel.as_posix() in ADREDE:
                    esperados.append(donde)
                elif pista:
                    por_pista.setdefault(pista, []).append(donde)
                else:
                    sin_clasificar.append((donde, str(e)[:90], sql[:110]))
            else:
                if "--todo" in sys.argv:
                    print(f"{rel}:{linea}")

    print("═══ SQL del código contra SQLite ═══")
    print(f"{total} consultas encontradas · {total - malas} compilan · {malas} NO\n")

    if por_pista:
        print("Fallos con causa conocida, y qué hay que hacer:")
        for pista, sitios in sorted(por_pista.items(), key=lambda x: -len(x[1])):
            print(f"\n {pista}   ({len(sitios)} sitios)")
            for s in sitios[:12]:
                print(f"    {s}")
            if len(sitios) > 12:
                print(f"    … y {len(sitios) - 12} más")

    if sin_clasificar:
        print(f"\nSin clasificar ({len(sin_clasificar)}), hay que mirarlas una a una:")
        for donde, err, sql in sin_clasificar[:25]:
            print(f"\n  {donde}\n    error: {err}\n    sql:   {sql}…")
        if len(sin_clasificar) > 25:
            print(f"\n  … y {len(sin_clasificar) - 25} más")

    if esperados:
        print(f"\nFallan a propósito ({len(esperados)}):")
        for donde in esperados:
            fichero = donde.rsplit(":", 1)[0]
            print(f"  · {donde:<46} {ADREDE.get(fichero, '')}")

    nuevos = sum(len(v) for v in por_pista.values()) + len(sin_clasificar)
    if nuevos:
        print(f"\n {nuevos} consulta(s) que NO deberían fallar contra SQLite.")
        sys.exit(1)
    if malas:
        print(f"\n Todo el SQL compila salvo los {len(esperados)} casos deliberados.")
        # Con `--ci` esto es un éxito: lo que se vigila es que no aparezca uno nuevo.
        sys.exit(0 if "--ci" in sys.argv else 1)
    print("Todo el SQL del código compila contra SQLite.")


if __name__ == "__main__":
    main()
