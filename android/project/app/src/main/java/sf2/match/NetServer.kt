package sf2.match

import android.util.Log
import org.java_websocket.WebSocket
import org.java_websocket.handshake.ClientHandshake
import org.java_websocket.server.WebSocketServer
import org.json.JSONObject
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.random.Random

/** The network server (two players, two devices), the same protocol as server.py's NetworkHandler:
 *  the games: /sf2-lan?role=host -> seat 0 (LEFT), role=join -> seat 1 (RIGHT); what a seat sends goes to
 *  the other seat. The menus before the games: role=lobby-host / lobby-join (the host's "setup" or "cfg",
 *  the joiner's "pick"), each to the other menu; the host's last one is kept for a joiner who comes later.
 *  A connection that reconnects replaces the old one. */
class NetServer(port: Int) : WebSocketServer(InetSocketAddress(port)) {
    private val seats = arrayOfNulls<WebSocket>(2)
    private val lobby = arrayOfNulls<WebSocket>(2)
    private var lobbyLast: String? = null
    private var seed = 1
    private val started = CountDownLatch(1)
    private var failure: Exception? = null

    init { isReuseAddr = true; isTcpNoDelay = true }

    override fun onStart() { started.countDown() }

    override fun onOpen(conn: WebSocket, hs: ClientHandshake): Unit = synchronized(this) {
        val role = Regex("role=([\\w-]+)").find(hs.resourceDescriptor)?.groupValues?.get(1) ?: "host"
        if (role.startsWith("lobby")) {
            val i = if (role == "lobby-join") 1 else 0
            val old = lobby[i]; lobby[i] = conn; old?.close()
            if (i == 1) lobbyLast?.let { conn.send(it) }
            return@synchronized
        }
        val seat = if (role == "join") 1 else 0
        if (seats[0] == null && seats[1] == null) seed = Random.nextInt(1, Int.MAX_VALUE - 1)
        val old = seats[seat]
        seats[seat] = conn
        old?.close()
        val other = seats[1 - seat]
        conn.send(JSONObject().put("t", "hello").put("side", seat).put("seed", seed).put("peer", other != null).toString())
        if (other != null && old == null) other.send("""{"t":"peer","on":true}""")
        Log.i("SF2", "[lan] ${conn.remoteSocketAddress} joined as ${if (seat == 0) "LEFT" else "RIGHT"} on ${hs.getFieldValue("User-Agent")}")
    }

    override fun onMessage(conn: WebSocket, msg: String): Unit = synchronized(this) {
        val li = lobby.indexOf(conn)
        if (li >= 0) {                                   // a menu: to the other menu
            if (li == 0) lobbyLast = if (msg.contains("\"t\":\"setup\"")) msg else null   // a later joiner: an open match, not a started one
            lobby[1 - li]?.send(msg); return@synchronized
        }
        val seat = seats.indexOf(conn)
        if (seat >= 0) seats[1 - seat]?.send(msg)
    }

    override fun onClose(conn: WebSocket, code: Int, reason: String?, remote: Boolean): Unit = synchronized(this) {
        val li = lobby.indexOf(conn)
        if (li >= 0) { lobby[li] = null; return@synchronized }
        val seat = seats.indexOf(conn)
        if (seat >= 0) {
            seats[seat] = null
            seats[1 - seat]?.send("""{"t":"peer","on":false}""")
        }
    }

    override fun onError(conn: WebSocket?, ex: Exception) {
        Log.w("SF2", "network server: $ex")
        if (conn == null) { failure = ex; started.countDown() }   // the server itself (e.g. the port is taken)
    }

    companion object {
        const val DEFAULT_PORT = 8081
        private var server: NetServer? = null
        private var port = 0

        /** open on [p] (another port closes the open one); throws when the port can't be opened */
        @Synchronized fun start(p: Int) {
            if (server != null && port == p) return
            stop()
            val s = NetServer(p)
            s.start()
            if (!s.started.await(5, TimeUnit.SECONDS)) throw Exception("no answer")
            s.failure?.let { throw Exception(it.message ?: it.toString()) }
            server = s; port = p
        }

        @Synchronized fun stop() {
            try { server?.stop(500) } catch (_: Exception) {}
            server = null
        }

        /** this device's address on the local network (Wi-Fi first) */
        fun lanIp(): String {
            val all = NetworkInterface.getNetworkInterfaces()?.toList().orEmpty().filter { it.isUp && !it.isLoopback }
            val sorted = all.sortedBy { if (it.name.startsWith("wlan")) 0 else 1 }
            for (i in sorted) for (a in i.inetAddresses) if (a is Inet4Address && !a.isLoopbackAddress) return a.hostAddress ?: continue
            return "?"
        }
    }
}
