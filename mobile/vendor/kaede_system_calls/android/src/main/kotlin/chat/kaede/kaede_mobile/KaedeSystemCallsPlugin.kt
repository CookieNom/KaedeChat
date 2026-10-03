package chat.kaede.kaede_mobile

import android.content.ComponentName
import android.content.Context
import android.os.Build
import android.os.Bundle
import android.telecom.PhoneAccount
import android.telecom.PhoneAccountHandle
import android.telecom.TelecomManager
import io.flutter.embedding.engine.plugins.FlutterPlugin
import io.flutter.plugin.common.MethodChannel

/** Registered in every engine, including Firebase's headless engine. */
class KaedeSystemCallsPlugin : FlutterPlugin {
    private var channel: MethodChannel? = null

    override fun onAttachedToEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        channel = MethodChannel(binding.binaryMessenger, "chat.kaede.mobile/system_calls").apply {
            setMethodCallHandler { call, result ->
                val callId = call.argument<String>("callId")
                if (callId.isNullOrBlank()) {
                    result.error("INVALID_CALL", "A call identifier is required.", null)
                    return@setMethodCallHandler
                }
                try {
                    when (call.method) {
                        "showIncoming" -> showIncoming(
                            binding.applicationContext,
                            callId,
                            call.argument<String>("callerName") ?: "Kaede caller",
                        )
                        "setActive" -> KaedeConnectionService.setActive(callId)
                        "end" -> KaedeConnectionService.end(callId)
                        else -> {
                            result.notImplemented()
                            return@setMethodCallHandler
                        }
                    }
                    result.success(null)
                } catch (error: Exception) {
                    result.error("SYSTEM_CALL_FAILED", error.message, null)
                }
            }
        }
    }

    override fun onDetachedFromEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        channel?.setMethodCallHandler(null)
        channel = null
    }

    private fun showIncoming(context: Context, callId: String, callerName: String) {
        // Self-managed Telecom is available from Android 8; older devices use
        // the same call notification without registering a Telecom connection.
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val telecom = context.getSystemService(TelecomManager::class.java)
        val account = PhoneAccountHandle(
            ComponentName(context, KaedeConnectionService::class.java),
            "kaede_calls",
        )
        telecom.registerPhoneAccount(
            PhoneAccount.builder(account, "Kaede Chat")
                .setCapabilities(PhoneAccount.CAPABILITY_SELF_MANAGED)
                .build(),
        )
        if (!KaedeConnectionService.begin(callId)) return
        try {
            telecom.addNewIncomingCall(account, Bundle().apply {
                putString(KaedeConnectionService.EXTRA_CALL_ID, callId)
                putString(KaedeConnectionService.EXTRA_CALLER_NAME, callerName)
            })
        } catch (error: Exception) {
            KaedeConnectionService.end(callId)
            throw error
        }
    }
}
