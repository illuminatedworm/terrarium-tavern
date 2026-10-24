package com.terrarium.tavern;

import android.app.Activity;
import android.app.Instrumentation;
import android.app.KeyguardManager;
import android.content.Intent;
import android.os.Bundle;
import android.os.PowerManager;
import android.system.Os;
import android.util.Log;
import android.webkit.WebView;
import android.webkit.WebChromeClient;
import android.webkit.ConsoleMessage;
import android.webkit.WebViewClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import androidx.webkit.WebViewCompat;
import com.chaquo.python.Python;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayInputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/** Runs on the instrumentation thread while the main Looper serves real realm WebViews. */
public final class DevSmokeInstrumentation extends Instrumentation {
    private String suite;

    @Override public void onCreate(Bundle arguments) {
        super.onCreate(arguments);
        suite = arguments == null ? "all" : arguments.getString("suite", "all");
        start();
    }

    @Override public void onStart() {
        Bundle result = new Bundle();
        int code = Activity.RESULT_CANCELED;
        Activity progress = null;
        try {
            if (!getTargetContext().getSystemService(PowerManager.class).isInteractive()
                    || getTargetContext().getSystemService(KeyguardManager.class).isKeyguardLocked()) {
                throw new IllegalStateException("请先解锁手机并保持亮屏，再运行开发版自检");
            }
            progress = startActivitySync(new Intent(getTargetContext(), DevSmokeActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            Os.setenv("TAVERN_PLATFORM", "android", true);
            Os.setenv("TAVERN_NATIVE_LIB_DIR", getTargetContext().getApplicationInfo().nativeLibraryDir, true);
            Python.start(new com.chaquo.python.android.AndroidPlatform(getTargetContext()));
            JSONObject report = new JSONObject(Python.getInstance().getModule("tavern_dev_smoke")
                    .callAttr("run", getTargetContext().getCacheDir().getAbsolutePath(), suite).toString());
            if (suite.equals("all") || suite.equals("scripts") || suite.equals("ui")) {
                report.getJSONArray("checks").put(probeUiWebRtc(progress));
            }
            if (suite.equals("all") || suite.equals("restart")) {
                // Runs last: the real backend shares this process and sets its own environment.
                report.getJSONArray("checks").put(new BackendRestartProbe(this).run());
                Log.i("TavernDev", "PASS: backend_restart");
            }
            result.putString("report", report.toString());
            code = Activity.RESULT_OK;
        } catch (Exception failure) {
            // Preserve the actual Python/Java exception instead of reducing it to an HTTP 500.
            String error = Log.getStackTraceString(failure);
            Log.e("TavernDev", error);
            result.putString("error", error);
        } finally {
            Activity activity = progress;
            if (activity != null) runOnMainSync(activity::finish);
        }
        finish(code, result);
    }

    private JSONObject probeUiWebRtc(Activity activity) throws Exception {
        CompletableFuture<String> result = new CompletableFuture<>();
        WebView[] holder = new WebView[1];
        try (DatagramSocket udp = new DatagramSocket(0, InetAddress.getByName("127.0.0.1"))) {
            String cardProbe = """
                (async () => {
                    console.log('RTC probe: main');
                    const check = async target => {
                        const values = [];
                        for (const name of ['RTCPeerConnection','webkitRTCPeerConnection']) {
                            try {
                                const peer = new target[name]({iceServers:[{urls:'stun:127.0.0.1:__PORT__'}]});
                                peer.createDataChannel('probe');
                                await peer.setLocalDescription(await peer.createOffer());
                                peer.close(); values.push(false);
                            } catch (error) {
                                values.push(target !== window && error.name === 'SecurityError' ||
                                    target.__tavernScriptIsolation === 1 &&
                                        Object.getOwnPropertyDescriptor(target,name).writable === false);
                            }
                        }
                        return values;
                    };
                    const values = await check(window);
                    const blank = document.createElement('iframe');
                    document.body.append(blank);
                    values.push(...await check(blank.contentWindow));
                    blank.remove();
                    console.log('RTC probe: blank');
                    const html = '<meta charset="utf-8"><script>(' + check.toString() +
                        ')(window).then(value=>parent.postMessage({rtcProbe:value},"*"),error=>parent.postMessage({rtcProbeError:String(error)},"*"))</script>';
                    const blob = URL.createObjectURL(new Blob([html],{type:'text/html'}));
                    for (const [key,value] of [['srcdoc',html],['src',blob],['src','data:text/html;charset=utf-8,'+encodeURIComponent(html)]]) {
                        const child = document.createElement('iframe');
                        child.sandbox = 'allow-scripts';
                        console.log('RTC probe: ' + key + (value.startsWith('data:') ? ' data' : ''));
                        const loaded = new Promise((resolve,reject) => {
                            const receive = event => {
                                if(event.source !== child.contentWindow) return;
                                if(event.data?.rtcProbeError) {removeEventListener('message',receive); reject(Error(event.data.rtcProbeError));}
                                else if(event.data?.rtcProbe) {removeEventListener('message',receive); resolve(event.data.rtcProbe);}
                            };
                            addEventListener('message',receive);
                        });
                        child[key] = value; document.body.append(child);
                        values.push(...await loaded); child.remove();
                    }
                    URL.revokeObjectURL(blob);
                    return values;
                })().then(value=>parent.postMessage({complete:true,value},'*'),
                    error=>parent.postMessage({complete:true,error:String(error)},'*'));
                """.replace("__PORT__", String.valueOf(udp.getLocalPort()));
            String cardHtml = "<!doctype html><meta charset='utf-8'><body><script>"
                    + cardProbe.replace("</script", "<\\/script") + "</script></body>";
            // Match StoryFrame's actual untrusted boundary, including its
            // inability to read a fresh child Window before document-start runs.
            String probe = "(()=>{const card=document.createElement('iframe');card.sandbox='allow-scripts';"
                    + "addEventListener('message',event=>{if(event.source===card.contentWindow&&event.data?.complete)"
                    + "__tavernProbe.postMessage(JSON.stringify(event.data.error?{error:event.data.error}:event.data.value))});"
                    + "card.srcdoc=" + JSONObject.quote(cardHtml) + ";document.body.append(card)})()";
            runOnMainSync(() -> {
                WebView web = holder[0] = new WebView(activity);
                activity.addContentView(web, new android.view.ViewGroup.LayoutParams(1, 1));
                web.getSettings().setJavaScriptEnabled(true);
                web.getSettings().setBlockNetworkLoads(true);
                web.setWebChromeClient(new WebChromeClient() {
                    @Override public boolean onConsoleMessage(ConsoleMessage message) {
                        Log.i("TavernDev", message.message()); return true;
                    }
                });
                if (!CardNetworkPolicy.install(getTargetContext(), web)) {
                    result.completeExceptionally(new IllegalStateException("WebView 缺少文档起始注入"));
                    return;
                }
                WebViewCompat.addWebMessageListener(web, "__tavernProbe", Collections.singleton("https://probe.invalid"),
                    (view, message, origin, mainFrame, reply) -> {
                        if (mainFrame) result.complete(message.getData());
                    });
                web.setWebViewClient(new WebViewClient() {
                    @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                        if ("data".equals(request.getUrl().getScheme())) return null;
                        // A synthetic HTTPS origin avoids about:blank/Blob CSP renderer bugs;
                        // all bytes are supplied here, so no request reaches that hostname.
                        String html = request.getUrl().toString().equals("https://probe.invalid/document")
                            ? "<!doctype html><meta charset='utf-8'><body></body>" : "";
                        return new WebResourceResponse("text/html", "UTF-8",
                            new ByteArrayInputStream(html.getBytes(StandardCharsets.UTF_8)));
                    }
                    @Override public void onPageFinished(WebView view, String url) {
                        Log.i("TavernDev", "RTC page loaded: " + url);
                        if (url.equals("https://probe.invalid/document")) view.evaluateJavascript(probe, null);
                    }
                });
                web.loadUrl("https://probe.invalid/document");
            });
            JSONArray values = new JSONArray(result.get(10, TimeUnit.SECONDS));
            if (values.length() != 10) throw new IllegalStateException("WebRTC 探针未覆盖全部文档");
            for (int i = 0; i < values.length(); i++)
                if (!values.getBoolean(i)) throw new IllegalStateException("WebRTC 构造器未被宿主阻止：" + i);
            udp.setSoTimeout(1);
            try {
                udp.receive(new DatagramPacket(new byte[1024], 1024));
                throw new IllegalStateException("WebRTC 向回环地址发送了 UDP");
            } catch (SocketTimeoutException empty) {
                // Constructors were rejected synchronously; the local listener must remain empty.
            }
            return new JSONObject().put("name", "ui_webrtc_isolation").put("documents", 5).put("udp_packets", 0);
        } finally {
            runOnMainSync(() -> {
                if (holder[0] != null) {
                    if (holder[0].getParent() instanceof android.view.ViewGroup)
                        ((android.view.ViewGroup) holder[0].getParent()).removeView(holder[0]);
                    holder[0].destroy();
                }
            });
        }
    }
}
