package com.terrarium.tavern;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Messenger;
import android.os.Process;
import java.util.ArrayList;
import org.json.JSONObject;

/** Host-side async client. No method waits for the renderer or for the main Looper. */
public final class RealmClient {
    private final Context context;
    private final RealmCallback callback;
    private final Class<? extends RealmService> realm;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ArrayList<String> queued = new ArrayList<>();
    private Messenger incoming;
    private Messenger service;
    private ServiceConnection connection;
    private int pid;
    private boolean closed;
    private boolean bound;
    private volatile int generation;

    /** {@code tools} selects :realm_tools, so resetting one process never ends the other's pages. */
    public RealmClient(Context context, RealmCallback callback, boolean tools) {
        this.context = context.getApplicationContext();
        this.callback = callback;
        realm = tools ? ToolRealmService.class : RealmService.class;
    }

    public void request(String json) {
        main.post(() -> {
            if (closed) return;
            if (service != null && pid != 0) send(json);
            else {
                queued.add(json);
                if (connection == null) connect();
            }
        });
    }

    private void connect() {
        int epoch = ++generation;
        incoming = new Messenger(new Handler(Looper.getMainLooper(), message -> {
            if (epoch != generation) return true;
            RealmTransport.read(message, json -> {
                if (epoch == generation) received(json, epoch);
            }, error -> failed(error, epoch));
            return true;
        }));
        connection = new ServiceConnection() {
            @Override public void onServiceConnected(ComponentName name, IBinder binder) {
                if (connection != this || closed) return;
                service = new Messenger(binder);
                send("{\"command\":\"connect\"}");
            }
            @Override public void onServiceDisconnected(ComponentName name) {
                if (connection == this) failed(new IllegalStateException("卡片脚本进程已停止"), epoch);
            }
            @Override public void onBindingDied(ComponentName name) {
                onServiceDisconnected(name);
            }
            @Override public void onNullBinding(ComponentName name) {
                onServiceDisconnected(name);
            }
        };
        bound = context.bindService(new Intent(context, realm), connection, Context.BIND_AUTO_CREATE);
        if (!bound)
            failed(new IllegalStateException("无法启动卡片脚本进程"), epoch);
    }

    private void send(String json) {
        int epoch = generation;
        RealmTransport.send(service, incoming, json, error -> {
            failed(error, epoch);
        });
    }

    private void received(String json, int epoch) {
        try {
            JSONObject message = new JSONObject(json);
            if (message.optString("type").equals("connected")) {
                main.post(() -> {
                    if (closed || service == null || epoch != generation) return;
                    pid = message.optInt("pid");
                    for (String request : queued) send(request);
                    queued.clear();
                });
            } else if (epoch == generation) callback.onMessage(json);
        } catch (Exception error) { failed(error, epoch); }
    }

    private void failed(Exception error, int epoch) {
        main.post(() -> {
            if (closed || epoch != generation) return;
            disconnect(true);
            try {
                callback.onMessage(new JSONObject().put("type", "disconnected")
                        .put("error", error.toString()).toString());
            } catch (org.json.JSONException impossible) { throw new IllegalStateException(impossible); }
        });
    }

    private void disconnect(boolean kill) {
        generation++;
        queued.clear();
        service = null;
        ServiceConnection previous = connection;
        connection = null;
        if (previous != null && bound) context.unbindService(previous);
        bound = false;
        if (kill && pid != 0) Process.killProcess(pid);
        pid = 0;
    }

    /** Cancellation kills the separate process even when JavaScript never yields. */
    public void reset() { main.post(() -> disconnect(true)); }

    public void close() {
        main.post(() -> { closed = true; disconnect(false); });
    }
}
