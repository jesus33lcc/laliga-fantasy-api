package com.coconut.fantasy

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Comprueba que la pila de Coconut aguanta dentro de un móvil.
 *
 * Corre en un emulador real dentro de CI, no en la JVM del host: Python necesita el runtime
 * de Android para arrancar, así que un test unitario normal no probaría nada.
 *
 * Responde tres cosas: que el intérprete arranque de verdad en el teléfono, que las
 * dependencias existan para Android (eso ya lo contesta que la build termine, porque
 * Chaquopy ejecuta `pip` al compilar) y qué versión de SQLite hay. La última es la que
 * puede invalidar el enfoque sin avisar: el proyecto usa `->>` (3.38+), `FILTER` (3.30+)
 * y funciones de ventana (3.25+), y la libsqlite del sistema puede ser muy anterior.
 */
@RunWith(AndroidJUnit4::class)
class PruebaPilaTest {

    @Test
    fun laPilaDeCoconutFuncionaEnElMovil() {
        val ctx = InstrumentationRegistry.getInstrumentation().targetContext
        if (!Python.isStarted()) Python.start(AndroidPlatform(ctx))

        // Los assets se extraen ANTES del diagnóstico, y el orden importa: `api/main.py`
        // calcula la ruta de la interfaz al importarse, así que si el paso 5 importa el
        // paquete sin `COCONUT_WEB` puesta, el montaje de `/app` se decide con la ruta
        // equivocada y ya no hay vuelta atrás. La app real no llega a ese caso (nada importa
        // el paquete antes de `arrancar()`), pero la prueba tiene que reproducir el mismo
        // orden o comprueba algo distinto de lo que se ejecuta.
        val dirWeb = Interfaz.preparar(ctx)
        val dirDb = Interfaz.esquema(ctx)

        val bd = ctx.getDatabasePath("prueba_coconut.db").also { it.parentFile?.mkdirs() }
        val crudo = Python.getInstance()
            .getModule("prueba_pila")
            .callAttr("diagnostico", bd.absolutePath, dirWeb)
            .toString()

        // El diagnóstico sale por LOGCAT, que es un búfer del sistema: sobrevive a la
        // desinstalación y se lee con `adb logcat -d`. Las otras dos vías no sirven.
        // `println` no llega: Gradle no mete la salida de un test instrumentado ni en el
        // informe HTML ni en el XML. Y un fichero de la app tampoco, porque
        // `connectedAndroidTest` la DESINSTALA al terminar y da "unknown package".
        //
        // Se trocea porque logcat corta las líneas largas (~4 KB) y una línea partida por
        // la mitad ya no es JSON.
        val etiqueta = "COCONUT_DIAG"
        crudo.chunked(500).forEachIndexed { i, trozo ->
            android.util.Log.i(etiqueta, "[$i]$trozo")
        }
        android.util.Log.i(etiqueta, "[FIN]")
        println(crudo)                       // por si algún día sí se captura
        java.io.File(ctx.filesDir, "diagnostico.json").writeText(crudo)

        // Paso 6: más allá de que el código importe. Levanta uvicorn en 127.0.0.1 dentro
        // del teléfono, extrae `web/` de los assets y comprueba que `/app/` responde con
        // la interfaz.
        var servidorOk = "no se intentó"
        try {
            val url = Python.getInstance().getModule("servidor")
                .callAttr("arrancar", ctx.filesDir.absolutePath, dirWeb, dirDb)
                .toString()
            val conexion = (java.net.URL(url).openConnection() as java.net.HttpURLConnection)
                .apply { connectTimeout = 20_000; readTimeout = 20_000 }
            val codigo = conexion.responseCode
            val bytes = conexion.inputStream.readBytes().size
            servidorOk = "$url -> HTTP $codigo, $bytes bytes"
            android.util.Log.i("COCONUT_DIAG", "[SRV]$servidorOk")
            assertTrue("La API local respondió $codigo en vez de 200", codigo == 200)
            assertTrue("La interfaz vino vacía ($bytes bytes)", bytes > 500)
        } catch (e: Throwable) {
            servidorOk = "[FALLO] ${e::class.java.simpleName}: ${e.message}"
            android.util.Log.i("COCONUT_DIAG", "[SRV]$servidorOk")
            throw e
        }

        val j = JSONObject(crudo)
        val pasos = j.getJSONArray("pasos")
        val fallidos = (0 until pasos.length())
            .map { pasos.getJSONObject(it) }
            .filter { !it.getBoolean("ok") }
            .joinToString("\n") { "${it.getString("paso")}: ${it.getString("detalle")}" }

        assertTrue("Pasos que fallaron en el móvil:\n$fallidos", fallidos.isEmpty())
    }
}
