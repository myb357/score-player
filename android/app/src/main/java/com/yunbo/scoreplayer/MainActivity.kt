package com.yunbo.scoreplayer

import android.annotation.SuppressLint
import android.app.Activity
import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.hardware.biometrics.BiometricPrompt
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.CancellationSignal
import android.util.Base64
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
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
    private val cloudBaseUrl = "https://score-player.onrender.com"
    private val externalBaseUrl = "https://scoreplayer-myb.top"
    private val offlineHomeUrl = "file:///android_asset/home.html"
    private val offlineLoginUrl = "file:///android_asset/login.html"
    private val probeTimeoutMillis = 2_000
    private val imagePickRequestCode = 1001
    private val fileChooserRequestCode = 1002
    private val mainScope = CoroutineScope(Dispatchers.Main + Job())

    private var fileChooserCallback: android.webkit.ValueCallback<Array<Uri>>? = null

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
        applyStatusBarInset()

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.allowFileAccess = true
        webView.settings.allowContentAccess = true
        webView.settings.allowFileAccessFromFileURLs = true
        webView.settings.allowUniversalAccessFromFileURLs = true
        webView.settings.cacheMode = WebSettings.LOAD_DEFAULT
        webView.webViewClient = object : WebViewClient() {
            override fun shouldInterceptRequest(view: WebView?, request: WebResourceRequest?): WebResourceResponse? {
                return request?.url?.let { offlineAssetResponse(it) }
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                view?.evaluateJavascript(
                    "document.documentElement.classList.add('android-app');document.body&&document.body.classList.add('android-app-body');",
                    null,
                )
            }
        }
        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                webView: WebView?,
                filePathCallback: android.webkit.ValueCallback<Array<Uri>>?,
                fileChooserParams: FileChooserParams?,
            ): Boolean {
                return this@MainActivity.openWebFileChooser(filePathCallback, fileChooserParams)
            }
        }
        webView.addJavascriptInterface(bridge, "AndroidBridge")

        probeAndLoad(forceReload = true)
    }

    override fun onStart() {
        super.onStart()
        if (::webView.isInitialized) {
            probeAndLoad(forceReload = false)
        }
    }

    override fun onBackPressed() {
        if (::webView.isInitialized && webView.canGoBack()) {
            webView.goBack()
            return
        }
        super.onBackPressed()
    }

    override fun onDestroy() {
        mainScope.cancel()
        super.onDestroy()
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == imagePickRequestCode && resultCode == Activity.RESULT_OK) {
            data?.data?.let { bridge.savePickedImageUris(listOf(it)) }
        }
        if (requestCode == fileChooserRequestCode) {
            val result = if (resultCode == Activity.RESULT_OK) {
                val uris = mutableListOf<Uri>()
                data?.clipData?.let { clip ->
                    for (i in 0 until clip.itemCount) {
                        clip.getItemAt(i)?.uri?.let { uri ->
                            contentResolver.takePersistableUriPermissionIfPossible(uri)
                            uris.add(uri)
                        }
                    }
                }
                data?.data?.let { uri ->
                    contentResolver.takePersistableUriPermissionIfPossible(uri)
                    uris.add(uri)
                }
                bridge.savePickedImageUris(uris)
                uris.toTypedArray()
            } else {
                null
            }
            fileChooserCallback?.onReceiveValue(result)
            fileChooserCallback = null
        }
    }

    private fun applyStatusBarInset() {
        val statusBarId = resources.getIdentifier("status_bar_height", "dimen", "android")
        val statusBarHeight = if (statusBarId > 0) resources.getDimensionPixelSize(statusBarId) else 0
        val extraTopPadding = (12 * resources.displayMetrics.density).toInt()
        webView.setPadding(0, statusBarHeight + extraTopPadding, 0, 0)
        webView.clipToPadding = false
    }

    private fun probeAndLoad(forceReload: Boolean) {
        if (probing) return
        probing = true

        mainScope.launch {
            val targetBaseUrl = withContext(Dispatchers.IO) { selectReachableBaseUrl() }
            val targetUrl = when {
                bridge.shouldRequireBiometricGate() -> {
                    val base = targetBaseUrl ?: bridge.getLastBaseUrl().ifBlank { cloudBaseUrl }
                    "$base/login"
                }
                else -> targetBaseUrl ?: offlineHttpEntryUrl()
            }
            bridge.setActiveBaseUrl(
                baseUrl = targetBaseUrl ?: "",
                internalReachable = targetBaseUrl == internalBaseUrl,
                cloudReachable = targetBaseUrl == cloudBaseUrl,
            )

            val shouldLoad = forceReload || currentBaseUrl == null || currentBaseUrl != targetUrl
            currentBaseUrl = targetUrl
            probing = false

            if (shouldLoad) {
                webView.loadUrl(targetUrl)
            }
        }
    }

    private fun selectReachableBaseUrl(): String? {
        return when {
            probeUrl(internalBaseUrl) -> internalBaseUrl
            probeUrl(cloudBaseUrl) -> cloudBaseUrl
            probeUrl(externalBaseUrl) -> externalBaseUrl
            else -> null
        }
    }

    private fun offlineEntryUrl(): String {
        return offlineLoginUrl
    }

    private fun offlineHttpEntryUrl(): String {
        val lastBaseUrl = bridge.getLastBaseUrl().ifBlank { cloudBaseUrl }
        return if (bridge.shouldRequireBiometricGate()) "$lastBaseUrl/login" else lastBaseUrl
    }

    private fun offlineAssetResponse(uri: Uri): WebResourceResponse? {
        if (!bridge.isOfflineMode()) return null
        val host = uri.host ?: return null
        if (host !in listOf("192.168.1.2", "score-player.onrender.com", "scoreplayer-myb.top")) return null
        val path = uri.path ?: "/"
        val assetName = when {
            path == "/" || path == "" -> "home.html"
            path == "/login" -> "login.html"
            path == "/new" -> "new.html"
            path == "/users" -> "users.html"
            path == "/downloads" -> "downloads.html"
            path.startsWith("/player/") -> "player.html"
            path == "/static/style.css" || path == "/style.css" -> "style.css"
            path == "/static/sw.js" || path == "/sw.js" -> "sw.js"
            path == "/static/manifest.json" || path == "/manifest.json" -> "manifest.json"
            else -> null
        } ?: return null
        val mimeType = when {
            assetName.endsWith(".html") -> "text/html"
            assetName.endsWith(".css") -> "text/css"
            assetName.endsWith(".js") -> "application/javascript"
            assetName.endsWith(".json") -> "application/json"
            else -> "text/plain"
        }
        return try {
            WebResourceResponse(mimeType, "UTF-8", assets.open(assetName))
        } catch (_: Exception) {
            null
        }
    }

    private fun probeUrl(baseUrl: String): Boolean {
        var connection: HttpURLConnection? = null
        return try {
            connection = (URL(baseUrl).openConnection() as HttpURLConnection).apply {
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

    private fun openWebFileChooser(
        callback: android.webkit.ValueCallback<Array<Uri>>?,
        params: WebChromeClient.FileChooserParams?,
    ): Boolean {
        fileChooserCallback?.onReceiveValue(null)
        fileChooserCallback = callback
        val intent = try {
            params?.createIntent() ?: Intent(Intent.ACTION_OPEN_DOCUMENT)
        } catch (_: Exception) {
            Intent(Intent.ACTION_OPEN_DOCUMENT)
        }.apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            addFlags(Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
            putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params?.mode == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE)
            val acceptTypes = params?.acceptTypes?.filter { it.isNotBlank() }?.toTypedArray().orEmpty()
            if (acceptTypes.isNotEmpty()) putExtra(Intent.EXTRA_MIME_TYPES, acceptTypes)
            if (type.isNullOrBlank() || type == "*/*") {
                type = if (acceptTypes.isNotEmpty()) acceptTypes.first() else "*/*"
            }
        }
        return try {
            startActivityForResult(intent, fileChooserRequestCode)
            true
        } catch (_: Exception) {
            fileChooserCallback = null
            callback?.onReceiveValue(null)
            false
        }
    }

    class AndroidBridge(private val activity: MainActivity) {
        private val prefs = activity.getSharedPreferences("score_player", Context.MODE_PRIVATE)
        private var lastPickedImageUri: Uri? = null
        private var pickedImageUris: List<Uri> = emptyList()
        @Volatile private var activeBaseUrl: String = prefs.getString("last_base_url", activity.cloudBaseUrl) ?: activity.cloudBaseUrl
        @Volatile private var internalReachable: Boolean = false
        @Volatile private var cloudReachable: Boolean = false
        @Volatile private var offlineMode: Boolean = false
        @Volatile private var biometricUnlocked: Boolean = false

        fun savePickedImageUris(uris: List<Uri>) {
            val imageUris = uris.filter { uri ->
                val mime = activity.contentResolver.getType(uri) ?: ""
                mime.isBlank() || mime.startsWith("image/")
            }
            pickedImageUris = imageUris
            lastPickedImageUri = imageUris.lastOrNull()
            imageUris.forEach { uri ->
                activity.contentResolver.takePersistableUriPermissionIfPossible(uri)
            }
        }

        fun saveLastPickedImageUri(uri: Uri) {
            savePickedImageUris(listOf(uri))
        }

        fun setActiveBaseUrl(baseUrl: String, internalReachable: Boolean, cloudReachable: Boolean) {
            offlineMode = baseUrl.isBlank()
            if (baseUrl.isNotBlank()) {
                activeBaseUrl = baseUrl
                prefs.edit().putString("last_base_url", baseUrl).apply()
            }
            this.internalReachable = internalReachable
            this.cloudReachable = cloudReachable
        }

        fun getLastBaseUrl(): String = prefs.getString("last_base_url", activity.cloudBaseUrl) ?: activity.cloudBaseUrl

        fun isOfflineMode(): Boolean = offlineMode

        fun hasBiometricCredential(): Boolean = prefs.getString("biometric_token", "").orEmpty().isNotBlank()

        fun shouldRequireBiometricGate(): Boolean = hasBiometricCredential() && !biometricUnlocked

        fun isBiometricUnlocked(): Boolean = biometricUnlocked

        @JavascriptInterface
        fun getActiveBaseUrl(): String = activeBaseUrl

        @JavascriptInterface
        fun isInternalNetworkReachable(): Boolean = internalReachable

        @JavascriptInterface
        fun isCloudEndpointReachable(): Boolean = cloudReachable

        @JavascriptInterface
        fun saveToken(token: String) {
            prefs.edit()
                .putString("token", token)
                .putString("biometric_token", token)
                .apply()
        }

        @JavascriptInterface
        fun getToken(): String = prefs.getString("token", "") ?: ""

        private fun getBiometricToken(): String = prefs.getString("biometric_token", "") ?: ""

        @JavascriptInterface
        fun clearToken() {
            prefs.edit().remove("token").apply()
            biometricUnlocked = false
        }

        @JavascriptInterface
        fun isBiometricLoginAvailable(): Boolean {
            val hasToken = getBiometricToken().isNotBlank()
            if (!hasToken) return false
            val keyguardManager = activity.getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
            return keyguardManager?.isDeviceSecure == true
        }

        @JavascriptInterface
        fun requestBiometricLogin() {
            activity.runOnUiThread {
                val token = getBiometricToken()
                if (token.isBlank()) {
                    dispatchBiometricResult(false, "未找到已保存的登录状态", "")
                    return@runOnUiThread
                }
                if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) {
                    dispatchBiometricResult(false, "当前系统版本不支持指纹登录", "")
                    return@runOnUiThread
                }
                val keyguardManager = activity.getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
                if (keyguardManager?.isDeviceSecure != true) {
                    dispatchBiometricResult(false, "请先在系统中设置指纹或锁屏密码", "")
                    return@runOnUiThread
                }

                val prompt = BiometricPrompt.Builder(activity)
                    .setTitle("指纹登录")
                    .setSubtitle("验证后进入谱子播放器")
                    .setNegativeButton("使用密码登录", activity.mainExecutor) { _, _ ->
                        dispatchBiometricResult(false, "已切换为密码登录", "")
                    }
                    .build()

                prompt.authenticate(
                    CancellationSignal(),
                    activity.mainExecutor,
                    object : BiometricPrompt.AuthenticationCallback() {
                        override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult?) {
                            biometricUnlocked = true
                            prefs.edit().putString("token", token).apply()
                            dispatchBiometricResult(true, "指纹验证成功", token)
                        }

                        override fun onAuthenticationError(errorCode: Int, errString: CharSequence?) {
                            dispatchBiometricResult(false, errString?.toString() ?: "指纹验证失败", "")
                        }

                        override fun onAuthenticationFailed() {
                            dispatchBiometricResult(false, "指纹不匹配，请重试", "")
                        }
                    },
                )
            }
        }

        private fun dispatchBiometricResult(success: Boolean, message: String, token: String) {
            val safeMessage = message.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")
            val safeToken = token.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "")
            activity.runOnUiThread {
                activity.webView.evaluateJavascript(
                    "window.onBiometricAuthResult && window.onBiometricAuthResult($success, '$safeMessage', '$safeToken')",
                    null,
                )
            }
        }

        @JavascriptInterface
        fun openHomeAfterBiometric() {
            biometricUnlocked = true
            val token = getBiometricToken()
            if (token.isNotBlank()) {
                prefs.edit().putString("token", token).apply()
            }
            val target = getLastBaseUrl().ifBlank { activity.cloudBaseUrl }
            activity.currentBaseUrl = target
            activity.runOnUiThread {
                activity.webView.loadUrl(target)
            }
        }

        @JavascriptInterface
        fun pickImage() {
            activity.runOnUiThread { activity.openImagePicker() }
        }

        private fun readImageUriAsBase64(uri: Uri): String {
            return try {
                val mime = activity.contentResolver.getType(uri) ?: "image/jpeg"
                if (!mime.startsWith("image/")) return ""
                activity.contentResolver.openInputStream(uri)?.use { input ->
                    val bytes = input.readBytes()
                    if (bytes.isEmpty()) return ""
                    "data:$mime;base64," + Base64.encodeToString(bytes, Base64.NO_WRAP)
                } ?: ""
            } catch (_: Exception) {
                ""
            }
        }

        @JavascriptInterface
        fun readPickedImageAsBase64(index: Int): String {
            val uri = pickedImageUris.getOrNull(index) ?: return ""
            return readImageUriAsBase64(uri)
        }

        @JavascriptInterface
        fun pickedImageCount(): Int {
            return pickedImageUris.size
        }

        @JavascriptInterface
        fun readLastPickedImageAsBase64(): String {
            val uri = lastPickedImageUri ?: return ""
            return readImageUriAsBase64(uri)
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
