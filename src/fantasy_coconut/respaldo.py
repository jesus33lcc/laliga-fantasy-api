"""Copia de seguridad de lo que no se puede recuperar de ningún otro sitio.

Qué problema resuelve, y cuál no

No es sincronización. Sincronizar es mantener dos copias vivas de acuerdo, con mezcla y
conflictos; esto es copia y restauración, que es otro problema y mucho más fiable.

Y el riesgo que cubre no es "se me rompe el disco" (para eso está el volcado periódico de
un servidor bien montado) sino el caso del que instala Coconut y no tiene nada de eso:
su configuración, sus tareas y su serie de precios viven en un único fichero SQLite dentro
de un contenedor o de un teléfono.

Lo que nunca sale de aquí, y por qué es una lista blanca

`usuarios`, `sesiones` y el fichero del refresh token quedan fuera a propósito:

- El refresh token es la llave de la cuenta de LaLiga. Con él se opera el equipo sin
  contraseña y sin 2FA. Una copia que lo lleve convierte "se me ha filtrado un backup"
  en "me han robado la cuenta". Restaurar sin él pide volver a loguearse: 30 segundos.
- `usuarios` guarda hashes de contraseña y `sesiones` tokens de sesión vivos. Lo mismo.

Por eso el catálogo es una lista blanca, no una lista negra. Con lista negra, cualquier
tabla nueva con datos sensibles entraría en la copia el día que alguien la cree, y nadie se
enteraría. Con lista blanca, lo peor que puede pasar es que algo no se salve: que se nota
al restaurar, en vez de descubrirse cuando ya es tarde.

Por qué texto plano ordenado y no un volcado binario

El destino es un repositorio git, y ahí un `.db.gz` de 30 MB es un blob nuevo entero cada
día: un año son gigas y ni un solo diff legible.

Cada tabla se escribe como JSON por líneas ordenada por su clave, y eso es lo que hace
el diff legible: una fila que no cambia ocupa siempre la misma posición, así que git solo
marca lo que de verdad se movió.

No todas son de solo añadir. `ajustes`, `jobs_config`, `tareas`, `seguimientos` y
`managers` se modifican, y ahí el diff son líneas cambiadas, no añadidas al final. El
orden estable ayuda igual, pero no convierte la copia en un registro incremental.

Y tiene un segundo efecto que se aprovecha: dos exportaciones de los mismos datos son
idénticas byte a byte, así que "¿ha cambiado algo?" es comparar un hash, sin leer nada.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from sqlalchemy import text

from .db import ES_SQLITE, Session, json_param

log = logging.getLogger("coconut.respaldo")

FORMATO = 1


@dataclass(frozen=True)
class Tabla:
    nombre: str
    orden: str                 # columnas que la ordenan, para que la copia sea estable
    clave: tuple[str, ...]     # por dónde deduplicar al restaurar
    json: tuple[str, ...] = () # columnas que llevan JSON y hay que tratar aparte
    grande: bool = False       # entra solo si se pide la copia completa


# ---------- el catálogo. añadir aquí es lo único que mete una tabla en la copia. ----------
#
# Lo que no está, y que se ha decidido no salvar:
#   · usuarios, sesiones ......... credenciales (ver arriba)
#   · players, teams ............. maestro PÚBLICO de LaLiga, se reingiere solo
#   · player_points .............. va dentro de `players.raw`, que sale del maestro
#   · market_values_historico .... la 24/25, se descarga del archivo público (MIT)
#   · jobs_log, jobs_peticiones .. ruido de operación, no dice nada de mañana
#   · probable_lineups ........... se pisa en cada ingesta; la serie está en el histórico
#   · probable_lineups_vieja ..... resto de una migración, ya no se escribe
#   · backfill_dias .............. marcador de progreso, se rehace solo
#   · advanced_stats, match_odds . vacías: las fuentes nunca se llegaron a conectar
#   · pending_actions ............ propuestas del piloto de botones, que ya no existe
NUCLEO: tuple[Tabla, ...] = (
    Tabla("ajustes",      "clave",              ("clave",),   ("valor",)),
    Tabla("jobs_config",  "nombre",             ("nombre",),  ("config",)),
    Tabla("tareas",       "id",                 ("id",),      ("params",)),
    Tabla("seguimientos", "id",                 ("id",),      ("params", "historial")),
    Tabla("decisions",    "id",                 ("id",),      ("payload",)),
    Tabla("alert_log",    "clave",              ("clave",)),
    Tabla("predictions",  "id",                 ("id",),      ("features",)),
    Tabla("managers",     "id",                 ("id",)),
    Tabla("bloqueos",     "recurso",            ("recurso",), ("datos",)),
    Tabla("trading_ops",  "id",                 ("id",)),
    # Configuración del usuario: qué hacer con cada oferta que entra por un jugador
    # listado. No la reconstruye nadie, y restaurar sin ella deja las ventas en el modo
    # por defecto sin avisar.
    Tabla("politicas_venta", "player_team_id",  ("player_team_id",)),
)

SERIE: tuple[Tabla, ...] = (
    # La serie propia de precios. LaLiga publica su histórico, pero con un valor al día;
    # estas son 4 tomas diarias, y el 58 % de los jugadores cambia de precio más de una
    # vez al día, así que la resolución no se recupera de ninguna parte.
    Tabla("market_values",  "player_id, ts", ("player_id", "ts"), grande=True),
    Tabla("squad_snapshots", "ts, manager_id, player_id",
          ("ts", "manager_id", "player_id"), grande=True),
    Tabla("league_events",  "id",            ("id",), ("raw",), grande=True),
    Tabla("probable_lineups_historico", "player_id, jornada, source, ts",
          ("player_id", "jornada", "source", "ts"), grande=True),
    # `fecha`, no `dia`: con el nombre viejo el SELECT falla y la tabla no entra en la
    # copia. Al ser la última de la lista, nadie lo nota.
    Tabla("market_values_oficial", "player_id, fecha", ("player_id", "fecha"), grande=True),
    # La baraja de cada día, que LaLiga tampoco publica hacia atrás. De aquí salen los 13
    # agentes libres diarios, que no se repiten, y el sesgo de precios y posiciones con el
    # que se decide a qué caladero ir. Son 18 filas al día: los 13 libres y las fichas que
    # listan los rivales.
    Tabla("market_offers", "market_id", ("market_id",), grande=True),
)

TODAS = NUCLEO + SERIE


def _catalogo(completa: bool) -> tuple[Tabla, ...]:
    return TODAS if completa else NUCLEO


# ---------- secretos que viven dentro de `ajustes` y no pueden salir ----------
#
# Los ajustes guardan tokens, y la copia va a un repositorio: privado, pero un
# repositorio. Todo lo que entre aquí sale del sistema.
#
# Una lista blanca a nivel de TABLA no protege de un secreto guardado dentro de una
# fila. `usuarios` y `sesiones` quedan fuera por llevar credenciales, pero `ajustes` es
# una tabla inofensiva con credenciales dentro de un JSON de configuración.
#
# Con el token de un bot se puede mandar mensajes en su nombre y leer lo que recibe; con
# la clave de the-odds-api se gasta la cuota de otro; y el token de GitHub es justo el que
# da acceso a esta copia. Los tres se van, y restaurar los pide otra vez: es un minuto en
# Ajustes a cambio de que un backup filtrado no valga nada.
SECRETOS = ("telegram_token", "odds_api_key", "github_token", "clave", "token",
            "password", "secret")


def _sin_secretos(valor):
    """Quita de un ajuste cualquier campo que huela a credencial, a cualquier nivel.

    Recursivo a propósito: los ajustes son JSON libre y un secreto nuevo puede aparecer
    anidado. La comparación es por subcadena, en cualquier posición del nombre, así que
    `telegram_token`, `token_bot` y `github_token` caen los tres sin enumerarlos.

    Con `clave` y `token` en la lista, el filtro es deliberadamente amplio: puede
    llevarse un ajuste que solo contenga esas palabras en su nombre sin ser un secreto.
    Se prefiere perder un ajuste, que se vuelve a poner en un minuto, a publicar una
    credencial en la copia.
    """
    if isinstance(valor, dict):
        return {k: _sin_secretos(v) for k, v in valor.items()
                if not any(s in k.lower() for s in SECRETOS)}
    if isinstance(valor, list):
        return [_sin_secretos(v) for v in valor]
    return valor


def _linea(fila: dict, t: Tabla) -> str:
    """Una fila como JSON de una sola línea, con las claves ordenadas.

    `sort_keys` no es cosmético: sin él, dos exportaciones de los mismos datos salen
    distintas según cómo el driver devuelva el diccionario, y entonces ni el hash sirve
    para saber si algo cambió ni git puede hacer un diff pequeño.
    """
    d = {}
    for k, v in fila.items():
        if k in t.json and isinstance(v, str):
            # Guardarlo como objeto y no como cadena escapada: ocupa menos, se lee, y al
            # restaurar da igual el motor (Postgres quiere jsonb, SQLite texto).
            try:
                v = json.loads(v)
            except (ValueError, TypeError):
                pass
        elif hasattr(v, "isoformat"):
            v = v.isoformat()
        if t.nombre == "ajustes" and k == "valor":
            v = _sin_secretos(v)
        d[k] = v
    return json.dumps(d, ensure_ascii=False, sort_keys=True, default=str)


async def exportar(completa: bool = False) -> dict[str, str]:
    """Devuelve `{ruta relativa: contenido}`. No escribe nada: quien llama decide dónde.

    Separarlo así es lo que permite que el mismo código sirva para el fichero que te
    descargas y para el que se sube a git, sin dos implementaciones que puedan divergir.
    """
    salida: dict[str, str] = {}
    resumen: dict[str, int] = {}
    async with Session() as s:
        for t in _catalogo(completa):
            # Cada tabla en su propio SAVEPOINT. Sin él, en Postgres un SELECT fallido
            # aborta la transacción entera y todas las tablas de detrás caen con "current
            # transaction is aborted": una sola tabla rota se lleva media copia, y el log
            # lo cuenta como si fueran fallos independientes. Es lo que hacía
            # `market_values_oficial` con su columna mal escrita.
            try:
                async with s.begin_nested():
                    filas = (await s.execute(
                        text(f"SELECT * FROM {t.nombre} ORDER BY {t.orden}"))).mappings().all()
            except Exception as e:
                log.warning("no se pudo exportar %s: %s", t.nombre, e)
                continue
            salida[f"datos/{t.nombre}.jsonl"] = "".join(
                _linea(dict(f), t) + "\n" for f in filas)
            resumen[t.nombre] = len(filas)

    salida["meta.json"] = json.dumps({
        "formato": FORMATO,
        "completa": completa,
        "filas": resumen,
        # Sin fecha a propósito: si llevara marca de tiempo, toda copia sería distinta
        # aunque no hubiera cambiado nada, y se perdería el "¿hace falta subir?" de un
        # vistazo. La fecha la pone el commit, que es donde vive la historia.
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return salida


def huella(ficheros: dict[str, str]) -> str:
    """Un hash de la copia entera. Dos copias iguales dan el mismo, así que sirve para
    no subir nada cuando no ha cambiado nada."""
    h = hashlib.sha256()
    for ruta in sorted(ficheros):
        h.update(ruta.encode())
        h.update(ficheros[ruta].encode())
    return h.hexdigest()[:16]


async def _restaurar_ajustes(s, ficheros: dict[str, str], reemplazar: bool) -> None:
    """`ajustes` se mezcla, no se pisa. Es la única tabla que lo necesita.

    Y sin esto, restaurar sería destructivo justo donde duele: la copia sale sin los
    secretos (token de Telegram, clave de las cuotas, token de GitHub), así que un
    UPSERT normal escribiría el ajuste completo sin ellos y te dejaría sin bot sin
    decir nada. Lo que falta en la copia tiene que quedarse como está.

    Dicho de otra forma: una copia incompleta a propósito obliga a una restauración que
    sepa que lo es. Si no, la redacción que protege el backup rompe la restauración.
    """
    crudo = ficheros.get("datos/ajustes.jsonl")
    if not crudo:
        return
    for linea in crudo.splitlines():
        if not linea.strip():
            continue
        fila = json.loads(linea)
        clave, nuevo = fila["clave"], fila.get("valor")
        actual = (await s.execute(
            text("SELECT valor FROM ajustes WHERE clave = :k"), {"k": clave})).scalar()
        if actual and isinstance(nuevo, dict):
            viejo = json.loads(actual) if isinstance(actual, str) else actual
            if isinstance(viejo, dict):
                nuevo = {**viejo, **nuevo} if reemplazar else {**nuevo, **viejo}
        elif actual and not reemplazar:
            continue
        # `actualizado` se arrastra de la copia en vez de dejar que lo ponga el
        # `DEFAULT now()`: si no, una restauración pone la fecha de hoy en todos los
        # ajustes y se pierde cuándo se cambió cada uno.
        await s.execute(text(f"""
            INSERT INTO ajustes (clave, valor, actualizado)
            VALUES (:k, {json_param(':v')}, :t)
            ON CONFLICT (clave) DO UPDATE
               SET valor = {json_param(':v')}, actualizado = :t"""),
            {"k": clave, "v": json.dumps(nuevo, ensure_ascii=False),
             "t": fila.get("actualizado")})


async def importar(ficheros: dict[str, str], reemplazar: bool = False) -> dict[str, int]:
    """Mete la copia en la base de datos. Devuelve cuántas filas entró por tabla.

    Por defecto no borra nada: inserta con `ON CONFLICT DO NOTHING`, así que sobre una
    base con datos solo puede añadir. Es deliberado: el momento de restaurar es el peor
    para descubrir que has sustituido algo bueno por algo viejo.

    `reemplazar=True` sí pisa las filas que coincidan en clave. Nunca se aplica solo: lo
    pide quien restaura.

    `ajustes` se mezcla campo a campo en los dos modos, pero `reemplazar` sí decide quién
    gana en la mezcla y si una fila ya existente se salta. Va por `_restaurar_ajustes()`. Tiene que ser
    así porque la copia sale sin los secretos: un `DO NOTHING` dejaría la configuración
    vieja y un UPSERT entero te borraría el token del bot sin decir nada.
    """
    metido: dict[str, int] = {}
    async with Session() as s:
        await _restaurar_ajustes(s, ficheros, reemplazar)
        for t in TODAS:
            if t.nombre == "ajustes":
                metido["ajustes"] = len(
                    [x for x in ficheros.get("datos/ajustes.jsonl", "").splitlines() if x.strip()])
                continue
            crudo = ficheros.get(f"datos/{t.nombre}.jsonl")
            if not crudo:
                continue
            filas = [json.loads(linea) for linea in crudo.splitlines() if linea.strip()]
            if not filas:
                metido[t.nombre] = 0
                continue
            cols = list(filas[0])
            marcadores = ", ".join(
                json_param(f":{c}") if c in t.json else f":{c}" for c in cols)
            conflicto = ", ".join(t.clave)
            if reemplazar:
                sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in t.clave)
                cola = f"DO UPDATE SET {sets}" if sets else "DO NOTHING"
            else:
                cola = "DO NOTHING"
            sql = text(f"INSERT INTO {t.nombre} ({', '.join(cols)}) "
                       f"VALUES ({marcadores}) ON CONFLICT ({conflicto}) {cola}")
            n = 0
            for lote in (filas[i:i + 500] for i in range(0, len(filas), 500)):
                # Las columnas JSON vuelven a texto: es lo que espera el parámetro, y
                # `json_param` se encarga de la diferencia entre los dos motores.
                params = [{c: (json.dumps(f.get(c), ensure_ascii=False)
                               if c in t.json and not isinstance(f.get(c), (str, type(None)))
                               else f.get(c)) for c in cols} for f in lote]
                await s.execute(sql, params)
                n += len(lote)
            metido[t.nombre] = n
        await _recolocar_secuencias(s, [t for t in TODAS if metido.get(t.nombre)])
        await s.commit()
    return metido


async def _recolocar_secuencias(s, tablas: list) -> None:
    """Pone cada secuencia por encima del `id` más alto que acaba de entrar.

    Las filas de la copia traen su `id` explícito y en Postgres eso no mueve la secuencia,
    así que el siguiente INSERT normal pide un id ocupado y falla con `duplicate key`. La
    restauración parece haber ido bien y la base queda sin poder escribir. Afecta a
    `decisions`, `predictions`, `league_events` y `trading_ops`. En SQLite no pasa.
    """
    if ES_SQLITE:
        return
    for t in tablas:
        # `pg_get_serial_sequence` devuelve NULL cuando esa columna no tiene secuencia,
        # así que hace de filtro: las tablas con clave natural se saltan solas.
        await s.execute(text(f"""
            SELECT setval(seq, GREATEST(COALESCE((SELECT max(id) FROM {t.nombre}), 0), 1))
            FROM pg_get_serial_sequence('{t.nombre}', 'id') AS seq
            WHERE seq IS NOT NULL"""))
