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
                val context = binding.applicationContext
                if (call.method == "ready" || call.method == "reset") {
                    if (call.method == "ready") CallPresentation.ready(context, call.argument<String>("accountKey")!!, this)
                    else CallPresentation.reset(context)
                    result.success(null)
                    return@setMethodCallHandler
                }
                val callId = call.argument<String>("callId")
                if (callId.isNullOrBlank()) {
                    result.error("INVALID_CALL", "A call identifier is required.", null)
                    return@setMethodCallHandler
                }
                try {
                    when (call.method) {
                        "showIncoming" -> showIncoming(
                            context,
                            callId,
                            call.argument<String>("callerName") ?: "Kaede caller",
                            call.argument<String>("channelRef")!!, call.argument<String>("accountKey")!!,
                        )
                        "deferDecline" -> {
                            if (CallPresentation.details(context, callId) == null) {
                                CallPresentation.remember(context, callId, call.argument<String>("callerName") ?: "Kaede caller",
                                    call.argument<String>("channelRef")!!, call.argument<String>("accountKey")!!)
                            }
                            CallPresentation.action(context, callId, "decline")
                        }
                        "setActive" -> KaedeConnectionService.setActive(context, callId)
                        "end" -> KaedeConnectionService.end(context, callId)
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
        CallPresentation.detach(channel)
        channel?.setMethodCallHandler(null)
        channel = null
    }

    private fun showIncoming(context: Context, callId: String, callerName: String, channelRef: String, accountKey: String) {
        if (!KaedeConnectionService.begin(context, callId)) return
        CallPresentation.remember(context, callId, callerName, channelRef, accountKey)
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            CallPresentation.show(context, callId)
            return
        }
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
        try {
            telecom.addNewIncomingCall(account, Bundle().apply {
                putString(KaedeConnectionService.EXTRA_CALL_ID, callId)
                putString(KaedeConnectionService.EXTRA_CALLER_NAME, callerName)
            })
        } catch (error: Exception) {
            KaedeConnectionService.end(context, callId)
            throw error
        }
    }
}
