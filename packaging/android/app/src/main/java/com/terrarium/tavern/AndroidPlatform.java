package com.terrarium.tavern;

import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyStore;
import java.util.Arrays;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Python-facing capabilities. None of these methods are exposed to JavaScript. */
public final class AndroidPlatform {
    private static final String KEY_ALIAS = "terrarium.chatgpt.accounts.v1";
    private static Context context;
    private static HostService host;

    static void initialize(HostService service) {
        context = service.getApplicationContext();
        host = service;
    }

    public static void backendReady(int port) { host.backendReady(port); }

    public static int getServerPort() {
        return context.getSharedPreferences("runtime", Context.MODE_PRIVATE).getInt("port", 0);
    }

    public static void rememberServerPort(int port) {
        if (!context.getSharedPreferences("runtime", Context.MODE_PRIVATE).edit().putInt("port", port).commit()) {
            throw new IllegalStateException("无法保存本地服务端口，请检查设备存储空间");
        }
    }

    public static boolean openBrowser(String url) {
        Uri uri = Uri.parse(url);
        if (!"https".equals(uri.getScheme()) && !"http".equals(uri.getScheme())) {
            throw new IllegalArgumentException("Only HTTP browser URLs are allowed");
        }
        Intent intent = new Intent(Intent.ACTION_VIEW, uri);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            context.startActivity(intent);
            return true;
        } catch (ActivityNotFoundException unavailable) {
            return false;
        }
    }

    private static synchronized SecretKey credentialKey() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (store.containsAlias(KEY_ALIAS)) return (SecretKey) store.getKey(KEY_ALIAS, null);
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(KEY_ALIAS,
            KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build());
        return generator.generateKey();
    }

    public static String encryptCredentials(String json) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, credentialKey());
        byte[] ciphertext = cipher.doFinal(json.getBytes(StandardCharsets.UTF_8));
        byte[] iv = cipher.getIV();
        byte[] encoded = Arrays.copyOf(iv, iv.length + ciphertext.length);
        System.arraycopy(ciphertext, 0, encoded, iv.length, ciphertext.length);
        return Base64.encodeToString(encoded, Base64.NO_WRAP);
    }

    public static String decryptCredentials(String encoded) throws Exception {
        byte[] payload = Base64.decode(encoded, Base64.DEFAULT);
        if (payload.length < 28) throw new GeneralSecurityException("Invalid protected credentials");
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, credentialKey(), new GCMParameterSpec(128, payload, 0, 12));
        return new String(cipher.doFinal(payload, 12, payload.length - 12), StandardCharsets.UTF_8);
    }
}
