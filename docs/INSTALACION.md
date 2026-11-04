# Instalación y uso

Tres formas de tener Coconut, de menos a más trabajo. **Empieza por la primera**: las otras dos solo compensan si sabes por qué las quieres.

```mermaid
flowchart TD
    A{"¿Dónde quieres<br/>que corra?"}
    A -- "en un servidor o un PC<br/>que esté siempre encendido" --> B{"¿Gestionas tus servicios<br/>con Docker Compose?"}
    A -- "solo en el móvil" --> APK["📱 La app de Android"]
    B -- "no" --> UNO["🐳 Un contenedor"]
    B -- "sí" --> COMP["🧩 Docker Compose"]

    classDef pregunta fill:#15151f,stroke:#3d3d52,color:#e8e8f0
    classDef opcion fill:#2a1315,stroke:#FF4B44,stroke-width:2px,color:#ffd9d7
    class A,B pregunta
    class UNO,COMP,APK opcion
```

| | 🐳 [Un contenedor](#-un-contenedor) | 🧩 [Docker Compose](#-docker-compose) | 📱 [App de Android](#-la-app-de-android) |
|---|:---:|:---:|:---:|
| Cómo se instala | una orden | un fichero de Compose | un `.apk` |
| Base de datos | PostgreSQL interno, o la tuya con `DATABASE_URL` | igual | SQLite en el móvil |
| Procesos con todo cerrado | ✅ | ✅ | ✅ mientras el móvil esté encendido |
| Para quién | casi todo el mundo | quien gestiona sus servicios con Compose | probarlo sin servidor |

---

## 🐳 Un contenedor

```bash
docker volume create coconut-datos
docker run -d --name coconut -p 8140:8000 -v coconut-datos:/data ghcr.io/jesus33lcc/laliga-fantasy-api
```

Abre **`http://localhost:8140/app/`**.

Dentro va todo: la API, la interfaz, los procesos programados y un PostgreSQL propio en `/data/postgres`. No hace falta montar nada aparte.

### Qué hace al arrancar

```mermaid
flowchart LR
    S(["docker run"]) --> V{"¿/data es<br/>un volumen?"}
    V -- "no" --> N{"¿Aceptas<br/>perder los datos?"}
    N -- "no" --> X["⛔ No arranca<br/>y dice cómo hacerlo"]
    N -- "sí" --> D
    V -- "sí" --> D{"¿Hay<br/>DATABASE_URL?"}
    D -- "sí" --> E["Usa esa base"]
    D -- "no" --> P["PostgreSQL interno<br/>en /data/postgres"]
    E --> API["API, interfaz<br/>y procesos"]
    P --> API

    classDef paso fill:#15151f,stroke:#3d3d52,color:#e8e8f0
    classDef alto fill:#2a1315,stroke:#FF4B44,stroke-width:2px,color:#ffd9d7
    classDef bien fill:#13261a,stroke:#3fb950,color:#d2f4dc
    class S,V,N,D,E,P paso
    class X alto
    class API bien
```

### ⛔ Por qué se niega a arrancar sin volumen

Si `/data` no es un volumen, el contenedor no arranca y deja este mensaje en el registro:

> [!CAUTION]
> **/data no está montado como volumen. No se arranca.**
>
> Ahí vive la base de datos, y con ella la serie de precios, que no se puede reconstruir hacia atrás: cada día que no se toma se pierde para siempre. Sin volumen desaparece al recrear el contenedor.

La serie de precios se toma **cuatro veces al día**. LaLiga publica su propio histórico, pero con un solo valor diario, así que lo que se pierde no se puede recuperar con la misma resolución. Sin volumen, todo desaparece en el primer `docker rm`.

> [!TIP]
> «Aceptar perder los datos» es arrancar con `-e COCONUT_SIN_VOLUMEN=acepto-perder-los-datos`. Sirve para probar, con datos efímeros.

### Qué funciona sin conectar tu cuenta

Más de lo que parece, porque **el maestro de jugadores de LaLiga es público**:

- La serie de precios empieza a acumularse desde el primer minuto.
- El buscador de jugadores, el momentum y el calendario de dificultad funcionan desde el arranque.

Lo que necesita tu cuenta: **tu** equipo, **tu** mercado, las cláusulas y cualquier operación.

---

## 🧩 Docker Compose

Es la misma imagen que la opción anterior, con su configuración en un fichero: cómodo si ya gestionas tus servicios con Compose. Por defecto solo escucha en `127.0.0.1`.

```bash
git clone https://github.com/jesus33lcc/laliga-fantasy-api.git coconut && cd coconut
docker compose -f docker-compose.publicado.yml up -d
```

### Elegir la base de datos

La imagen trae su propio PostgreSQL, y **`DATABASE_URL` manda sobre él**: si la defines, los datos van a ese servidor y el PostgreSQL interno no arranca.

```bash
DATABASE_URL=postgresql+asyncpg://usuario:clave@servidor:5432/coconut
DATABASE_URL=sqlite+aiosqlite:////data/coconut.db
```

Las tablas las crea la propia API al arrancar, así que vale una base vacía. El volumen `/data` sigue siendo obligatorio aunque la base esté fuera, porque ahí viven el token y el estado.

> [!WARNING]
> **Con SQLite hacen falta cuatro barras** para una ruta absoluta. Con tres, la base se crea relativa al directorio de trabajo y **aparece vacía en otro sitio, sin ningún error**.

El SQL del proyecto es neutro: las mismas consultas corren en los dos motores, y `scripts/comparar_motores.py` lo comprueba ejecutando funciones reales contra ambos y comparando los resultados.

---

## 📱 La app de Android

El `.apk` está en **[Releases](../../releases)**. Al abrirla **arranca Coconut dentro del teléfono sin preguntar nada**. Si prefieres que apunte a tu servidor, se cambia en **Ajustes → Dónde vive tu Coconut**.

| | con servidor | dentro del móvil |
|---|:---:|:---:|
| La interfaz | ✅ | ✅ |
| Tus datos y tu historial | ✅ | ✅ |
| **Procesos a su hora** | ✅ | ✅ con la app **cerrada**, mientras el móvil esté encendido |

### 🔔 La notificación permanente

Con la app cerrada, el motor sigue vivo como **servicio en primer plano**, y Android obliga a anunciarlo con una notificación que no se puede quitar. Es lo que mantiene en marcha las tres tareas que dependen del reloj:

| tarea | margen | si no corre |
|---|---|---|
| Guardar el once antes de que se congele | ±12 min | **la jornada entera a 0** |
| Vigilar clausulazos | 10 min | te enteras tarde de un robo |
| Pujas que se resuelven al cerrar el ciclo del mercado | minutos | pierdes el fichaje |

La notificación dice si el motor está en marcha y trae un botón para **pararlo**.

> [!WARNING]
> **Un servicio en primer plano es lo que Android respeta, no una garantía.** Xiaomi, Samsung y Huawei tienen capas de ahorro agresivas: si ves que se para, excluye Coconut de la optimización de batería. Con el móvil apagado no corre nada; para eso está el servidor.

### Compilarla tú

Hacen falta **JDK 17**, el **SDK de Android** y **Gradle 8 o posterior**. El repositorio no lleva el *wrapper* (`gradlew`) porque es un `.jar` binario; en CI lo instala y lo cachea la acción de Gradle.

```bash
cd android && gradle assembleRelease      # sale en app/build/outputs/apk/release/
gradle assembleRelease -PconEmulador      # añade x86_64, para un emulador
```

Compila **solo para `arm64-v8a`**, que es lo que lleva cualquier móvil de los últimos años. Ocupa unos 32 MB porque el intérprete de Python va dentro; cada arquitectura de más suma unos 14 MB.

Va firmada con la clave de *debug*, porque se distribuye fuera de Play Store y así el repositorio no necesita secretos. A cambio, Android pide permitir «orígenes desconocidos» y **no se puede actualizar encima de un APK firmado con otra clave**.

---

## 🔐 Conectar tu cuenta de LaLiga

**Ajustes → Conexión con LaLiga → Conectar.**

```mermaid
sequenceDiagram
    autonumber
    actor U as Tú
    participant C as Coconut
    participant L as Login de LaLiga
    participant A as API de LaLiga

    U->>C: Conectar
    C->>C: Genera el reto PKCE y guarda el verificador en tu disco
    C-->>U: Abre la página oficial de LaLiga
    U->>L: Entras con Google o con correo y contraseña
    L-->>U: Te redirige a authredirect://… con un código
    U->>C: Pegas esa dirección
    C->>L: Código + verificador
    L-->>C: Tokens
    C->>C: Guarda el refresh token en /data
    C->>A: Lecturas y operaciones con el id_token
    Note over C,A: El token se renueva solo<br/>durante unos 90 días
```

> [!IMPORTANT]
> **Coconut nunca ve tu contraseña, y no podría aunque quisiera.** LaLiga usa Azure AD B2C con PKCE y no admite el inicio de sesión directo con usuario y contraseña, así que un formulario propio no funcionaría. Lo único que se guarda es un *refresh token*, en tu disco.

El copiar y pegar del paso 6 es inevitable en el navegador: LaLiga redirige a `authredirect://com.lfp.laligafantasy`, un enlace de su app móvil. **En la app de Android ese paso desaparece**, porque el WebView sí puede interceptarlo.

---

## ⚙️ Configuración

Casi todo se cambia **desde Ajustes**, sin reiniciar nada, con efecto en 15 segundos. Solo esto va por variables de entorno:

| variable | para qué |
|---|---|
| `DATABASE_URL` | PostgreSQL o SQLite, [ver arriba](#elegir-la-base-de-datos) |
| `COCONUT_MODO` | `agente` por defecto: propone y no gasta solo. `sombra` para probar sin tocar nada |
| `COCONUT_AUTH` | `1` para exigir usuario y contraseña en la propia interfaz |
| `COCONUT_DISPOSITIVO` | Nombre de esta instalación, si tienes varias |
| `COCONUT_STOP` o el fichero `/data/STOP` | **Interruptor de parada:** corta toda petición saliente |

### Avisos por Telegram

Opcional. **Ajustes → Avisos por Telegram** explica cómo crear el bot con @BotFather. Hay interruptor por categoría, umbral en euros y silencio nocturno: un aviso sin acción es ruido, y el ruido hace que se dejen de leer los buenos.

### Copia de seguridad

**Ajustes → Copia de seguridad.** Va a un repositorio **privado tuyo** de GitHub, en un solo commit y solo si algo ha cambiado. Al ir a un repositorio conservas el historial: puedes volver a la copia de anteayer, no solo a la última.

> [!IMPORTANT]
> **Ninguna credencial entra en la copia:** ni el token de LaLiga, ni los de Telegram y GitHub, ni los usuarios de Coconut. Restaurar te pide volver a ponerlos. Es un minuto, y a cambio una copia filtrada no sirve para nada.

---

## 🩺 Si algo va mal

```bash
docker logs -f coconut                       # qué está pasando
curl -s http://localhost:8140/health         # 503 si algún invariante falla
```

| síntoma | qué es |
|---|---|
| `/health` da **503** con «sin refresh token» | Normal en una instalación nueva: falta conectar la cuenta |
| «BLOQUEO por WAF, sistema detenido» | LaLiga devolvió un 403 y el cliente **paró en seco**, a propósito: reintentar convierte un bloqueo temporal en permanente. `POST /salud/reactivar` comprueba primero si el bloqueo sigue y solo entonces reanuda |
| La web se ve con el código viejo | Recarga forzada. Los ficheros llevan huella, pero el `index.html` puede quedar en caché |
| Un 400 de LaLiga con un código como `030.01.49` | Es una **regla del juego**, no una avería. Búscalo en [ERRORES-API.md](ERRORES-API.md) |

---

## 🧪 Cómo se verifica

No hay tests unitarios: la verificación es **empírica y contra la API real**, que es donde aparecen los fallos. Las herramientas, qué caza cada una y cuáles corren en CI están en **[ARQUITECTURA.md](ARQUITECTURA.md#cómo-se-verifica)**.
