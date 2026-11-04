package com.coconut.fantasy

import android.content.Context
import android.util.Log
import java.io.File

/**
 * Deja los ficheros de la interfaz (`web/`) en un directorio REAL del teléfono.
 *
 * Hay que copiarlos porque `StaticFiles` necesita un directorio del sistema de ficheros, y
 * dentro del APK los assets son un archivo comprimido al que solo se llega por
 * `AssetManager`. Se extraen a `filesDir/web` y esa ruta se le pasa a Python por
 * `COCONUT_WEB`.
 *
 * Solo una vez: son ~15 ficheros y 700 KB, medio segundo en cada apertura de la app. El
 * sello es el `versionCode`, no una marca de tiempo ni la existencia del directorio. Con
 * «si existe, no copies», una actualización seguiría sirviendo la interfaz vieja y el
 * arreglo desplegado no llegaría nunca a la pantalla.
 */
object Interfaz {

    private const val TAG = "COCONUT_WEB"

    /** La interfaz. Devuelve la ruta para `COCONUT_WEB`. */
    fun preparar(ctx: Context): String = extraer(ctx, "web")

    /** El esquema SQL. Sin esto, un móvil nuevo tiene una base de datos VACÍA. */
    fun esquema(ctx: Context): String = extraer(ctx, "db")

    private fun extraer(ctx: Context, carpeta: String): String {
        val destino = File(ctx.filesDir, carpeta)
        val sello = File(ctx.filesDir, "$carpeta.version")
        val version = ctx.packageManager
            .getPackageInfo(ctx.packageName, 0).longVersionCode.toString()

        if (sello.exists() && sello.readText() == version && destino.isDirectory) {
            return destino.absolutePath
        }

        destino.deleteRecursively()
        val n = copiar(ctx, carpeta, destino)
        sello.writeText(version)
        Log.i(TAG, "«$carpeta» extraído: $n ficheros en ${destino.absolutePath}")
        return destino.absolutePath
    }

    /** Copia recursivamente una carpeta de assets. Devuelve cuántos ficheros. */
    private fun copiar(ctx: Context, ruta: String, destino: File): Int {
        val hijos = ctx.assets.list(ruta) ?: return 0
        // Un asset sin hijos es un FICHERO. `list()` no distingue, así que esa es la
        // única forma de saberlo: y equivocarse deja carpetas vacías en silencio.
        if (hijos.isEmpty()) {
            destino.parentFile?.mkdirs()
            ctx.assets.open(ruta).use { entrada ->
                destino.outputStream().use { salida -> entrada.copyTo(salida) }
            }
            return 1
        }
        destino.mkdirs()
        var n = 0
        for (hijo in hijos) n += copiar(ctx, "$ruta/$hijo", File(destino, hijo))
        return n
    }
}
