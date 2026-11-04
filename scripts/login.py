"""Login interactivo contra LaLiga (Authorization Code + PKCE). Se hace UNA vez.

El login ocurre en tu navegador, así que vale igual para una cuenta local que para una
federada con Google (donde entra Google y, si toca, tu llave FIDO2). Este script nunca ve
tu contraseña: solo recibe el `code` de vuelta y lo canjea por tokens.

También se puede hacer sin terminal, desde la propia interfaz (Ajustes > Conexión con
LaLiga). Este script es el respaldo y la vía del primer arranque.

Uso:
    docker compose run --rm worker python scripts/login.py

Luego el sistema se renueva solo con el refresh token. Cuando ese caduque (o lo
revoques), se repite este paso.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fantasy_coconut.clients.auth import (
    construir_autorizacion,
    extraer_code,
    intercambiar_code,
)


async def main() -> int:
    # La lógica vive en clients/auth.py, compartida con la API. Aquí solo se orquesta el
    # ida y vuelta por consola.
    url, verifier, _estado = construir_autorizacion(prompt="login")

    print("\n1) Abre esta URL en tu navegador e inicia sesión (Google + tu llave):\n")
    print(url)
    print("\n2) Al terminar, el navegador intentará ir a 'authredirect://...' y dirá que")
    print("   no puede abrirlo. Es NORMAL. Copia esa URL entera de la barra de")
    print("   direcciones (o solo el valor de 'code=').\n")

    code = extraer_code(input("3) Pégala aquí: "))
    if not code:
        print("No he encontrado ningún 'code'. Aborto.")
        return 1

    try:
        d = await intercambiar_code(code, verifier)
    except httpx.HTTPStatusError as e:
        print(f"\nB2C respondió {e.response.status_code}:\n{e.response.text[:500]}")
        print("\nSi pone que el code expiró, repite el proceso: solo dura unos minutos.")
        return 1
    except ValueError as e:
        print(f"\n{e}. Aborto.")
        return 1

    print("\n Listo. Token guardado.")
    if not d.get("refresh_token"):
        print("No vino refresh_token: habrá que repetir el login cuando caduque.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
