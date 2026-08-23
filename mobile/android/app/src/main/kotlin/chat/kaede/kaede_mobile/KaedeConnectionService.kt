package chat.kaede.kaede_mobile

import android.net.Uri
import android.os.Bundle
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

        fun setActive(callId: String) = connections[callId]?.setActive()

        fun end(callId: String) {
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
        val callerName = request.extras.getString(EXTRA_CALLER_NAME) ?: "Kaede caller"
        return KaedeConnection(callId).apply {
            setAddress(Uri.fromParts("kaede", callId, null), TelecomManager.PRESENTATION_RESTRICTED)
            setCallerDisplayName(callerName, TelecomManager.PRESENTATION_ALLOWED)
            connectionProperties = Connection.PROPERTY_SELF_MANAGED
            setAudioModeIsVoip(true)
            setRinging()
            connections[callId] = this
        }
    }

    override fun onCreateIncomingConnectionFailed(
        connectionManagerPhoneAccount: PhoneAccountHandle?,
        request: ConnectionRequest,
    ) {
        request.extras.getString(EXTRA_CALL_ID)?.let { emit("decline", it) }
    }
}

private class KaedeConnection(private val callId: String) : Connection() {
    override fun onAnswer() {
        setActive()
        KaedeConnectionService.emit("answer", callId)
    }

    override fun onReject() {
        disconnect(DisconnectCause.REJECTED)
        KaedeConnectionService.emit("decline", callId)
    }

    override fun onDisconnect() {
        disconnect(DisconnectCause.LOCAL)
        KaedeConnectionService.emit("ended", callId)
    }

    fun disconnect(cause: Int) {
        setDisconnected(DisconnectCause(cause))
        destroy()
    }
}
