package chat.kaede.kaede_mobile

import chat.kaede.kaede_mobile.system_calls.R
import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Person
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.media.AudioAttributes
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.Gravity
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import io.flutter.plugin.common.MethodChannel
import org.json.JSONObject

internal object CallPresentation {
    private const val channelId = "kaede_incoming_calls_v2"
    private const val lifetime = 60_000L
    private var channel: MethodChannel? = null
    private var account: String? = null
    private val delivering = mutableSetOf<String>()
    fun prefs(context: Context): SharedPreferences = context.getSharedPreferences("kaede_system_calls", Context.MODE_PRIVATE)
    private fun manager(context: Context) = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    fun remember(context: Context, id: String, caller: String, channelRef: String, accountKey: String) {
        val p = prefs(context)
        p.edit().putString("ring:$id", JSONObject(mapOf("callId" to id, "channelRef" to channelRef,
            "callerName" to caller, "accountKey" to accountKey, "expires" to (System.currentTimeMillis() + lifetime))).toString()).apply()
    }

    fun details(context: Context, id: String): JSONObject? =
        prefs(context).getString("ring:$id", null)?.let { JSONObject(it) }

    private fun intent(context: Context, id: String, action: String) = PendingIntent.getActivity(
        context, 0, Intent(context, IncomingCallActivity::class.java).apply {
            data = Uri.fromParts("kaede-call", id, action)
            putExtra("id", id)
            putExtra("action", action)
        }, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    fun show(context: Context, id: String) {
        val details = details(context, id) ?: return
        val nm = manager(context)
        val ringtone = Settings.System.DEFAULT_RINGTONE_URI
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            val legacy = nm.getNotificationChannel("kaede_calls")
            val customized = legacy != null && (legacy.importance != NotificationManager.IMPORTANCE_HIGH &&
                legacy.importance != NotificationManager.IMPORTANCE_MAX ||
                legacy.sound !in setOf(Settings.System.DEFAULT_RINGTONE_URI, Settings.System.DEFAULT_NOTIFICATION_URI) ||
                (Build.VERSION.SDK_INT >= 30 && legacy.hasUserSetSound()) ||
                (Build.VERSION.SDK_INT >= 29 && legacy.hasUserSetImportance()))
            val chosen = if (customized) "kaede_calls" else channelId
            if (!customized) nm.createNotificationChannel(NotificationChannel(chosen, context.getString(R.string.incoming_calls_channel), NotificationManager.IMPORTANCE_HIGH).apply {
                setSound(ringtone, AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE).build())
            })
            Notification.Builder(context, chosen)
        } else Notification.Builder(context).setSound(ringtone, android.media.AudioManager.STREAM_RING)
        val open = intent(context, id, "open")
        val notification = builder.setSmallIcon(android.R.drawable.sym_call_incoming)
            .setContentTitle(details.getString("callerName")).setContentText(context.getString(R.string.incoming_call))
            .setCategory(Notification.CATEGORY_CALL).setVisibility(Notification.VISIBILITY_PRIVATE)
            .setPriority(Notification.PRIORITY_MAX).setOngoing(true).setOnlyAlertOnce(true)
            .setContentIntent(open).setFullScreenIntent(open, true)
            .apply {
                val answer = intent(context, id, "answer")
                val decline = intent(context, id, "decline")
                if (Build.VERSION.SDK_INT >= 31) {
                    val caller = Person.Builder().setName(details.getString("callerName")).build()
                    setStyle(Notification.CallStyle.forIncomingCall(caller, decline, answer))
                } else {
                    addAction(android.R.drawable.ic_menu_close_clear_cancel, context.getString(R.string.decline_call), decline)
                    addAction(android.R.drawable.sym_action_call, context.getString(R.string.answer_call), answer)
                }
                if (Build.VERSION.SDK_INT >= 26) setTimeoutAfter(lifetime)
            }.build()
        notification.flags = notification.flags or Notification.FLAG_INSISTENT
        nm.notify(id, 1, notification)
    }

    fun hide(context: Context, id: String, ended: Boolean) {
        manager(context).cancel(id, 1)
        val p = prefs(context)
        val detail = details(context, id) ?: return
        if (ended) p.edit().remove("ring:$id").apply()
        else p.edit().putString("ring:$id", detail.put("active", true).toString()).apply()
    }

    fun action(context: Context, id: String, action: String) {
        val detail = details(context, id) ?: return
        if (action != "ended" && detail.getLong("expires") < System.currentTimeMillis()) {
            KaedeConnectionService.end(context, id)
            return
        }
        val p = prefs(context)
        val update = detail.put("action", action).put("expires", System.currentTimeMillis() + lifetime)
        p.edit().putString("event:$id", update.toString()).commit()
        manager(context).cancel(id, 1)
        if (action != "answer") KaedeConnectionService.end(context, id)
        deliver(context, id)
        if (channel == null || action == "answer") {
            context.packageManager.getLaunchIntentForPackage(context.packageName)?.let {
                context.startActivity(it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP))
            }
        }
    }

    private fun deliver(context: Context, id: String) {
        val target = channel ?: return
        val p = prefs(context)
        val key = "event:$id"
        val value = p.getString(key, null) ?: return
        val detail = JSONObject(value)
        if (detail.getString("accountKey") != account || detail.getLong("expires") < System.currentTimeMillis()) {
            p.edit().remove(key).apply()
            return
        }
        if (!delivering.add(id)) return
        target.invokeMethod(detail.getString("action"), listOf("callId", "channelRef", "callerName", "accountKey")
            .associateWith { detail.getString(it) }, object : MethodChannel.Result {
            override fun success(result: Any?) {
                delivering.remove(id)
                if (result == true && p.getString(key, null) == value) p.edit().remove(key).apply()
                else if (result == true) deliver(context, id)
            }
            override fun error(code: String, message: String?, details: Any?) { delivering.remove(id) }
            override fun notImplemented() { delivering.remove(id) }
        })
    }

    fun ready(context: Context, accountKey: String, target: MethodChannel) {
        account = accountKey
        channel = target
        val now = System.currentTimeMillis()
        for (key in prefs(context).all.keys) {
            val id = key.substringAfter(':')
            if (key.startsWith("event:")) deliver(context, id)
            else details(context, id)?.let {
                if (it.getString("accountKey") != accountKey || !it.optBoolean("active") && it.getLong("expires") < now)
                    KaedeConnectionService.end(context, id)
            }
        }
    }

    fun detach(target: MethodChannel?) {
        if (channel == target) { channel = null; account = null; delivering.clear() }
    }

    fun reset(context: Context) {
        detach(channel)
        prefs(context).all.keys.filter { it.startsWith("ring:") }.forEach { KaedeConnectionService.end(context, it.substringAfter(':')) }
        prefs(context).edit().clear().apply()
    }
}

/** The lock screen only exposes the caller and the two call actions. */
class IncomingCallActivity : Activity(), SharedPreferences.OnSharedPreferenceChangeListener {
    private val id get() = intent.getStringExtra("id").orEmpty()
    private val timeout = Handler(Looper.getMainLooper())
    private val finishRing = Runnable { finish() }

    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or WindowManager.LayoutParams.FLAG_SECURE)
        val call = CallPresentation.details(this, id)
        val remaining = (call?.optLong("expires") ?: 0) - System.currentTimeMillis()
        if (call == null || call.optBoolean("active") || remaining <= 0) { finish(); return }
        val command = intent.getStringExtra("action")
        if (command == "answer" || command == "decline") { handle(command); return }
        val padding = (24 * resources.displayMetrics.density).toInt()
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(padding, padding, padding, padding)
        }
        layout.addView(TextView(this).apply { text = call.getString("callerName"); textSize = 24f })
        layout.addView(TextView(this).apply { setText(R.string.incoming_call) })
        for ((action, label) in listOf("answer" to R.string.answer_call, "decline" to R.string.decline_call)) {
            layout.addView(Button(this).apply { setText(label); setOnClickListener { handle(action) } })
        }
        setContentView(layout)
        CallPresentation.prefs(this).registerOnSharedPreferenceChangeListener(this)
        timeout.postDelayed(finishRing, remaining)
    }

    private fun handle(action: String) { CallPresentation.action(this, id, action); finish() }
    override fun onSharedPreferenceChanged(prefs: SharedPreferences?, key: String?) {
        if (key == "ring:$id" || key == "event:$id") finish()
    }
    override fun onDestroy() {
        timeout.removeCallbacks(finishRing)
        CallPresentation.prefs(this).unregisterOnSharedPreferenceChangeListener(this)
        super.onDestroy()
    }
}
