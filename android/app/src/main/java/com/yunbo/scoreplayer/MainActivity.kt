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
        applyStatusBarInset()

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.allowFileAccess = true
        webView.settings.allowContentAccess = true
        webView.settings.allowFileAccessFromFileURLs = true
        webView.settings.allowUniversalAccessFromFileURLs = true
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
            val targetUrl = targetBaseUrl ?: offlineEntryUrl()
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

    class AndroidBridge(private val activity: MainActivity) {
        private val prefs = activity.getSharedPreferences("score_player", Context.MODE_PRIVATE)
        private var lastPickedImageUri: Uri? = null
        @Volatile private var activeBaseUrl: String = activity.cloudBaseUrl
        @Volatile private var internalReachable: Boolean = false
        @Volatile private var cloudReachable: Boolean = false

        fun saveLastPickedImageUri(uri: Uri) {
            lastPickedImageUri = uri
            activity.contentResolver.takePersistableUriPermissionIfPossible(uri)
        }

        fun setActiveBaseUrl(baseUrl: String, internalReachable: Boolean, cloudReachable: Boolean) {
            activeBaseUrl = baseUrl
            this.internalReachable = internalReachable
            this.cloudReachable = cloudReachable
        }

        @JavascriptInterface
        fun getActiveBaseUrl(): String = activeBaseUrl

        @JavascriptInterface
        fun isInternalNetworkReachable(): Boolean = internalReachable

        @JavascriptInterface
        fun isCloudEndpointReachable(): Boolean = cloudReachable

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
        fun isBiometricLoginAvailable(): Boolean {
            val hasToken = getToken().isNotBlank()
            if (!hasToken) return false
            val keyguardManager = activity.getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
            return keyguardManager?.isDeviceSecure == true
        }

        @JavascriptInterface
        fun requestBiometricLogin() {
            activity.runOnUiThread {
                val token = getToken()
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
