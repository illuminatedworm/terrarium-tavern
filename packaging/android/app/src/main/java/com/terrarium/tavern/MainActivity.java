package com.terrarium.tavern;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Base64;
import android.view.Gravity;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.webkit.JavaScriptReplyProxy;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.util.Collections;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** The only UI WebView: local main frame, external links in the system browser. */
public final class MainActivity extends Activity implements HostService.Listener {
    private static final int CHOOSE_FILE = 10;
    private static final int SAVE_FILE = 11;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService files = Executors.newSingleThreadExecutor();
    private WebView web;
    private LinearLayout status;
    private TextView statusText;
    private ProgressBar progress;
    private Button retry;
    private HostService host;
    private boolean bound;
    private String origin;
    private String loadedToken;
    private boolean pageFailed;
    private ValueCallback<Uri[]> fileChooser;
    private int nextTransfer;
    private ExportTransfer transfer;
    private JavaScriptReplyProxy saveReply;
    private String saveRequest;

    private static final class ExportTransfer {
        int id;
        File file;
        FileOutputStream stream;
        String name;
        String mime;
        long expected;
        long written;
    }

    private final ServiceConnection connection = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            host = ((HostService.LocalBinder) binder).service();
            host.addListener(MainActivity.this);
        }
        @Override public void onServiceDisconnected(ComponentName name) {
            host = null;
            showStatus("本地服务已断开，请重新启动。", false);
        }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(23, 26, 28));
        // Target 35 enforces edge-to-edge; keep system bars and keyboard outside the web viewport.
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets.consumeSystemWindowInsets();
        });
        web = new WebView(this);
        web.setBackgroundColor(Color.rgb(23, 26, 28));
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        status = new LinearLayout(this);
        status.setOrientation(LinearLayout.VERTICAL);
        status.setGravity(Gravity.CENTER);
        status.setPadding(28, 28, 28, 28);
        status.setBackgroundColor(Color.rgb(23, 26, 28));
        progress = new ProgressBar(this);
        status.addView(progress);
        ScrollView scroll = new ScrollView(this);
        statusText = new TextView(this);
        statusText.setTextColor(Color.rgb(231, 228, 218));
        statusText.setTextSize(16);
        statusText.setTextIsSelectable(true);
        statusText.setPadding(0, 24, 0, 24);
        scroll.addView(statusText);
        status.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
        retry = new Button(this);
        retry.setText("重新启动");
        retry.setOnClickListener(view -> {
            if (host != null) {
                if (host.getPort() > 0) {
                    web.reload();
                    showStatus("正在载入界面…", true);
                } else host.startBackend();
            } else connectHost();
        });
        status.addView(retry);
        root.addView(status, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);
        root.requestApplyInsets();
        configureWebView();
        files.execute(() -> {
            File[] abandoned = getCacheDir().listFiles((directory, name) -> name.startsWith("tavern-export-") && name.endsWith(".tmp"));
            if (abandoned != null) for (File file : abandoned) file.delete();
        });
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            showStatus("Android System WebView 版本过旧，请先在系统应用商店更新后重新打开。", false);
            retry.setVisibility(View.GONE);
            return;
        }
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 12);
        }
        connectHost();
    }

    private void connectHost() {
        showStatus("正在启动本地 Python 服务…\n首次启动需要解压应用资源。", true);
        Intent intent = new Intent(this, HostService.class);
        startForegroundService(intent);
        if (!bound) bound = bindService(intent, connection, Context.BIND_AUTO_CREATE);
    }

    private void configureWebView() {
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true); // SAF URIs returned by the system file picker.
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setSupportMultipleWindows(false);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                if (!request.isForMainFrame()) return false;
                if (isLocal(request.getUrl())) return false;
                openExternal(request.getUrl());
                return true;
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                // Cookies are scoped by host, not port. Even an <img> on another app's
                // loopback port would receive the startup token if subresources escaped.
                // The document CSP also checks redirect targets and WebSockets, which
                // WebViewClient does not consistently expose to this interception API.
                boolean http = "http".equals(uri.getScheme()) || "https".equals(uri.getScheme());
                if ((request.isForMainFrame() || http) && !isLocal(uri)) {
                    return new WebResourceResponse("text/plain", "UTF-8", 403, "Forbidden", Collections.emptyMap(),
                        new java.io.ByteArrayInputStream(new byte[0]));
                }
                return null;
            }
            @Override public void onPageStarted(WebView view, String url, android.graphics.Bitmap icon) {
                pageFailed = false;
                if (!isLocal(Uri.parse(url))) view.stopLoading();
            }
            @Override public void onPageFinished(WebView view, String url) {
                if (!pageFailed && isLocal(Uri.parse(url))) status.setVisibility(View.GONE);
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError failure) {
                if (request.isForMainFrame()) {
                    pageFailed = true;
                    showStatus("无法载入本地界面：" + failure.getDescription(), false);
                }
            }
            @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (request.isForMainFrame()) {
                    pageFailed = true;
                    showStatus("本地界面返回 HTTP " + response.getStatusCode() + "，请重新启动后重试。", false);
                }
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileChooser != null) fileChooser.onReceiveValue(null);
                fileChooser = callback;
                Intent picker = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                picker.addCategory(Intent.CATEGORY_OPENABLE);
                // Document providers may label JSON as text/plain or octet-stream,
                // and CHARX has no standard MIME type. Filtering by WebView accept
                // would hide valid imports. Select any document and let the existing
                // import handlers validate its contents; SAF grants access on selection.
                picker.setType("*/*");
                picker.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE);
                try {
                    startActivityForResult(picker, CHOOSE_FILE);
                } catch (ActivityNotFoundException missing) {
                    callback.onReceiveValue(null);
                    fileChooser = null;
                    Toast.makeText(MainActivity.this, "系统没有可用的文件选择器", Toast.LENGTH_LONG).show();
                }
                return true;
            }
        });
    }

    private boolean isLocal(Uri uri) {
        return origin != null && "http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost())
            && uri.getPort() == Uri.parse(origin).getPort() && uri.getUserInfo() == null;
    }

    private void openExternal(Uri uri) {
        if (!"http".equals(uri.getScheme()) && !"https".equals(uri.getScheme())) return;
        try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); }
        catch (ActivityNotFoundException missing) {
            Toast.makeText(this, "系统没有可用的浏览器", Toast.LENGTH_LONG).show();
        }
    }

    @Override public void onStateChanged(HostService service) {
        if (service.getPort() > 0) {
            String nextOrigin = "http://127.0.0.1:" + service.getPort();
            if (nextOrigin.equals(origin) && service.getStartupToken().equals(loadedToken)) return;
            if (origin != null) WebViewCompat.removeWebMessageListener(web, "TavernAndroid");
            origin = nextOrigin;
            loadedToken = service.getStartupToken();
            WebViewCompat.addWebMessageListener(web, "TavernAndroid", Collections.singleton(origin),
                (view, message, sourceOrigin, mainFrame, reply) -> {
                    // Exact origin alone is insufficient: an untrusted card could load a same-origin iframe.
                    if (mainFrame && isLocal(sourceOrigin)) receiveExport(message.getData(), reply);
                });
            CookieManager.getInstance().setCookie(origin,
                "tavern_session=" + service.getStartupToken() + "; Path=/; HttpOnly; SameSite=Strict",
                accepted -> {
                    if (accepted) web.loadUrl(origin + "/");
                    else showStatus("无法设置本地访问凭据，请检查系统 WebView。", false);
                });
        } else if (service.getError() != null) {
            showStatus("本地服务启动失败\n\n" + service.getError(), false);
        } else showStatus("正在启动本地 Python 服务…\n首次启动需要解压应用资源。", true);
    }

    private void showStatus(String text, boolean loading) {
        statusText.setText(text);
        progress.setVisibility(loading ? View.VISIBLE : View.GONE);
        retry.setVisibility(loading ? View.GONE : View.VISIBLE);
        status.setVisibility(View.VISIBLE);
    }

    private void receiveExport(String text, JavaScriptReplyProxy reply) {
        files.execute(() -> {
            String request = "";
            boolean ownsTransfer = false;
            try {
                if (text == null || text.length() > 100000) throw new IOException("导出消息大小无效");
                JSONObject message = new JSONObject(text);
                request = message.getString("id");
                String action = message.getString("action");
                if ("begin".equals(action)) {
                    if (transfer != null) throw new IOException("已有文件正在保存，请先完成或取消");
                    ExportTransfer next = new ExportTransfer();
                    next.id = ++nextTransfer;
                    next.expected = message.getLong("size");
                    if (next.expected < 0) throw new IOException("导出文件大小无效");
                    next.name = message.getString("name").replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", "_");
                    if (next.name.isEmpty()) next.name = "tavern-export";
                    next.mime = message.optString("mime", "application/octet-stream").split(";", 2)[0];
                    if (next.mime.isEmpty()) next.mime = "application/octet-stream";
                    next.file = File.createTempFile("tavern-export-", ".tmp", getCacheDir());
                    next.stream = new FileOutputStream(next.file);
                    transfer = next;
                    ownsTransfer = true;
                    respond(reply, request, next.id, null);
                } else {
                    if (transfer == null || transfer.id != message.getInt("transfer")) throw new IOException("导出任务已经结束");
                    ownsTransfer = true;
                    if ("chunk".equals(action)) {
                        byte[] data = Base64.decode(message.getString("data"), Base64.DEFAULT);
                        if (data.length > 65536 || transfer.written + data.length > transfer.expected) throw new IOException("导出数据大小不匹配");
                        transfer.stream.write(data);
                        transfer.written += data.length;
                        respond(reply, request, true, null);
                    } else if ("finish".equals(action)) {
                        if (transfer.written != transfer.expected) throw new IOException("导出数据不完整");
                        transfer.stream.close();
                        transfer.stream = null;
                        saveReply = reply;
                        saveRequest = request;
                        main.post(() -> {
                            Intent save = new Intent(Intent.ACTION_CREATE_DOCUMENT);
                            save.addCategory(Intent.CATEGORY_OPENABLE);
                            save.setType(transfer.mime);
                            save.putExtra(Intent.EXTRA_TITLE, transfer.name);
                            try { startActivityForResult(save, SAVE_FILE); }
                            catch (ActivityNotFoundException missing) {
                                files.execute(() -> finishExport(null, "系统没有可用的文件保存器"));
                            }
                        });
                    } else if ("abort".equals(action)) {
                        discardExport();
                        respond(reply, request, false, null);
                    } else throw new IOException("未知导出操作");
                }
            } catch (Exception failure) {
                respond(reply, request, null, "导出失败：" + failure);
                // An interrupted write is never offered as a successfully saved file.
                if (ownsTransfer) discardExport();
            }
        });
    }

    private void respond(JavaScriptReplyProxy reply, String id, Object value, String error) {
        JSONObject payload = new JSONObject();
        try {
            payload.put("id", id);
            payload.put("ok", error == null);
            if (error == null) payload.put("value", value);
            else payload.put("error", error);
        } catch (org.json.JSONException impossible) { throw new IllegalStateException(impossible); }
        main.post(() -> reply.postMessage(payload.toString()));
    }

    private void finishExport(Uri destination, String initialError) {
        String error = initialError;
        boolean saved = false;
        try {
            if (destination != null) {
                try (FileInputStream input = new FileInputStream(transfer.file);
                     OutputStream output = getContentResolver().openOutputStream(destination, "wt")) {
                    if (output == null) throw new IOException("无法打开保存位置");
                    byte[] buffer = new byte[65536];
                    int count;
                    while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
                    output.flush();
                    saved = true;
                }
            }
        } catch (Exception failure) {
            error = "保存失败：" + failure.getMessage();
        } finally {
            respond(saveReply, saveRequest, saved, error);
            saveReply = null;
            saveRequest = null;
            discardExport();
        }
    }

    private void discardExport() {
        if (transfer == null) return;
        try { if (transfer.stream != null) transfer.stream.close(); }
        catch (IOException ignored) { /* The failed transfer is deleted below. */ }
        if (!transfer.file.delete()) transfer.file.deleteOnExit();
        transfer = null;
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == CHOOSE_FILE && fileChooser != null) {
            Uri[] values = null;
            if (result == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    values = new Uri[data.getClipData().getItemCount()];
                    for (int index = 0; index < values.length; index++) values[index] = data.getClipData().getItemAt(index).getUri();
                } else if (data.getData() != null) values = new Uri[]{data.getData()};
            }
            fileChooser.onReceiveValue(values);
            fileChooser = null;
        } else if (request == SAVE_FILE && saveReply != null) {
            Uri destination = result == RESULT_OK && data != null ? data.getData() : null;
            files.execute(() -> finishExport(destination, null));
        }
    }

    @Override public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else moveTaskToBack(true);
    }

    @Override protected void onDestroy() {
        if (host != null) host.removeListener(this);
        if (bound) unbindService(connection);
        if (fileChooser != null) fileChooser.onReceiveValue(null);
        web.destroy();
        files.execute(this::discardExport);
        files.shutdown();
        super.onDestroy();
    }
}
