package chat.kaede.kaede_mobile

import androidx.lifecycle.Lifecycle
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.embedding.android.FlutterFragmentActivity
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterFragmentActivity() {
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
    }
}
