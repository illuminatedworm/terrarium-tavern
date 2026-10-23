package com.terrarium.tavern;

import android.app.Service;
import android.content.Intent;
import android.net.Uri;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Messenger;
import android.os.Process;
import android.util.Base64;
import android.webkit.CookieManager;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebStorage;
import androidx.webkit.JavaScriptReplyProxy;
import androidx.webkit.ServiceWorkerClientCompat;
import androidx.webkit.ServiceWorkerControllerCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Story card WebViews live in :realm, with no UI WebView cookies, token or native bridges.
 * Resetting a process ends every page in it, so tools run in {@link ToolRealmService}.
 */
public class RealmService extends Service {
    // HTTPS resources reach shouldInterceptRequest; network loads themselves are disabled.
    // Non-HTTP transports cannot enter that callback, so CSP and bootstrap disable them.
    private static final String CSP = "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' https:; "
            + "connect-src https:; img-src data: blob: https:; style-src 'unsafe-inline' data: https:; "
            + "font-src data: https:; media-src data: blob: https:; frame-src 'none'; child-src 'none'; "
            + "worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; webrtc 'block'";
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Map<String, Page> pages = new HashMap<>();
    private final Map<String, Messenger> contexts = new HashMap<>();
    private final ConcurrentHashMap<String, CompletableFuture<JSONObject>> callbacks = new ConcurrentHashMap<>();
    private final Messenger incoming = new Messenger(new Handler(Looper.getMainLooper(), message -> {
        Messenger reply = message.replyTo;
        RealmTransport.read(message, json -> {
            try {
                JSONObject request = new JSONObject(json);
                // Resource responses must not depend on the WebView UI thread making progress.
                if (request.optString("command").equals("callback")) {
                    CompletableFuture<JSONObject> pending = callbacks.remove(request.getString("callback"));
                    if (pending != null) pending.complete(request);
                } else main.post(() -> handle(request, reply));
            } catch (Exception error) { failProcess(); }
        }, error -> failProcess());
        return true;
    }));

    // Android keeps the process cached after the last client unbinds. A later bind creates
    // a new service instance there, but the suffix may only be set before WebView first loads.
    private static boolean dataDirectorySet;

    /** Each WebView process needs its own data directory. */
    protected String dataDirectorySuffix() { return "realm"; }

    @Override public void onCreate() {
        super.onCreate();
        if (!dataDirectorySet) {
            WebView.setDataDirectorySuffix(dataDirectorySuffix());
            dataDirectorySet = true;
        }
        CookieManager.getInstance().setAcceptCookie(false);
        if (WebViewFeature.isFeatureSupported(WebViewFeature.SERVICE_WORKER_BASIC_USAGE)) {
            ServiceWorkerControllerCompat controller = ServiceWorkerControllerCompat.getInstance();
            controller.getServiceWorkerWebSettings().setBlockNetworkLoads(true);
            controller.setServiceWorkerClient(new ServiceWorkerClientCompat() {
                @Override public WebResourceResponse shouldInterceptRequest(WebResourceRequest request) {
                    return denied();
                }
            });
        }
    }

    @Override public IBinder onBind(Intent intent) { return incoming.getBinder(); }

    private void send(Messenger destination, JSONObject value) {
        RealmTransport.send(destination, incoming, value.toString(), error -> failProcess());
    }

    private static JSONObject object(Object... pairs) {
        JSONObject value = new JSONObject();
        try {
            for (int i = 0; i < pairs.length; i += 2) value.put((String) pairs[i], pairs[i + 1]);
        } catch (org.json.JSONException impossible) { throw new IllegalArgumentException(impossible); }
        return value;
    }

    private void result(Messenger client, String request, Object value, String error) {
        send(client, object("type", "result", "request", request,
                "value", value == null ? JSONObject.NULL : value,
                "error", error == null ? JSONObject.NULL : error));
    }

    private void handle(JSONObject request, Messenger client) {
        String requestId = request.optString("request");
        try {
            switch (request.getString("command")) {
                case "connect":
                    send(client, object("type", "connected", "pid", Process.myPid()));
                    break;
                case "context":
                    contexts.put(request.getString("context"), client);
                    result(client, requestId, null, null);
                    break;
                case "open": {
                    if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)
                            || !WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT))
                        throw new IllegalStateException("Android System WebView 版本过旧，请更新后重试");
                    String contextId = request.getString("context");
                    if (!contexts.containsKey(contextId)) throw new IllegalStateException("脚本环境已关闭");
                    Page page = new Page(request.getString("page"), contextId, client,
                            requestId, request.getString("html"));
                    pages.put(page.id, page);
                    page.web.loadUrl(page.url);
                    break;
                }
                case "evaluate": {
                    Page page = page(request);
                    // Serialize the argument as a JSON literal: String.valueOf would splice
                    // string arguments (HTML, code, selectors, URLs) into the source unquoted.
                    String argument = new JSONArray().put(request.opt("arg")).toString();
                    String expression = "(async()=>{try{let value=(0,eval)("
                            + JSONObject.quote("(" + request.getString("script") + "\n)")
                            + ");if(typeof value==='function')value=value("
                            + argument + "[0]);value=await value;__tavernRealmResult("
                            + JSONObject.quote(requestId) + ",value,null)}catch(error){__tavernRealmResult("
                            + JSONObject.quote(requestId) + ",null,String(error&&error.stack||error))}})()";
                    page.pending.put(requestId, true);
                    page.web.evaluateJavascript(expression, null);
                    break;
                }
                case "script": {
                    Page page = page(request);
                    // A classic script element preserves global let/const bindings across calls.
                    String expression = "(()=>{let failure=null;const failed=e=>{failure=String(e.error&&e.error.stack||e.message)};"
                            + "addEventListener('error',failed);try{const script=document.createElement('script');script.text="
                            + JSONObject.quote(request.getString("source"))
                            + ";document.head.appendChild(script);script.remove()}catch(error){failure=String(error)}"
                            + "removeEventListener('error',failed);__tavernRealmResult("
                            + JSONObject.quote(requestId) + ",null,failure)})()";
                    page.pending.put(requestId, true);
                    page.web.evaluateJavascript(expression, null);
                    break;
                }
                case "close_page": {
                    Page page = pages.remove(request.getString("page"));
                    if (page != null) page.destroy();
                    result(client, requestId, null, null);
                    break;
                }
                case "close_context": {
                    String contextId = request.getString("context");
                    for (Page page : pages.values().toArray(new Page[0])) if (page.context.equals(contextId)) {
                        pages.remove(page.id);
                        page.destroy();
                    }
                    contexts.remove(contextId);
                    result(client, requestId, null, null);
                    break;
                }
                default: throw new IllegalArgumentException("未知脚本操作");
            }
        } catch (Exception error) { result(client, requestId, null, error.toString()); }
    }

    private Page page(JSONObject request) throws Exception {
        Page page = pages.get(request.getString("page"));
        if (page == null) throw new IllegalStateException("脚本页面已关闭");
        return page;
    }

    private CompletableFuture<JSONObject> callback(Page page, String type, Object... fields) {
        String id = UUID.randomUUID().toString();
        CompletableFuture<JSONObject> future = new CompletableFuture<>();
        callbacks.put(id, future);
        page.waiting.add(future);
        JSONObject message = object("type", type, "callback", id, "context", page.context, "page", page.id);
        try { for (int i = 0; i < fields.length; i += 2) message.put((String) fields[i], fields[i + 1]); }
        catch (org.json.JSONException impossible) { throw new IllegalArgumentException(impossible); }
        future.whenComplete((value, error) -> {
            callbacks.remove(id);
            page.waiting.remove(future);
        });
        send(page.client, message);
        return future;
    }

    private final class Page {
        final String id, context, origin, url, opening;
        final Messenger client;
        final WebView web;
        final Map<String, Boolean> pending = new HashMap<>();
        final java.util.Set<CompletableFuture<JSONObject>> waiting = ConcurrentHashMap.newKeySet();
        volatile String html;
        boolean loaded;
        volatile boolean destroyed;

        @SuppressWarnings("deprecation")
        Page(String id, String context, Messenger client, String opening, String html) {
            this.id = id;
            this.context = context;
            this.client = client;
            this.opening = opening;
            this.html = html;
            origin = "https://p" + UUID.randomUUID().toString().replace("-", "") + ".realm.invalid";
            url = origin + "/document";
            web = new WebView(RealmService.this);
            WebSettings settings = web.getSettings();
            settings.setJavaScriptEnabled(true);
            settings.setDomStorageEnabled(false);
            settings.setDatabaseEnabled(false);
            settings.setAllowFileAccess(false);
            settings.setAllowContentAccess(false);
            settings.setAllowFileAccessFromFileURLs(false);
            settings.setAllowUniversalAccessFromFileURLs(false);
            settings.setBlockNetworkLoads(true);
            settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
            settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
            settings.setSafeBrowsingEnabled(false);
            settings.setGeolocationEnabled(false);
            settings.setMediaPlaybackRequiresUserGesture(true);
            settings.setSupportMultipleWindows(false);
            CookieManager.getInstance().setAcceptThirdPartyCookies(web, false);
            web.setWebChromeClient(new WebChromeClient());
            android.util.DisplayMetrics display = getResources().getDisplayMetrics();
            web.measure(android.view.View.MeasureSpec.makeMeasureSpec(display.widthPixels, android.view.View.MeasureSpec.EXACTLY),
                    android.view.View.MeasureSpec.makeMeasureSpec(display.heightPixels, android.view.View.MeasureSpec.EXACTLY));
            web.layout(0, 0, display.widthPixels, display.heightPixels);
            web.setWebViewClient(new WebViewClient() {
                @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                    return true;
                }
                @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                    if (request.isForMainFrame() && request.getUrl().toString().equals(url)
                            && request.getMethod().equals("GET") && Page.this.html != null) {
                        byte[] bytes = Page.this.html.getBytes(StandardCharsets.UTF_8);
                        Page.this.html = null;
                        return response(bytes, "text/html", 200, true);
                    }
                    if (destroyed || request.isForMainFrame()) return denied();
                    CompletableFuture<JSONObject> future = callback(Page.this, "resource",
                            "url", request.getUrl().toString(), "method", request.getMethod());
                    try {
                        JSONObject result = future.get(45, TimeUnit.SECONDS);
                        if (!result.isNull("error")) throw new IllegalStateException(result.getString("error"));
                        if (result.isNull("value")) return denied();
                        JSONObject resource = result.getJSONObject("value");
                        byte[] data = resource.getBoolean("binary")
                                ? Base64.decode(resource.getString("body"), Base64.DEFAULT)
                                : resource.getString("body").getBytes(StandardCharsets.UTF_8);
                        return response(data, resource.getString("mime"), 200, false);
                    } catch (Exception error) {
                        future.completeExceptionally(error);
                        send(client, object("type", "pageerror", "page", id, "error", error.toString()));
                        return response(new byte[0], "text/plain", 500, false);
                    }
                }
                @Override public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                    // All pages share this renderer's failure boundary; Python must discard their mirrors.
                    failProcess();
                    return true;
                }
            });
            WebViewCompat.addWebMessageListener(web, "__tavernBridge", Collections.singleton(origin),
                    (view, message, source, mainFrame, reply) -> {
                        if (!mainFrame || !source.equals(Uri.parse(origin)) || destroyed) return;
                        try { message(new JSONObject(message.getData()), reply); }
                        catch (Exception error) {
                            send(client, object("type", "pageerror", "page", id, "error", error.toString()));
                        }
                    });
            WebViewCompat.addDocumentStartJavaScript(web, NETWORK_GUARD, Collections.singleton("*"));
            WebViewCompat.addDocumentStartJavaScript(web, BOOTSTRAP, Collections.singleton(origin));
        }

        void message(JSONObject message, JavaScriptReplyProxy reply) throws Exception {
            switch (message.getString("type")) {
                case "loaded":
                    if (!loaded) { loaded = true; result(client, opening, null, null); }
                    break;
                case "result": {
                    String request = message.getString("request");
                    if (pending.remove(request) != null)
                        result(client, request, message.opt("value"), message.isNull("error") ? null : message.getString("error"));
                    break;
                }
                case "host": {
                    Object hostId = message.get("request");
                    callback(this, "host", "method", message.getString("method"), "args", message.opt("args"))
                            .whenComplete((response, error) -> main.post(() -> {
                                if (destroyed) return;
                                reply.postMessage(object("request", hostId,
                                        "value", error == null ? response.opt("value") : JSONObject.NULL,
                                        "error", error == null ? response.opt("error") : error.toString()).toString());
                            }));
                    break;
                }
                case "pageerror":
                    send(client, object("type", "pageerror", "page", id, "error", message.getString("error")));
                    break;
                default: throw new IllegalArgumentException("未知脚本桥消息");
            }
        }

        void destroy() {
            destroyed = true;
            html = null;
            if (!loaded) result(client, opening, null, "脚本页面已关闭");
            for (CompletableFuture<JSONObject> future : waiting)
                future.completeExceptionally(new IllegalStateException("脚本页面已关闭"));
            for (String request : pending.keySet()) result(client, request, null, "脚本页面已关闭");
            pending.clear();
            web.stopLoading();
            web.destroy();
            WebStorage.getInstance().deleteOrigin(origin);
        }
    }

    private static WebResourceResponse response(byte[] body, String mime, int status, boolean document) {
        Map<String, String> headers = new HashMap<>();
        headers.put("Cache-Control", "no-store");
        headers.put("Access-Control-Allow-Origin", "*");
        headers.put("X-Content-Type-Options", "nosniff");
        headers.put("X-DNS-Prefetch-Control", "off");
        if (document) headers.put("Content-Security-Policy", CSP);
        return new WebResourceResponse(mime.split(";", 2)[0], "UTF-8", status,
                status == 200 ? "OK" : "Blocked", headers, new ByteArrayInputStream(body));
    }

    private static WebResourceResponse denied() { return response(new byte[0], "text/plain", 403, false); }

    private void failProcess() { Process.killProcess(Process.myPid()); }

    @Override public void onDestroy() {
        for (Page page : pages.values()) page.destroy();
        pages.clear();
        for (CompletableFuture<JSONObject> future : callbacks.values())
            future.completeExceptionally(new IllegalStateException("脚本环境已关闭"));
        callbacks.clear();
        super.onDestroy();
    }

    // Also install in inherited about:blank/srcdoc frames before their scripts: a fresh
    // Window must not recover a network constructor disabled in the top-level document.
    private static final String NETWORK_GUARD = "(()=>{'use strict';"
            + "const define=(name,value)=>Object.defineProperty(globalThis,name,{value,writable:false,configurable:false});"
            + "for(const name of ['WebSocket','WebTransport','RTCPeerConnection','webkitRTCPeerConnection','Worker','SharedWorker'])"
            + "define(name,class{constructor(){throw Error('卡片不能访问网络或启动工作线程')}});"
            + "if(navigator.serviceWorker)Object.defineProperty(navigator,'serviceWorker',{value:undefined});"
            + "for(const name of ['indexedDB','caches','webkitRequestFileSystem'])define(name,undefined);"
            + "Object.defineProperty(navigator,'storage',{value:undefined});"
            + "define('open',()=>null);})();";

    private static final String BOOTSTRAP = "(()=>{'use strict';const bridge=globalThis.__tavernBridge;"
            + "const send=message=>bridge.postMessage(JSON.stringify(message));const calls=new Map();let sequence=0;"
            + "const define=(name,value)=>Object.defineProperty(globalThis,name,{value,writable:false,configurable:false});"
            + "bridge.onmessage=event=>{const result=JSON.parse(event.data),pending=calls.get(result.request);"
            + "if(!pending)return;calls.delete(result.request);result.error?pending.reject(Error(result.error)):pending.resolve(result.value)};"
            + "define('__tavernHost',(method,args)=>new Promise((resolve,reject)=>{const request=++sequence;"
            + "calls.set(request,{resolve,reject});send({type:'host',request,method,args})}));"
            + "define('__tavernRealmResult',(request,value,error)=>{try{send({type:'result',request,value:value===undefined?null:value,error})}"
            + "catch(error){send({type:'result',request,value:null,error:String(error)})}});"
            // A service WebView has no surface; native rAF would never fire while detached.
            + "define('requestAnimationFrame',callback=>setTimeout(()=>callback(performance.now()),16));"
            + "define('cancelAnimationFrame',handle=>clearTimeout(handle));"
            + "addEventListener('error',event=>send({type:'pageerror',error:String(event.error&&event.error.stack||event.message)}));"
            + "addEventListener('unhandledrejection',event=>send({type:'pageerror',error:String(event.reason)}));"
            + "addEventListener('load',()=>send({type:'loaded'}),{once:true});})();";
}
