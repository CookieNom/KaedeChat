package chat.kaede.kaede_mobile

import android.content.ComponentName
import android.os.Bundle
import android.os.Build
import android.app.PictureInPictureParams
import android.content.res.Configuration
import android.content.pm.PackageManager
import android.util.Rational
import android.telecom.PhoneAccount
import android.telecom.PhoneAccountHandle
import android.telecom.TelecomManager
import androidx.lifecycle.Lifecycle
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.embedding.android.FlutterFragmentActivity
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterFragmentActivity() {
    private lateinit var systemCallChannel: MethodChannel
    private lateinit var pipChannel: MethodChannel
    private var autoPip = false

    private fun pipSupported() = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
        packageManager.hasSystemFeature(PackageManager.FEATURE_PICTURE_IN_PICTURE)

    override fun onUserLeaveHint() {
        super.onUserLeaveHint()
        if (autoPip && pipSupported() && Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            try { enterPictureInPictureMode(PictureInPictureParams.Builder().setAspectRatio(Rational(16, 9)).build()) }
            catch (_: IllegalStateException) { /* System policy can deny PiP. */ }
        }
    }

    override fun onPictureInPictureModeChanged(active: Boolean, config: Configuration) {
        super.onPictureInPictureModeChanged(active, config)
        if (::pipChannel.isInitialized) pipChannel.invokeMethod("state", active)
    }

    override fun onStop() {
        super.onStop()
        // Closing PiP can leave the activity in PiP mode but no longer visible.
        if (::pipChannel.isInitialized) pipChannel.invokeMethod("state", false)
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        pipChannel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "chat.kaede.mobile/video_pip")
        pipChannel.setMethodCallHandler { call, result ->
            if (call.method == "configure") {
                autoPip = call.argument<Boolean>("enabled") == true && pipSupported()
                try {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && pipSupported()) {
                        setPictureInPictureParams(PictureInPictureParams.Builder()
                            .setAspectRatio(Rational(16, 9)).setAutoEnterEnabled(autoPip).build())
                    }
                    result.success(pipSupported())
                } catch (error: Exception) {
                    autoPip = false
                    result.error("PIP_FAILED", error.message, null)
                }
            } else result.notImplemented()
        }
        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "chat.kaede.mobile/voice_lifecycle",
        ).setMethodCallHandler { call, result ->
            if (call.method != "setCallActive") {
                result.notImplemented()
                return@setMethodCallHandler
            }
            try {
                val active = call.argument<Boolean>("active") == true
                if (active) {
                    // Android 12+ rejects new foreground-service starts after
                    // the activity has moved to the background. Flutter also
                    // fences this call, but the native lifecycle is the final
                    // authority for a transition racing the method channel.
                    if (!lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) {
                        result.success(false)
                        return@setMethodCallHandler
                    }
                    VoiceForegroundService.start(
                        this,
                        microphone = call.argument<Boolean>("microphone") == true,
                        screenShare = call.argument<Boolean>("screenShare") == true,
                    )
                } else {
                    VoiceForegroundService.stop(this)
                }
                result.success(true)
            } catch (error: Exception) {
                result.error(
                    "VOICE_FOREGROUND_SERVICE_FAILED",
                    error.message ?: "Could not update the Android voice service.",
                    null,
                )
            }
        }

        systemCallChannel = MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "chat.kaede.mobile/system_calls",
        )
        KaedeConnectionService.attach { action, callId ->
            runOnUiThread {
                systemCallChannel.invokeMethod(action, mapOf("callId" to callId))
            }
        }
        systemCallChannel.setMethodCallHandler { call, result ->
            val callId = call.argument<String>("callId")
            if (callId.isNullOrBlank()) {
                result.error("INVALID_CALL", "A call identifier is required.", null)
                return@setMethodCallHandler
            }
            try {
                when (call.method) {
                    "showIncoming" -> {
                        showIncomingSystemCall(
                            callId,
                            call.argument<String>("callerName") ?: "Kaede caller",
                        )
                        result.success(null)
                    }
                    "setActive" -> {
                        KaedeConnectionService.setActive(callId)
                        result.success(null)
                    }
                    "end" -> {
                        KaedeConnectionService.end(callId)
                        result.success(null)
                    }
                    else -> result.notImplemented()
                }
            } catch (error: Exception) {
                result.error(
                    "SYSTEM_CALL_FAILED",
                    error.message ?: "Could not update the Android system call.",
                    null,
                )
            }
        }
    }

    override fun onDestroy() {
        KaedeConnectionService.attach(null)
        super.onDestroy()
    }

    private fun showIncomingSystemCall(callId: String, callerName: String) {
        val telecom = getSystemService(TelecomManager::class.java)
        val account = PhoneAccountHandle(
            ComponentName(this, KaedeConnectionService::class.java),
            "kaede_calls",
        )
        telecom.registerPhoneAccount(
            PhoneAccount.builder(account, "Kaede Chat")
                .setCapabilities(PhoneAccount.CAPABILITY_SELF_MANAGED)
                .build(),
        )
        telecom.addNewIncomingCall(
            account,
            Bundle().apply {
                putString(KaedeConnectionService.EXTRA_CALL_ID, callId)
                putString(KaedeConnectionService.EXTRA_CALLER_NAME, callerName)
            },
        )
    }
}
