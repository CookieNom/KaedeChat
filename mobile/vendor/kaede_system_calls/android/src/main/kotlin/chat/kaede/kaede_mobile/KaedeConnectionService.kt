package chat.kaede.kaede_mobile

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
        @Volatile private var eventSink: ((String, String) -> Unit)? = null
        private val pendingEvents = mutableListOf<Pair<String, String>>()

        fun attach(sink: ((String, String) -> Unit)?) {
            synchronized(pendingEvents) {
                eventSink = sink
                if (sink != null) {
                    pendingEvents.forEach { sink(it.first, it.second) }
                    pendingEvents.clear()
                }
            }
        }

        fun emit(action: String, callId: String) {
            synchronized(pendingEvents) {
                val sink = eventSink
                if (sink == null) pendingEvents.add(action to callId) else sink(action, callId)
            }
        }

        fun begin(callId: String): Boolean {
            if (connections.containsKey(callId) || !pendingCalls.add(callId)) return false
            val timeout = Runnable {
                end(callId)
                emit("ended", callId)
            }
            expiry[callId] = timeout
            handler.postDelayed(timeout, 60_000)
            return true
        }

        fun setActive(callId: String) {
            expiry.remove(callId)?.let { handler.removeCallbacks(it) }
            connections[callId]?.setActive()
        }

        fun end(callId: String) {
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
        // end() can arrive while Telecom is still creating the connection.
        if (!pendingCalls.remove(callId)) return Connection.createFailedConnection(
            DisconnectCause(DisconnectCause.CANCELED),
        )
        val callerName = request.extras.getString(EXTRA_CALLER_NAME) ?: "Kaede caller"
        return KaedeConnection(callId).apply {
            setAddress(Uri.fromParts("kaede", callId, null), TelecomManager.PRESENTATION_RESTRICTED)
            setCallerDisplayName(callerName, TelecomManager.PRESENTATION_ALLOWED)
            connectionProperties = Connection.PROPERTY_SELF_MANAGED
            setAudioModeIsVoip(true)
            if (expiry.containsKey(callId)) setRinging() else setActive()
            connections[callId] = this
        }
    }

    override fun onCreateIncomingConnectionFailed(
        connectionManagerPhoneAccount: PhoneAccountHandle?,
        request: ConnectionRequest,
    ) {
        request.extras.getString(EXTRA_CALL_ID)?.let {
            end(it)
            emit("decline", it)
        }
    }
}

private class KaedeConnection(private val callId: String) : Connection() {
    override fun onAnswer() {
        // The controller marks the call active after the home accepts it.
        KaedeConnectionService.emit("answer", callId)
    }

    override fun onReject() {
        KaedeConnectionService.end(callId)
        KaedeConnectionService.emit("decline", callId)
    }

    override fun onDisconnect() {
        KaedeConnectionService.end(callId)
        KaedeConnectionService.emit("ended", callId)
    }

    override fun onAbort() = onDisconnect()

    fun disconnect(cause: Int) {
        setDisconnected(DisconnectCause(cause))
        destroy()
    }
}
