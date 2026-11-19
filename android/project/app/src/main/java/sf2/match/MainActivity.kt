package sf2.match

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.widget.Toast
import android.util.Log
import android.view.View
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.webkit.WebViewAssetLoader
import org.json.JSONObject

/** The game in a WebView, its files from the APK (assets/www, through WebViewAssetLoader: no server).
 *  The menu reaches the app through window.SF2Native: host(port) opens the network server. */
class MainActivity : Activity() {
    private lateinit var web: WebView
    private var game: GameServer? = null                       // "Share the game" (other devices' browsers)
    private var picked: ValueCallback<Array<Uri>>? = null      // a page's <input type=file> waiting for a file
    private var saving: String? = null                         // a file the page saves, waiting for its place
    private var wifi: WifiManager.WifiLock? = null             // while this device serves the network: no Wi-Fi power saving

    /** Android's Wi-Fi power saving holds incoming packets back (tens to hundreds of ms) on a device that
     *  sends little - the one that only serves the others (Share). A Wi-Fi lock keeps the radio awake */
    private fun lowLatency(on: Boolean) {
        if (!on) { wifi?.let { if (it.isHeld) it.release() }; return }
        if (wifi == null) {
            @Suppress("DEPRECATION")
            val mode = if (Build.VERSION.SDK_INT >= 29) WifiManager.WIFI_MODE_FULL_LOW_LATENCY else WifiManager.WIFI_MODE_FULL_HIGH_PERF
            wifi = (applicationContext.getSystemService(WIFI_SERVICE) as WifiManager).createWifiLock(mode, "sf2:network")
        }
        wifi?.let { if (!it.isHeld) it.acquire() }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val assets = WebViewAssetLoader.Builder()
            .setDomain(DOMAIN).setHttpAllowed(true)       // http: the page may open ws:// to the other device
            .addPathHandler("/", WebViewAssetLoader.AssetsPathHandler(this))
            .build()
        web = WebView(this)
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true                         // the game's save, the menu's match
            mediaPlaybackRequiresUserGesture = false
            allowFileAccess = false
            allowContentAccess = false
        }
        web.webViewClient = object : WebViewClient() {
            override fun shouldInterceptRequest(v: WebView, r: WebResourceRequest): WebResourceResponse? =
                assets.shouldInterceptRequest(r.url)
        }
        // the normal game's save import (<input type=file>): the system's file picker
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(v: WebView, cb: ValueCallback<Array<Uri>>, p: FileChooserParams): Boolean {
                picked?.onReceiveValue(null)
                picked = cb
                startActivityForResult(Intent(Intent.ACTION_GET_CONTENT).addCategory(Intent.CATEGORY_OPENABLE)
                    .setType("*/*"), REQ_OPEN)
                return true
            }
        }
        web.addJavascriptInterface(Bridge(), "SF2Native")
        setContentView(web)
        immersive()
        web.loadUrl("http://$DOMAIN/www/index.html")
    }

    private fun immersive() {
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_FULLSCREEN
            or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
            or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) immersive()
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(req: Int, res: Int, data: Intent?) {
        super.onActivityResult(req, res, data)
        val uri = if (res == RESULT_OK) data?.data else null
        when (req) {
            REQ_OPEN -> { picked?.onReceiveValue(uri?.let { arrayOf(it) }); picked = null }
            REQ_SAVE -> {
                val text = saving; saving = null
                if (uri != null && text != null) try {
                    contentResolver.openOutputStream(uri)?.use { it.write(text.toByteArray()) }
                    Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
                } catch (e: Exception) {
                    Toast.makeText(this, "Not saved: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    override fun onPause() { super.onPause(); web.onPause() }
    override fun onResume() { super.onResume(); web.onResume() }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {                           // back: out of Try, else the match menu
        web.evaluateJavascript("window.__sf2Back&&__sf2Back()||(location.href=location.pathname+'?menu')", null)
    }

    override fun onDestroy() { lowLatency(false); NetServer.stop(); game?.stop(); web.destroy(); super.onDestroy() }

    inner class Bridge {
        /** open the network server on [port] -> {"port", "lan": "ip:port"} or {"error"} */
        @JavascriptInterface
        fun host(port: Int): String {
            val p = if (port > 0) port else NetServer.DEFAULT_PORT
            return try {
                NetServer.start(p)
                runOnUiThread { lowLatency(true) }
                JSONObject().put("port", p).put("lan", "${NetServer.lanIp()}:$p").toString()
            } catch (e: Exception) {
                JSONObject().put("error", "port $p: ${e.message} - choose another").toString()
            }
        }

        /** the host left its match screens: close the network server */
        @JavascriptInterface
        fun unhost() { NetServer.stop(); runOnUiThread { lowLatency(false) } }

        @JavascriptInterface
        fun netport(): Int = NetServer.DEFAULT_PORT

        /** "Share the game": the game for other devices on [port] (and the network server on [netPort])
         *  -> {"url": "http://ip:port/", "lan": "ip:netPort"} or {"error"} */
        @JavascriptInterface
        fun share(port: Int, netPort: Int): String {
            val np = if (netPort > 0) netPort else NetServer.DEFAULT_PORT
            val hp = if (port > 0) port else 8080
            return try {
                NetServer.start(np)
                runOnUiThread { lowLatency(true) }
                if (game?.port != hp) { game?.stop(); game = null; game = GameServer(assets, hp, np) }
                val ip = NetServer.lanIp()
                JSONObject().put("url", "http://$ip:$hp/").put("lan", "$ip:$np").put("port", np).toString()
            } catch (e: Exception) {
                JSONObject().put("error", "port $hp / $np: ${e.message} - choose others").toString()
            }
        }

        /** a file the page saves (the normal game's save export): the system's "save as" picks its place */
        @JavascriptInterface
        fun saveFile(name: String, text: String) {
            runOnUiThread {
                saving = text
                startActivityForResult(Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                    .setType("application/octet-stream").putExtra(Intent.EXTRA_TITLE, name), REQ_SAVE)
            }
        }

        @JavascriptInterface
        fun log(kind: String, msg: String) { Log.i("SF2", "[game-$kind] $msg") }
    }

    companion object {
        const val DOMAIN = "appassets.androidplatform.net"
        const val REQ_OPEN = 1
        const val REQ_SAVE = 2
    }
}
