"""La configuración. Funciona con pydantic 2 y con pydantic 1.

Por qué las dos versiones

En el servidor corre pydantic 2. Pero en el móvil no puede: pydantic 2 delega en
`pydantic-core`, que está escrito en Rust y no existe para Android: no está en el
repositorio de Chaquopy y no hay forma de compilarlo. FastAPI 0.115 funciona igual de bien
con pydantic 1.10, que sí es Python puro: valida, devuelve 422 y genera el OpenAPI.
Por eso el móvil usa esa.

Este fichero es el único que nota la diferencia, porque `BaseSettings` cambió de paquete
entre las dos versiones. El resto del código usa `BaseModel` y `Field`, que son iguales.
"""

from __future__ import annotations

try:                                                  # pydantic 2 (servidor)
    from pydantic_settings import BaseSettings, SettingsConfigDict
    PYDANTIC_V2 = True
except ImportError:                                   # pydantic 1 (Android)
    from pydantic import BaseSettings  # type: ignore[no-redef]
    SettingsConfigDict = None                         # type: ignore[assignment]
    PYDANTIC_V2 = False


def a_dict(modelo, **kw) -> dict:
    """Serializa un modelo, con el método que exista en cada versión de pydantic.

    `model_dump()` es de pydantic 2 y no existe en el 1. `.dict()` existe en las dos,
    pero en el 2 está obsoleto y suelta un aviso por cada llamada. Con esto no hay que
    elegir entre romper el móvil o llenar el log del servidor.
    """
    return (modelo.model_dump(**kw) if PYDANTIC_V2 else modelo.dict(**kw))


class Settings(BaseSettings):
    # Las dos formas de decir lo mismo. Pydantic 1 lee `class Config` e ignora
    # `model_config`; pydantic 2 al revés. Declarar solo la que toca evita el aviso de
    # obsolescencia que saldría si se dejaran las dos puestas a la vez.
    if PYDANTIC_V2:
        model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    else:
        class Config:
            env_file = ".env"
            extra = "ignore"

    # --- API ---
    fantasy_base_url: str = "https://fantasy-api.llt-services.com"
    competition_id: int = 1  # 1 = LaLiga

    # --- credenciales: aquí no hay ninguna, y es a propósito ---
    # Nada de usuario y contraseña: el tenant de Azure B2C de LaLiga no expone ROPC, y
    # con una cuenta federada con Google tampoco funcionaría. El login es interactivo
    # (PKCE, `scripts/login.py`), así que pedir una contraseña sería recoger un secreto
    # que no sirve para nada.

    # --- identidad en la liga (se autodescubre al loguear) ---
    fantasy_league_id: str = ""
    fantasy_team_id: str = ""

    # --- infraestructura ---
    database_url: str = "postgresql+asyncpg://coconut:${DB_PASSWORD}@db:5432/coconut"
    redis_url: str = "redis://redis:6379/0"
    token_cache: str = "/data/token.json"
    stop_file: str = "/data/STOP"

    # --- politeness: la mitad de lo que se autoimpone el cliente oficial ---
    rate_limit_rps: float = 0.5
    rate_limit_burst: int = 5
    max_reintentos: int = 4
    user_agent: str = "fantasy-coconut/0.1 (uso privado)"

    # --- alertas ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


settings = Settings()
