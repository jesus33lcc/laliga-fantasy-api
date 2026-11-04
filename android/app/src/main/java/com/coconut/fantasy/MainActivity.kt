package com.coconut.fantasy

import android.annotation.SuppressLint
import android.content.Context
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.util.Log
import android.view.Gravity
import android.view.ViewGroup
import android.widget.ProgressBar
import android.widget.TextView
import android.webkit.CookieManager
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.LinearLayout
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import kotlin.concurrent.thread

/**
 * Envoltorio nativo de la interfaz web de Coconut.
 *
 * Con una URL de servidor guardada en ajustes, envuelve esa web. Sin ella arranca Coconut
 * dentro del teléfono y apunta a `127.0.0.1`, que es el modo al que va el proyecto.
 *
 * Lo que aporta frente al navegador: intercepta `authredirect://com.lfp.laligafantasy`, el
 * `redirect_uri` del login de LaLiga. Ningún navegador sabe abrir ese esquema, así que en
 * web hay que copiar la URL de vuelta y pegarla a mano.
 *
 * El motor NO se levanta aquí: lo levanta `ServicioCoconut`. Dentro de la Activity los
 * trabajos morirían al cerrar la app, y sin guardar la alineación antes de que se congele
 * la jornada puntúa 0. Esta clase solo espera a que el motor esté listo y apunta el WebView.
 *
 * Es un WebView y no una app nativa porque las nueve pantallas ya existen y son responsive.
 * La TWA (el APK "oficial" de una PWA) queda descartada mientras Coconut se sirva por HTTP:
 * exige HTTPS y verificación de dominio con `assetlinks.json`.
 */
class MainActivity : ComponentActivity() {

    private lateinit var web: WebView
    private lateinit var refresco: SwipeRefreshLayout

    /** La URL del servidor la pone cada usuario: aquí nadie comparte el mismo host. */
    private val prefs by lazy { getSharedPreferences("coconut", Context.MODE_PRIVATE) }
    private var base: String
        get() = prefs.getString("base", "") ?: ""
        set(v) = prefs.edit().putString("base", v.trimEnd('/')).apply()

    /** La URL del Coconut que se está usando: el servidor remoto, o el del teléfono.
     *
     * Nunca se concatena `base` a pelo. Con `base` vacío, que es lo normal en modo
     * autónomo, `base + "/app/#ajustes"` da una ruta sin esquema y el WebView la resuelve
     * como `file://`, con `net::ERR_ACCESS_DENIED`: el login se perdería en una página en
     * blanco justo al volver de LaLiga con el `code`. */
    private var urlLocal: String = ""
    private val raiz: String
        get() = if (base.isNotEmpty()) base else urlLocal

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        refresco = SwipeRefreshLayout(this)
        web = WebView(this)
        refresco.addView(web, ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(refresco)

        configurarWeb()

        // Atrás navega dentro de la web antes de cerrar la app. Sin esto, el botón físico
        // sale de la aplicación desde cualquier pantalla.
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else finish()
            }
        })

        refresco.setOnRefreshListener { web.reload() }

        // Con servidor configurado, se apunta ahí. Sin él, se arranca Coconut dentro del
        // teléfono sin preguntar nada.
        if (base.isNotEmpty()) web.loadUrl("$base/app/") else arrancarLocal()

        // El intent con el que se ABRE la app también puede traer el code: si la app
        // estaba cerrada, Android la lanza con él en vez de llamar a `onNewIntent`.
        atenderRedirect(intent)
    }

    /* Puente con la interfaz */

    /**
     * Lo mínimo para que Ajustes pueda cambiar de servidor desde la propia web.
     *
     * Como la app arranca sola, el diálogo de servidor no vuelve a salir por su cuenta: sin
     * esto no hay forma de apuntar a un Coconut propio salvo desinstalando. Vive en Ajustes,
     * y para eso el JavaScript necesita leer y escribir una preferencia de Android.
     *
     * Tres métodos y nada más. Un puente es código nativo expuesto a la web: cada cosa que
     * se añada aquí puede llamarla cualquier página que acabe cargándose en este WebView.
     * Leer y escribir la URL del servidor es inocuo; no se expone nada del sistema de
     * ficheros, ni del token, ni de la base de datos.
     */
    inner class Puente {
        @android.webkit.JavascriptInterface
        fun servidor(): String = base

        @android.webkit.JavascriptInterface
        fun modoLocal(): Boolean = base.isEmpty()

        /** Guarda y recarga. Cadena vacía = volver al Coconut del teléfono. */
        @android.webkit.JavascriptInterface
        fun fijarServidor(url: String) {
            val limpia = url.trim().trimEnd('/')
            runOnUiThread {
                base = limpia
                if (limpia.isEmpty()) {
                    // Volver al local: si ya está arrancado se reutiliza, y si no, arranca.
                    if (urlLocal.isNotEmpty()) web.loadUrl("$urlLocal/app/") else arrancarLocal()
                } else {
                    web.loadUrl("$limpia/app/")
                }
            }
        }
    }

    /* El `code` que llega DESDE FUERA */

    /**
     * Recoge el `authredirect://` cuando el login NO ocurrió en el WebView de la app.
     *
     * El `intent-filter` del manifiesto declara el esquema, y sin este manejador Android
     * entrega el `code` a la app y se pierde.
     *
     * Y no es un caso raro: Google bloquea el login OAuth dentro de un WebView
     * (`disallowed_useragent`) para evitar que una app pueda leer las credenciales. La
     * salida entonces es abrir el login en el navegador del sistema, y la vuelta llega por
     * aquí, no por `shouldOverrideUrlLoading`.
     */
    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        atenderRedirect(intent)
    }

    private fun atenderRedirect(intent: android.content.Intent?) {
        val datos = intent?.data?.toString() ?: return
        if (!datos.startsWith("authredirect://")) return
        Log.i("COCONUT", "code recibido desde fuera de la app")
        // Puede llegar con la app recién abierta y el servidor aún arrancando: se reintenta
        // hasta que haya una raíz a la que entregárselo. Perder el code obliga a repetir
        // todo el login, así que un par de segundos de espera salen baratos.
        esperarYEntregar(datos, intentos = 20)
    }

    private fun esperarYEntregar(url: String, intentos: Int) {
        if (raiz.isNotEmpty()) { entregarCodeAlServidor(url); return }
        if (intentos <= 0) {
            Log.e("COCONUT", "llegó un code pero Coconut no estaba listo")
            return
        }
        web.postDelayed({ esperarYEntregar(url, intentos - 1) }, 500)
    }

    /* Coconut dentro del teléfono */

    /**
     * Levanta la API en `127.0.0.1` y apunta el WebView ahí.
     *
     * Todo fuera del hilo principal: arrancar el intérprete, extraer la interfaz y esperar a
     * que uvicorn escuche puede tardar varios segundos, y hacerlo en el hilo de la interfaz
     * congela la pantalla hasta que Android muestra el diálogo de «la aplicación no
     * responde». Mientras tanto se enseña un mensaje en vez de una pantalla en negro.
     */
    private fun arrancarLocal() {
        setContentView(pantallaDeEspera())
        pedirNotificaciones()
        // El motor NO se levanta aquí: lo levanta `ServicioCoconut`, que sigue vivo con la
        // app cerrada. La Activity solo espera a que esté listo y apunta el WebView.
        ServicioCoconut.arrancar(this)

        thread(name = "coconut-espera") {
            // Espera activa y acotada. El arranque en frío extrae la interfaz, crea el
            // esquema e importa el proyecto entero: en un móvil lento puede pasar del
            // minuto, y rendirse antes dejaría una pantalla de error con el motor
            // levantándose por detrás.
            val limite = System.currentTimeMillis() + 120_000
            while (ServicioCoconut.url.isEmpty() && System.currentTimeMillis() < limite) {
                if (ServicioCoconut.estado == "No ha podido arrancar") {
                    runOnUiThread { avisarFalloArranque(IllegalStateException(
                        "el motor no arrancó; mira el registro con la etiqueta COCONUT")) }
                    return@thread
                }
                Thread.sleep(250)
            }
            val raizLocal = ServicioCoconut.url
            if (raizLocal.isEmpty()) {
                runOnUiThread { avisarFalloArranque(
                    IllegalStateException("el motor tardó más de 2 minutos en arrancar")) }
                return@thread
            }
            Log.i("COCONUT", "Coconut local en $raizLocal")
            urlLocal = raizLocal
            runOnUiThread {
                setContentView(refresco)
                web.loadUrl("$raizLocal/app/")
            }
        }
    }

    /**
     * Pide permiso para la notificación del motor (API 33+).
     *
     * No bloquea el arranque. Si el usuario lo deniega, el servicio corre igual y los
     * trabajos siguen a su hora; lo único que se pierde es ver que está vivo. Tratarlo como
     * obligatorio sería cambiar una molestia por una app que no arranca.
     */
    private fun pedirNotificaciones() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
            == android.content.pm.PackageManager.PERMISSION_GRANTED) return
        requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 1)
    }

    /** Un mensaje mientras arranca. Sin esto el primer uso es una pantalla en negro. */
    private fun pantallaDeEspera(): ViewGroup {
        val caja = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            // El MISMO color que `windowBackground`: con dos negros distintos se ve una
            // costura al cambiar de la pantalla de espera a la web.
            setBackgroundColor(this@MainActivity.getColor(R.color.fondo))
            setPadding(48, 48, 48, 48)
        }
        caja.addView(ProgressBar(this))
        caja.addView(TextView(this).apply {
            text = "Arrancando Coconut…\nLa primera vez tarda unos segundos."
            setTextColor(this@MainActivity.getColor(R.color.texto))
            gravity = Gravity.CENTER
            setPadding(0, 40, 0, 0)
        })
        return caja
    }

    /**
     * Si el arranque local falla, se ofrece la salida: apuntar a un servidor.
     *
     * No se deja la app muerta con un error: un fallo de arranque tiene alternativa.
     */
    private fun avisarFalloArranque(e: Throwable) {
        AlertDialog.Builder(this)
            .setTitle("No se pudo arrancar Coconut aquí")
            .setMessage("${e::class.java.simpleName}: ${e.message}\n\n" +
                        "Puedes apuntar a un servidor propio mientras tanto.")
            .setPositiveButton("Usar un servidor") { _, _ -> pedirServidor() }
            .setNegativeButton("Reintentar") { _, _ -> arrancarLocal() }
            .setCancelable(false)
            .show()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configurarWeb() {
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true          // la web usa localStorage
            // Coconut se sirve por HTTP y Android bloquea el tráfico en claro desde
            // API 28. `res/xml/red.xml` lo permite (para cualquier host, porque la
            // dirección la pone cada usuario) y esta línea es su contraparte en el WebView.
            mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            // Sin esto el layout responsive no se aplica: el WebView usaría un viewport
            // de escritorio y anularía todo el trabajo de adaptación de la web.
            useWideViewPort = true
            loadWithOverviewMode = false
            setSupportZoom(false)
            builtInZoomControls = false
            // Identificarse como app propia ayuda a distinguir estas peticiones en el log
            // de la API de las del navegador.
            userAgentString = "$userAgentString CoconutApp"
        }
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true)
        // El nombre `Coconut` es el que busca la web para saber que corre dentro del APK:
        // si no existe, la tarjeta de servidor propio ni se dibuja.
        web.addJavascriptInterface(Puente(), "Coconut")

        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                v: WebView, req: WebResourceRequest
            ): Boolean {
                val url = req.url.toString()

                // LaLiga devuelve el `code` del login a `authredirect://…`, un esquema de
                // app que ningún navegador sabe abrir: en web hay que copiar la URL a mano.
                // Aquí se captura y se le entrega a Coconut, que lo canjea.
                if (url.startsWith("authredirect://")) {
                    entregarCodeAlServidor(url)
                    return true
                }
                // Todo lo del servidor propio se queda dentro; lo demás (Google, enlaces
                // externos) también, porque el login de Google ocurre en este WebView.
                return false
            }

            override fun onPageFinished(v: WebView, url: String) { refresco.isRefreshing = false }
        }
    }

    /**
     * Pasa el `code` del login a la pantalla de Ajustes de Coconut.
     *
     * Se hace desde JavaScript y no con una petición nativa a propósito: así el flujo es
     * exactamente el mismo que pulsando el botón en el navegador (mismos endpoints, mismas
     * comprobaciones, mismo manejo de errores) y no hay una segunda implementación del
     * login que pueda divergir de la web.
     */
    private fun entregarCodeAlServidor(url: String) {
        val code = Regex("code=([^&]+)").find(url)?.groupValues?.get(1) ?: return
        web.loadUrl("$raiz/app/#ajustes")
        web.postDelayed({
            web.evaluateJavascript(
                """
                (async () => {
                  try {
                    const r = await fetch('/auth/laliga/code', {
                      method: 'POST',
                      headers: {'content-type': 'application/json'},
                      body: JSON.stringify({pegado: ${jsonString(code)}})
                    });
                    const d = await r.json();
                    alert(r.ok ? 'Cuenta de LaLiga conectada'
                               : 'No se pudo conectar: ' + (d.detail || r.status));
                    location.reload();
                  } catch (e) { alert('Error al conectar: ' + e.message); }
                })();
                """.trimIndent(), null)
        }, 1200)
    }

    /** Escapa una cadena para incrustarla en JavaScript sin romper comillas. */
    private fun jsonString(s: String) =
        "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\""

    /**
     * Apuntar a un servidor propio, para quien lo tenga.
     *
     * No se puede traer por defecto: cada instalación tiene su propia IP o dominio, y la
     * de este proyecto vive detrás de una VPN.
     *
     * Dejarlo en blanco es una respuesta válida: significa «usa el Coconut del teléfono».
     */
    private fun pedirServidor() {
        val campo = EditText(this).apply {
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            hint = "http://TU_SERVIDOR:8140"
            setText(prefs.getString("base", "http://"))
        }
        val caja = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 24, 48, 0)
            addView(campo)
        }
        AlertDialog.Builder(this)
            .setTitle("¿Dónde está tu Coconut?")
            .setMessage("Si tienes un servidor propio, su dirección con http:// y puerto " +
                        "(conecta la VPN antes). Si no, Coconut funciona dentro del móvil.")
            .setView(caja)
            .setCancelable(true)
            .setPositiveButton("Conectar") { _, _ ->
                base = campo.text.toString().trim()
                if (base.isEmpty()) arrancarLocal() else web.loadUrl("$base/app/")
            }
            .setNegativeButton("Usar el del teléfono") { _, _ ->
                base = ""
                arrancarLocal()
            }
            .show()
    }
}
