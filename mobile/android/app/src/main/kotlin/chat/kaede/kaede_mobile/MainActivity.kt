package chat.kaede.kaede_mobile

import android.content.ComponentName
import android.os.Bundle
import android.telecom.PhoneAccount
import android.telecom.PhoneAccountHandle
import android.telecom.TelecomManager
import androidx.lifecycle.Lifecycle
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.embedding.android.FlutterFragmentActivity
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterFragmentActivity() {
    private lateinit var systemCallChannel: MethodChannel

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
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
