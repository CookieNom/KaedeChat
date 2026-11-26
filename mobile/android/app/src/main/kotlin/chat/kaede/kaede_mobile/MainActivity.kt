package chat.kaede.kaede_mobile

import android.media.AudioAttributes
import android.media.SoundPool
import io.flutter.FlutterInjector
import android.os.Build
import android.app.PictureInPictureParams
import android.content.res.Configuration
import android.content.pm.PackageManager
import android.util.Log
import android.util.Rational
import androidx.lifecycle.Lifecycle
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.embedding.android.FlutterFragmentActivity
import io.flutter.plugin.common.MethodChannel
import java.io.IOException
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

class MainActivity : FlutterFragmentActivity() {
    private lateinit var pipChannel: MethodChannel
    private var voiceCuePool: SoundPool? = null
    private var autoPip = false
    private var pendingProjectionStart: (() -> Unit)? = null
    private var pendingProjectionResult: MethodChannel.Result? = null

    override fun onPostResume() {
        super.onPostResume()
        val start = pendingProjectionStart
        pendingProjectionStart = null
        pendingProjectionResult = null
        start?.invoke()
    }

    private fun cancelPendingProjection() {
        pendingProjectionStart = null
        pendingProjectionResult?.success(false)
        pendingProjectionResult = null
    }

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
        // Follow the active call route without requesting audio focus or changing speaker mode.
        val pool = SoundPool.Builder().setMaxStreams(3).setAudioAttributes(
            AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
        ).build()
        voiceCuePool = pool
        val loader = FlutterInjector.instance().flutterLoader()
        val cues = listOf("join", "leave").associateWith { name ->
            val key = loader.getLookupKeyForAsset("assets/sounds/$name.wav")
            try {
                assets.openFd(key).use { pool.load(it, 1) }
            } catch (error: IOException) {
                Log.w("KaedeVoice", "Could not load the $name cue", error)
                null
            }
        }
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "chat.kaede.mobile/voice_cues")
            .setMethodCallHandler { call, result ->
                val sound = cues[call.method]
                if (sound == null) result.notImplemented()
                else {
                    pool.play(sound, 1f, 1f, 1, 0, 1f)
                    result.success(null)
                }
            }
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
            if (call.method == "screenCaptureStopped") {
                try { result.success(VoiceForegroundService.screenCaptureStopped()) }
                catch (error: SecurityException) { result.success(false) }
                return@setMethodCallHandler
            }
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
                        if (call.argument<Boolean>("screenShare") == true && !isFinishing) {
                            cancelPendingProjection()
                            pendingProjectionResult = result
                            pendingProjectionStart = {
                                try {
                                    VoiceForegroundService.start(this,
                                        microphone = call.argument<Boolean>("microphone") == true,
                                        screenShare = true, camera = call.argument<Boolean>("camera") == true) { result.success(it) }
                                } catch (error: Exception) {
                                    result.error("VOICE_FOREGROUND_SERVICE_FAILED", error.message, null)
                                }
                            }
                        } else result.success(false)
                        return@setMethodCallHandler
                    }
                    cancelPendingProjection()
                    VoiceForegroundService.start(
                        this,
                        microphone = call.argument<Boolean>("microphone") == true,
                        camera = call.argument<Boolean>("camera") == true,
                        screenShare = call.argument<Boolean>("screenShare") == true,
                    ) { result.success(it) }
                    return@setMethodCallHandler
                } else {
                    cancelPendingProjection()
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

    }

    override fun onDestroy() {
        cancelPendingProjection()
        voiceCuePool?.release()
        voiceCuePool = null
        super.onDestroy()
    }
}
