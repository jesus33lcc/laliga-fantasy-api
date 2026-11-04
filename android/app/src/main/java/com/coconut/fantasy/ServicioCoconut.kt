package com.coconut.fantasy

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlin.concurrent.thread

/**
 * Mantiene Coconut vivo con una notificación permanente.
 *
 * Android no garantiza ejecutar nada en segundo plano: Doze, App Standby y las capas de
 * Xiaomi, Samsung o Huawei matan los procesos de una app que no está en pantalla. Con el
 * servidor dentro de la Activity, tres trabajos que dependen del reloj se quedan sin correr:
 * guardar el once antes de que se congele (±12 min, o la jornada entera puntúa 0), el
 * centinela de clausulazos (10 min) y las pujas que resuelven a las 13:48. Un servicio en
 * primer plano es la única forma que el sistema respeta, y el precio es una notificación
 * que no se puede quitar.
 *
 * Tres reglas de Android que hay que respetar o la app se cae:
 *
 * 1. `startForeground()` antes de 5 s, o el sistema lanza
 *    `ForegroundServiceDidNotStartInTimeException`. Arrancar Python tarda mucho más, así
 *    que primero se publica la notificación y después se arranca, en un hilo.
 * 2. Desde API 34 hay que declarar el tipo, en el manifiesto y en la llamada. Aquí es
 *    `dataSync`.
 * 3. `Python.start()` solo se puede llamar una vez por proceso, y el servicio comparte
 *    proceso con la Activity: sin la guarda `isStarted()`, abrir la app con el servicio ya
 *    en marcha revienta el intérprete.
 *
 * Devuelve `START_STICKY`: si el sistema lo mata por memoria lo recrea, y entonces el
 * `Intent` llega nulo.
 */
class ServicioCoconut : Service() {

    companion object {
        const val CANAL = "coconut-motor"
        const val ID_NOTIFICACION = 1
        const val ACCION_PARAR = "com.coconut.fantasy.PARAR"

        /** URL local, o cadena vacía mientras arranca. La lee la Activity. */
        @Volatile var url: String = ""
            private set

        /** Qué contar en la notificación y en la pantalla de espera. */
        @Volatile var estado: String = "Arrancando…"
            private set

        @Volatile private var arrancando = false

        fun arrancar(ctx: Context) {
            val i = Intent(ctx, ServicioCoconut::class.java)
            // `startForegroundService` obliga al servicio a llamar a `startForeground()`
            // enseguida; en versiones viejas no existe y basta con `startService`.
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) ctx.startForegroundService(i)
            else ctx.startService(i)
        }

        fun parar(ctx: Context) {
            ctx.startService(Intent(ctx, ServicioCoconut::class.java).setAction(ACCION_PARAR))
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACCION_PARAR) {
            Log.i("COCONUT", "parando el motor a petición del usuario")
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return START_NOT_STICKY
        }

        // PRIMERO la notificación: el sistema la exige antes de 5 s.
        crearCanal()
        publicar(estado)

        // Y solo después el arranque, que tarda. La guarda evita arrancar dos veces
        // cuando el sistema recrea el servicio (ahí `intent` llega nulo).
        if (url.isEmpty() && !arrancando) {
            arrancando = true
            thread(name = "coconut-motor") { levantar() }
        }
        return START_STICKY
    }

    private fun levantar() {
        try {
            if (!Python.isStarted()) Python.start(AndroidPlatform(this))
            val web = Interfaz.preparar(this)
            val db = Interfaz.esquema(this)
            val u = Python.getInstance().getModule("servidor")
                .callAttr("arrancar", filesDir.absolutePath, web, db)
                .toString()
            url = u.removeSuffix("/app/").removeSuffix("/")
            estado = "En marcha"
            Log.i("COCONUT", "motor levantado en $url")
        } catch (e: Throwable) {
            // Un fallo aquí NO tumba el servicio: la notificación pasa a decir que algo
            // va mal, que es más útil que desaparecer sin dejar rastro.
            estado = "No ha podido arrancar"
            Log.e("COCONUT", "el motor no pudo arrancar", e)
        } finally {
            arrancando = false
            publicar(estado)
        }
    }

    /** El canal. Sin importancia baja, cada actualización haría sonar el teléfono. */
    private fun crearCanal() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val canal = NotificationChannel(CANAL, "Motor de Coconut",
                                        NotificationManager.IMPORTANCE_LOW).apply {
            description = "Avisa de que Coconut sigue ingiriendo y vigilando la liga."
            setShowBadge(false)
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(canal)
    }

    private fun publicar(texto: String) {
        val abrir = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val parar = PendingIntent.getService(
            this, 1, Intent(this, ServicioCoconut::class.java).setAction(ACCION_PARAR),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

        val n = Notification.Builder(this, CANAL)
            .setContentTitle("Coconut")
            .setContentText(texto)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentIntent(abrir)
            .addAction(Notification.Action.Builder(null, "Parar", parar).build())
            .setOngoing(true)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(ID_NOTIFICACION, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(ID_NOTIFICACION, n)
        }
    }
}
