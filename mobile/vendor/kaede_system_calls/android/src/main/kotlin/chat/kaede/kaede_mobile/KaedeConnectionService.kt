package chat.kaede.kaede_mobile

import android.content.Context
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.telecom.Connection
import android.telecom.ConnectionRequest
import android.telecom.ConnectionService
import android.telecom.DisconnectCause
import android.telecom.PhoneAccountHandle
import android.telecom.TelecomManager
import java.util.concurrent.ConcurrentHashMap

class KaedeConnectionService : ConnectionService() {
    companion object {
        const val EXTRA_CALL_ID = "chat.kaede.mobile.call.ID"
        const val EXTRA_CALLER_NAME = "chat.kaede.mobile.call.CALLER"

        private val pendingCalls = mutableSetOf<String>()
        private val handler = Handler(Looper.getMainLooper())
        private val expiry = mutableMapOf<String, Runnable>()
        private val connections = ConcurrentHashMap<String, KaedeConnection>()
        fun begin(context: Context, callId: String): Boolean {
            if (connections.containsKey(callId) || !pendingCalls.add(callId)) return false
            val timeout = Runnable {
                end(context, callId)
            }
            expiry[callId] = timeout
            handler.postDelayed(timeout, 60_000)
            return true
        }

        fun setActive(context: Context, callId: String) {
            CallPresentation.hide(context, callId, ended = false)
            expiry.remove(callId)?.let { handler.removeCallbacks(it) }
            connections[callId]?.setActive()
        }

        fun end(context: Context, callId: String) {
            CallPresentation.hide(context, callId, ended = true)
            pendingCalls.remove(callId)
            expiry.remove(callId)?.let { handler.removeCallbacks(it) }
            connections.remove(callId)?.disconnect(DisconnectCause.LOCAL)
        }
    }

    override fun onCreateIncomingConnection(
        connectionManagerPhoneAccount: PhoneAccountHandle?,
        request: ConnectionRequest,
    ): Connection {
        val callId = request.extras.getString(EXTRA_CALL_ID).orEmpty()
        if (callId.isBlank()) return Connection.createFailedConnection(
            DisconnectCause(DisconnectCause.ERROR, "Missing Kaede call identifier"),
        )
        // end() removes the persisted ring, including when Telecom is still
        // creating this connection. A recreated process can recover a live ring.
        val detail = CallPresentation.details(applicationContext, callId)
        if (detail == null || (!detail.optBoolean("active") &&
            detail.getLong("expires") <= System.currentTimeMillis())) {
            return Connection.createFailedConnection(DisconnectCause(DisconnectCause.CANCELED))
        }
        if (!pendingCalls.remove(callId) && !detail.optBoolean("active")) {
            if (!begin(applicationContext, callId)) {
                return Connection.createFailedConnection(DisconnectCause(DisconnectCause.CANCELED))
            }
            pendingCalls.remove(callId)
        }
        val callerName = request.extras.getString(EXTRA_CALLER_NAME) ?: "Kaede caller"
        return KaedeConnection(applicationContext, callId).apply {
            setAddress(Uri.fromParts("kaede", callId, null), TelecomManager.PRESENTATION_RESTRICTED)
            setCallerDisplayName(callerName, TelecomManager.PRESENTATION_ALLOWED)
            connectionProperties = Connection.PROPERTY_SELF_MANAGED
            setAudioModeIsVoip(true)
            if (detail.optBoolean("active")) setActive() else setRinging()
            connections[callId] = this
        }
    }

    override fun onCreateIncomingConnectionFailed(
        connectionManagerPhoneAccount: PhoneAccountHandle?,
        request: ConnectionRequest,
    ) {
        request.extras.getString(EXTRA_CALL_ID)?.let {
            end(applicationContext, it)
        }
    }
}

private class KaedeConnection(private val context: Context, private val callId: String) : Connection() {
    override fun onShowIncomingCallUi() = CallPresentation.show(context, callId)
    override fun onAnswer() = CallPresentation.action(context, callId, "answer")
    override fun onReject() = CallPresentation.action(context, callId, "decline")
    override fun onDisconnect() = CallPresentation.action(context, callId,
        if (state == STATE_ACTIVE) "ended" else "decline")

    override fun onAbort() = onDisconnect()

    fun disconnect(cause: Int) {
        setDisconnected(DisconnectCause(cause))
        destroy()
    }
}
