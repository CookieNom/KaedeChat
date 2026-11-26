package chat.kaede.kaede_mobile

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.ResultReceiver

class VoiceForegroundService : Service() {
    private var microphone = false
    private var camera = false
    private var screenShare = false
    companion object {
        private var running: VoiceForegroundService? = null

        fun screenCaptureStopped(): Boolean {
            val service = running ?: return true
            if (!service.screenShare) return true
            service.startVoiceForeground(service.buildNotification(false),
                useCamera = service.camera, useMicrophone = service.microphone, useScreenShare = false)
            service.screenShare = false
            return true
        }

        private const val ACTION_START = "chat.kaede.mobile.voice.START"
        private const val EXTRA_CAMERA = "chat.kaede.mobile.voice.CAMERA"
        private const val EXTRA_MICROPHONE = "chat.kaede.mobile.voice.MICROPHONE"
        private const val EXTRA_SCREEN_SHARE = "chat.kaede.mobile.voice.SCREEN_SHARE"
        private const val CHANNEL_ID = "kaede_voice_calls"
        private const val NOTIFICATION_ID = 7301

        fun start(context: Context, microphone: Boolean, screenShare: Boolean, camera: Boolean, complete: (Boolean) -> Unit) {
            val handler = Handler(Looper.getMainLooper())
            var finished = false
            val timeout = Runnable { if (!finished) { finished = true; complete(false) } }
            val response = object : ResultReceiver(handler) {
                override fun onReceiveResult(code: Int, data: Bundle?) {
                    handler.removeCallbacks(timeout)
                    if (!finished) { finished = true; complete(code == 1) }
                }
            }
            val intent = Intent(context, VoiceForegroundService::class.java)
                .setAction(ACTION_START)
                .putExtra("response", response)
                .putExtra(EXTRA_CAMERA, camera)
                .putExtra(EXTRA_MICROPHONE, microphone)
                .putExtra(EXTRA_SCREEN_SHARE, screenShare)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
            handler.postDelayed(timeout, 5000)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, VoiceForegroundService::class.java))
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action != ACTION_START) {
            stopForegroundCompat()
            stopSelf()
            return START_NOT_STICKY
        }
        @Suppress("DEPRECATION")
        val response = intent.getParcelableExtra<ResultReceiver>("response")
        try {
            createNotificationChannel()
            startVoiceForeground(
                buildNotification(intent.getBooleanExtra(EXTRA_SCREEN_SHARE, false)),
                useCamera = intent.getBooleanExtra(EXTRA_CAMERA, false),
                useMicrophone = intent.getBooleanExtra(EXTRA_MICROPHONE, false),
                useScreenShare = intent.getBooleanExtra(EXTRA_SCREEN_SHARE, false),
            )
            camera = intent.getBooleanExtra(EXTRA_CAMERA, false)
            microphone = intent.getBooleanExtra(EXTRA_MICROPHONE, false)
            screenShare = intent.getBooleanExtra(EXTRA_SCREEN_SHARE, false)
            running = this
            response?.send(1, null)
        } catch (error: SecurityException) {
            response?.send(0, null)
            if (running != this) stopSelf(startId)
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        if (running == this) running = null
        stopForegroundCompat()
        super.onDestroy()
    }

    private fun buildNotification(screenShare: Boolean): Notification {
        val launchIntent = packageManager.getLaunchIntentForPackage(packageName)
        val contentIntent = launchIntent?.let {
            PendingIntent.getActivity(
                this,
                0,
                it,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
        }
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setSmallIcon(R.drawable.ic_stat_kaede)
            .setContentTitle(if (screenShare) "Kaede is sharing your screen" else "Kaede voice connected")
            .setContentText(if (screenShare) "Tap to manage or stop sharing" else "Tap to return to your call")
            .setCategory(Notification.CATEGORY_CALL)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(contentIntent)
            .build()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Voice calls",
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = "Keeps active Kaede voice calls connected"
            setSound(null, null)
            enableVibration(false)
            setShowBadge(false)
        }
        manager.createNotificationChannel(channel)
    }

    private fun startVoiceForeground(
        notification: Notification,
        useCamera: Boolean,
        useMicrophone: Boolean,
        useScreenShare: Boolean,
    ) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            var serviceTypes = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
            if (
                useMicrophone &&
                checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
            ) {
                serviceTypes = serviceTypes or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && useCamera && checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
                serviceTypes = serviceTypes or ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
            }
            if (useScreenShare) {
                serviceTypes = serviceTypes or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
            }
            startForeground(NOTIFICATION_ID, notification, serviceTypes)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION")
            stopForeground(true)
        }
    }
}
