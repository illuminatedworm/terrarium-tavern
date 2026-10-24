package com.terrarium.tavern;

import android.content.Context;
import android.webkit.WebView;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Collections;

/** Host injection covers fresh srcdoc/about:blank windows before untrusted JS. */
final class CardNetworkPolicy {
    private CardNetworkPolicy() {}

    static boolean install(Context context, WebView web) {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) return false;
        try (InputStream source = context.getAssets().open("card-network-guard.js");
                ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
            byte[] block = new byte[4096];
            int count;
            while ((count = source.read(block)) != -1) bytes.write(block, 0, count);
            WebViewCompat.addDocumentStartJavaScript(web,
                    new String(bytes.toByteArray(), StandardCharsets.UTF_8), Collections.singleton("*"));
            return true;
        } catch (IOException missing) {
            throw new IllegalStateException("APK 缺少卡片网络隔离脚本，请重新安装", missing);
        }
    }
}
