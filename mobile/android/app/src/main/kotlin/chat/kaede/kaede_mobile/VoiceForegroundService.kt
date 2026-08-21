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

class VoiceForegroundService : Service() {
    companion object {
        private const val ACTION_START = "chat.kaede.mobile.voice.START"
        private const val EXTRA_MICROPHONE = "chat.kaede.mobile.voice.MICROPHONE"
        private const val EXTRA_SCREEN_SHARE = "chat.kaede.mobile.voice.SCREEN_SHARE"
        private const val CHANNEL_ID = "kaede_voice_calls"
        private const val NOTIFICATION_ID = 7301

        fun start(context: Context, microphone: Boolean, screenShare: Boolean) {
            val intent = Intent(context, VoiceForegroundService::class.java)
                .setAction(ACTION_START)
                .putExtra(EXTRA_MICROPHONE, microphone)
                .putExtra(EXTRA_SCREEN_SHARE, screenShare)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
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
        createNotificationChannel()
        startVoiceForeground(
            buildNotification(intent.getBooleanExtra(EXTRA_SCREEN_SHARE, false)),
            useMicrophone = intent.getBooleanExtra(EXTRA_MICROPHONE, false),
            useScreenShare = intent.getBooleanExtra(EXTRA_SCREEN_SHARE, false),
        )
        return START_NOT_STICKY
    }

    override fun onDestroy() {
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
