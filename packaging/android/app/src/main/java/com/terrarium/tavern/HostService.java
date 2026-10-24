package com.terrarium.tavern;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Binder;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.system.Os;
import android.util.Base64;

import com.chaquo.python.Python;

import java.io.File;
import java.security.SecureRandom;
import java.util.HashSet;
import java.util.Set;

/** Owns the local backend; Activity recreation never starts a second Python server. */
public final class HostService extends Service {
    public interface Listener { void onStateChanged(HostService service); }
    public final class LocalBinder extends Binder { HostService service() { return HostService.this; } }

    private static final String CHANNEL = "local-runtime";
    private static final int NOTIFICATION = 1;
    private static final String ACTION_STOP = "com.terrarium.tavern.STOP";
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Set<Listener> listeners = new HashSet<>();
    private final LocalBinder binder = new LocalBinder();
    private Thread backend;
    private String startupToken;
    private int port;
    private String error;
    private boolean stopping;

    public int getPort() { return port; }
    public String getStartupToken() { return startupToken; }
    public String getError() { return error; }
    public boolean isStarting() { return backend != null && backend.isAlive() && port == 0 && error == null; }

    public void addListener(Listener listener) {
        listeners.add(listener);
        listener.onStateChanged(this);
    }

    public void removeListener(Listener listener) { listeners.remove(listener); }

    @Override public void onCreate() {
        super.onCreate();
        AndroidPlatform.initialize(this);
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel(CHANNEL, "本地故事服务", NotificationManager.IMPORTANCE_LOW));
        enterForeground();
    }

    private void enterForeground() {
        Notification notification = notification("正在启动本地服务…");
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIFICATION, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(NOTIFICATION, notification);
        }
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopBackend();
        } else if (backend == null) {
            startBackend();
        }
        // A user relaunch restores SQLite's committed state after process termination.
        return START_NOT_STICKY;
    }

    @Override public IBinder onBind(Intent intent) { return binder; }

    public void startBackend() {
        if (backend != null && backend.isAlive()) return;
        enterForeground();
        byte[] random = new byte[32];
        new SecureRandom().nextBytes(random);
        startupToken = Base64.encodeToString(random, Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING);
        port = 0;
        error = null;
        stopping = false;
        File data = new File(getFilesDir(), "tavern");
        backend = new Thread(() -> {
            try {
                Os.setenv("TAVERN_PLATFORM", "android", true);
                Os.setenv("TAVERN_NATIVE_LIB_DIR", getApplicationInfo().nativeLibraryDir, true);
                Os.setenv("TAVERN_DATA", data.getAbsolutePath(), true);
                Os.setenv("TAVERN_STARTUP_TOKEN", startupToken, true);
                if (!Python.isStarted()) Python.start(new com.chaquo.python.android.AndroidPlatform(this));
                Python python = Python.getInstance();
                // os.environ is a snapshot; a retry uses a fresh token in the same interpreter.
                var environment = python.getModule("os").get("environ");
                environment.callAttr("__setitem__", "TAVERN_PLATFORM", "android");
                environment.callAttr("__setitem__", "TAVERN_NATIVE_LIB_DIR", getApplicationInfo().nativeLibraryDir);
                environment.callAttr("__setitem__", "TAVERN_DATA", data.getAbsolutePath());
                environment.callAttr("__setitem__", "TAVERN_STARTUP_TOKEN", startupToken);
                python.getModule("tavern.android.launcher").callAttr("main");
                main.post(() -> {
                    port = 0;
                    error = "本地服务已停止";
                    changed();
                    stopForeground(STOP_FOREGROUND_REMOVE);
                    stopSelf();
                });
            } catch (Exception failure) {
                main.post(() -> {
                    port = 0;
                    error = failure.toString();
                    getSystemService(NotificationManager.class).notify(NOTIFICATION, notification("启动失败，请打开应用重试"));
                    changed();
                    if (stopping) {
                        stopForeground(STOP_FOREGROUND_REMOVE);
                        stopSelf();
                    }
                });
            }
        }, "tavern-python");
        backend.start();
        changed();
    }

    void backendReady(int readyPort) {
        main.post(() -> {
            if (stopping) {
                stopBackend();
                return;
            }
            port = readyPort;
            getSystemService(NotificationManager.class).notify(NOTIFICATION, notification("本地服务运行中 · 数据保存在此设备"));
            changed();
        });
    }

    private void stopBackend() {
        stopping = true;
        if (Python.isStarted()) {
            new Thread(() -> Python.getInstance().getModule("tavern.android.launcher").callAttr("request_shutdown"),
                "tavern-stop").start();
        }
        if (backend == null || !backend.isAlive()) {
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
        }
    }

    private void changed() {
        for (Listener listener : new HashSet<>(listeners)) listener.onStateChanged(this);
    }

    private Notification notification(String detail) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1,
            new Intent(this, HostService.class).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_tavern)
            .setContentTitle("Terrarium Tavern")
            .setContentText(detail)
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(new Notification.Action.Builder(null, "停止服务", stop).build())
            .build();
    }

    @Override public void onDestroy() {
        stopBackend();
        listeners.clear();
        super.onDestroy();
    }
}
