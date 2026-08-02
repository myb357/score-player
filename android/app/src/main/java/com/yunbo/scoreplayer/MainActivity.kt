package com.yunbo.scoreplayer

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.util.Base64
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.URL

class MainActivity : Activity() {
    private val internalBaseUrl = "http://192.168.1.2:9000"
    private val externalBaseUrl = "https://scoreplayer-myb.top"
    private val probeTimeoutMillis = 2_000
    private val imagePickRequestCode = 1001
    private val mainScope = CoroutineScope(Dispatchers.Main + Job())

    private lateinit var webView: WebView
    private lateinit var bridge: AndroidBridge
    private var currentBaseUrl: String? = null
    private var probing = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        bridge = AndroidBridge(this)
        webView = WebView(this)
        setContentView(webView)

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.cacheMode = WebSettings.LOAD_DEFAULT
        webView.webViewClient = WebViewClient()
        webView.webChromeClient = WebChromeClient()
        webView.addJavascriptInterface(bridge, "AndroidBridge")

        probeAndLoad(forceReload = true)
    }

    override fun onStart() {
        super.onStart()
        if (::webView.isInitialized) {
            probeAndLoad(forceReload = false)
        }
    }

    override fun onDestroy() {
        mainScope.cancel()
        super.onDestroy()
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == imagePickRequestCode && resultCode == Activity.RESULT_OK) {
            data?.data?.let { bridge.saveLastPickedImageUri(it) }
        }
    }

    private fun probeAndLoad(forceReload: Boolean) {
        if (probing) return
        probing = true

        mainScope.launch {
            val internalReachable = withContext(Dispatchers.IO) { probeInternalUrl() }
            val targetBaseUrl = if (internalReachable) internalBaseUrl else externalBaseUrl
            bridge.setActiveBaseUrl(targetBaseUrl, internalReachable)

            val shouldLoad = forceReload || currentBaseUrl == null || currentBaseUrl != targetBaseUrl
            currentBaseUrl = targetBaseUrl
            probing = false

            if (shouldLoad) {
                webView.loadUrl(targetBaseUrl)
            }
        }
    }

    private fun probeInternalUrl(): Boolean {
        var connection: HttpURLConnection? = null
        return try {
            connection = (URL(internalBaseUrl).openConnection() as HttpURLConnection).apply {
                requestMethod = "HEAD"
                connectTimeout = probeTimeoutMillis
                readTimeout = probeTimeoutMillis
                instanceFollowRedirects = false
            }
            val code = connection.responseCode
            code in 200..399
        } catch (_: Exception) {
            false
        } finally {
            connection?.disconnect()
        }
    }

    fun openImagePicker() {
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "image/*"
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            addFlags(Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
        }
        startActivityForResult(intent, imagePickRequestCode)
    }

    class AndroidBridge(private val activity: MainActivity) {
        private val prefs = activity.getSharedPreferences("score_player", Context.MODE_PRIVATE)
        private var lastPickedImageUri: Uri? = null
        @Volatile private var activeBaseUrl: String = activity.externalBaseUrl
        @Volatile private var internalReachable: Boolean = false

        fun saveLastPickedImageUri(uri: Uri) {
            lastPickedImageUri = uri
            activity.contentResolver.takePersistableUriPermissionIfPossible(uri)
        }

        fun setActiveBaseUrl(baseUrl: String, reachable: Boolean) {
            activeBaseUrl = baseUrl
            internalReachable = reachable
        }

        @JavascriptInterface
        fun getActiveBaseUrl(): String = activeBaseUrl

        @JavascriptInterface
        fun isInternalNetworkReachable(): Boolean = internalReachable

        @JavascriptInterface
        fun saveToken(token: String) {
            prefs.edit().putString("token", token).apply()
        }

        @JavascriptInterface
        fun getToken(): String = prefs.getString("token", "") ?: ""

        @JavascriptInterface
        fun clearToken() {
            prefs.edit().remove("token").apply()
        }

        @JavascriptInterface
        fun pickImage() {
            activity.runOnUiThread { activity.openImagePicker() }
        }

        @JavascriptInterface
        fun readLastPickedImageAsBase64(): String {
            val uri = lastPickedImageUri ?: return ""
            return try {
                val mime = activity.contentResolver.getType(uri) ?: "image/jpeg"
                activity.contentResolver.openInputStream(uri)?.use { input ->
                    val bytes = input.readBytes()
                    "data:$mime;base64," + Base64.encodeToString(bytes, Base64.NO_WRAP)
                } ?: ""
            } catch (_: Exception) {
                ""
            }
        }
    }
}

private fun android.content.ContentResolver.takePersistableUriPermissionIfPossible(uri: Uri) {
    try {
        takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
    } catch (_: Exception) {
        // 部分相册应用不会授予可持久化权限，忽略即可，当前进程内仍可读取。
    }
}
