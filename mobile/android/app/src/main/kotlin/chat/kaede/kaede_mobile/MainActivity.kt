package chat.kaede.kaede_mobile

import android.os.Build
import android.app.PictureInPictureParams
import android.content.res.Configuration
import android.content.pm.PackageManager
import android.util.Rational
import androidx.lifecycle.Lifecycle
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.embedding.android.FlutterFragmentActivity
import io.flutter.plugin.common.MethodChannel
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

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

        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "chat.kaede.mobile/aead",
        ).setMethodCallHandler { call, result ->
            // AES-256-GCM for the encryption store. The 16-byte tag follows
            // the ciphertext, matching the Dart cipher this stands in for.
            val mode = when (call.method) {
                "seal" -> Cipher.ENCRYPT_MODE
                "open" -> Cipher.DECRYPT_MODE
                else -> {
                    result.notImplemented()
                    return@setMethodCallHandler
                }
            }
            val key = call.argument<ByteArray>("key")
            val nonce = call.argument<ByteArray>("nonce")
            val data = call.argument<ByteArray>("data")
            val aad = call.argument<ByteArray>("aad")
            if (key == null || nonce == null || data == null || aad == null ||
                key.size != 32 || nonce.size != 12
            ) {
                key?.fill(0)
                result.error("AEAD_INVALID", null, null)
                return@setMethodCallHandler
            }
            try {
                val cipher = Cipher.getInstance("AES/GCM/NoPadding")
                cipher.init(mode, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
                cipher.updateAAD(aad)
                result.success(cipher.doFinal(data))
            } catch (_: Exception) {
                // Includes a rejected tag; Dart re-checks with its own cipher.
                result.error("AEAD_FAILED", null, null)
            } finally {
                key.fill(0)
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
    }

    override fun onDestroy() {
        KaedeConnectionService.attach(null)
        super.onDestroy()
    }
}
