package sf2.match

import android.content.res.AssetManager
import android.util.Log
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.FileNotFoundException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URLDecoder
import java.util.concurrent.Executors

/** "Share the game": the game files of the APK (assets/www) for other devices' browsers (iPhones too),
 *  like server.py --share: GET files, GET /sf2-netport (the network server's port, for their menu),
 *  POST /sf2-log (their game's errors -> logcat). Plain HTTP/1.1, one request per connection. */
class GameServer(private val assets: AssetManager, port: Int, private val netPort: Int) {
    private val socket = ServerSocket().apply { reuseAddress = true; bind(InetSocketAddress(port)) }
    private val pool = Executors.newFixedThreadPool(8)
    val port: Int = socket.localPort

    init {
        Thread({
            while (!socket.isClosed) {
                val c = try { socket.accept() } catch (_: Exception) { break }
                pool.execute { serve(c) }
            }
        }, "sf2-share").apply { isDaemon = true }.start()
    }

    fun stop() { try { socket.close() } catch (_: Exception) {}; pool.shutdownNow() }

    private fun serve(c: Socket): Unit = c.use {
        try {
            c.soTimeout = 30000
            val inp = BufferedInputStream(c.getInputStream())
            val head = readHead(inp) ?: return@use
            val first = head.lineSequence().first().split(" ")
            if (first.size < 2) return@use
            val method = first[0]
            val path = URLDecoder.decode(first[1].substringBefore('?').substringBefore('#'), "UTF-8")
            val out = c.getOutputStream()
            when {
                method == "POST" && path == "/sf2-log" -> {
                    val n = Regex("(?im)^content-length:\\s*(\\d+)").find(head)?.groupValues?.get(1)?.toInt() ?: 0
                    val body = ByteArray(minOf(n, 20000)).also { var r = 0; while (r < it.size) { val k = inp.read(it, r, it.size - r); if (k < 0) break; r += k } }
                    Log.i("SF2", "[game from ${c.inetAddress.hostAddress}] ${String(body)}")
                    reply(out, 204, "text/plain", ByteArray(0))
                }
                method == "GET" && path == "/sf2-netport" ->
                    reply(out, 200, "application/json", "{\"port\":$netPort}".toByteArray())
                method == "GET" || method == "HEAD" -> file(out, if (path.endsWith("/")) path + "index.html" else path, method == "HEAD")
                else -> reply(out, 405, "text/plain", "method not allowed".toByteArray())
            }
        } catch (e: Exception) {
            Log.w("SF2", "share: $e")
        }
    }

    private fun file(out: OutputStream, path: String, headOnly: Boolean) {
        if (path.contains("..")) return reply(out, 404, "text/plain", "not found".toByteArray())
        val name = "www" + path
        var len = -1L
        try { assets.openFd(name).use { len = it.length } } catch (_: Exception) {}   // stored (uncompressed): its size
        val stream: InputStream = try { assets.open(name, AssetManager.ACCESS_STREAMING) } catch (_: FileNotFoundException) {
            return reply(out, 404, "text/plain", "not found".toByteArray())
        }
        stream.use {
            val data: ByteArray? = if (len < 0) it.readBytes() else null              // compressed: read it to know its size
            val size = data?.size?.toLong() ?: len
            out.write(("HTTP/1.1 200 OK\r\nContent-Type: ${mime(path)}\r\nContent-Length: $size\r\n" +
                "Cache-Control: no-store\r\nConnection: close\r\n\r\n").toByteArray())
            if (!headOnly) { if (data != null) out.write(data) else it.copyTo(out, 65536) }
            out.flush()
        }
    }

    private fun reply(out: OutputStream, code: Int, type: String, body: ByteArray) {
        out.write(("HTTP/1.1 $code ${if (code < 300) "OK" else "Error"}\r\nContent-Type: $type\r\n" +
            "Content-Length: ${body.size}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n").toByteArray())
        out.write(body); out.flush()
    }

    private fun readHead(inp: InputStream): String? {
        val b = ByteArrayOutputStream()
        var last = 0
        while (b.size() < 16384) {
            val x = inp.read(); if (x < 0) return null
            b.write(x); last = (last shl 8) or x
            if (last == 0x0d0a0d0a) return b.toString("ISO-8859-1")
        }
        return null
    }

    companion object {
        private val TYPES = mapOf(
            "html" to "text/html; charset=utf-8", "js" to "text/javascript; charset=utf-8", "json" to "application/json",
            "css" to "text/css", "webmanifest" to "application/manifest+json", "xml" to "application/xml",
            "png" to "image/png", "jpg" to "image/jpeg", "webp" to "image/webp", "avif" to "image/avif", "ico" to "image/x-icon",
            "svg" to "image/svg+xml", "m4a" to "audio/mp4", "mp4" to "video/mp4", "wasm" to "application/wasm")
        fun mime(path: String) = TYPES[path.substringAfterLast('.', "").lowercase()] ?: "application/octet-stream"
    }
}
