#!/usr/bin/env python3
"""Da de alta un usuario de la web. Hay que ejecutarlo antes de encender `COCONUT_AUTH=1`,
o te quedas fuera de tu propia aplicación.

    docker compose run --rm --no-deps worker python scripts/crear_usuario.py

Pide el correo y la contraseña por teclado a propósito: así la contraseña no queda
en el historial del shell ni en los logs de Docker, que es justo lo que pasaría
pasándola como argumento. La contraseña real va a un gestor de contraseñas, no a un fichero.

No hay registro público: este panel mueve dinero de verdad y las cuentas se crean a
mano. El login con Google enlaza con una cuenta ya existente, nunca la crea.
"""

from __future__ import annotations

import asyncio
import getpass
import sys

sys.path.insert(0, "/app/src")

from sqlalchemy import text

from fantasy_coconut.api.sesion import cifrar, crear_tablas
from fantasy_coconut.db import Session

MINIMO = 12


async def main() -> int:
    await crear_tablas()

    email = input("Correo: ").strip().lower()
    if "@" not in email:
        print("eso no parece un correo")
        return 1
    nombre = input("Nombre a mostrar: ").strip() or email.split("@")[0]

    clave = getpass.getpass(f"Contraseña (mínimo {MINIMO}): ")
    if len(clave) < MINIMO:
        print(f"demasiado corta: {MINIMO} caracteres como mínimo")
        return 1
    if clave != getpass.getpass("Repítela: "):
        print("no coinciden")
        return 1

    async with Session() as s:
        existe = (await s.execute(text("SELECT id FROM usuarios WHERE email = :e"),
                                  {"e": email})).first()
        if existe:
            if input(f"{email} ya existe. ¿Cambiar su contraseña? [s/N] ").lower() != "s":
                print("Sin cambios.")
                return 0
            await s.execute(text("UPDATE usuarios SET clave = :c, nombre = :n WHERE id = :i"),
                            {"c": cifrar(clave), "n": nombre, "i": existe[0]})
            # Cambiar la contraseña cierra las sesiones abiertas: si se cambia por
            # sospecha de acceso ajeno, dejarlas vivas no arreglaría nada.
            await s.execute(text("DELETE FROM sesiones WHERE usuario_id = :i"), {"i": existe[0]})
            print("contraseña cambiada; se han cerrado todas sus sesiones")
        else:
            await s.execute(text("""INSERT INTO usuarios (email, nombre, clave)
                                    VALUES (:e, :n, :c)"""),
                            {"e": email, "n": nombre, "c": cifrar(clave)})
            print(f"usuario creado: {email}")
        await s.commit()

    print("\nAhora, para exigir login:  COCONUT_AUTH=1 en el .env")
    print("y  docker compose up -d --force-recreate api   (restart NO relee el .env)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
