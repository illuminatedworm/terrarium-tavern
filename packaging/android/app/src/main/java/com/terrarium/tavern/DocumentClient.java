package com.terrarium.tavern;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Message;
import android.os.Messenger;
import android.os.Process;
import android.os.RemoteException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import org.json.JSONException;
import org.json.JSONObject;

/** One bound, disposable parser process. Call result/close off the Android UI thread. */
public final class DocumentClient implements AutoCloseable {
    static final int CONVERT = 1;
    static final int PID = 2;
    static final int RESULT = 3;
    static final int STOP = 4;
    private final Context context;
    private final String request;
    private final CompletableFuture<String> completion = new CompletableFuture<>();
    private final CountDownLatch death = new CountDownLatch(1);
    private boolean bound;
    private boolean closed;
    private int workerPid;
    private Messenger service;

    private final Messenger reply = new Messenger(new Handler(Looper.getMainLooper(), message -> {
        synchronized (DocumentClient.this) {
            if (message.what == PID) {
                workerPid = message.arg1;
                if (closed) Process.killProcess(workerPid);
            } else if (message.what == RESULT) {
                completion.complete(message.getData().getString("result"));
            }
        }
        return true;
    }));

    private final ServiceConnection connection = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            synchronized (DocumentClient.this) {
                if (closed) return;
                service = new Messenger(binder);
                try {
                    binder.linkToDeath(() -> {
                        completion.complete(error("Office 转换进程异常退出，请检查文件或拆分后重试。"));
                        death.countDown();
                    }, 0);
                    Message message = Message.obtain(null, CONVERT);
                    Bundle data = new Bundle();
                    data.putString("request", request);
                    message.setData(data);
                    message.replyTo = reply;
                    service.send(message);
                } catch (RemoteException failure) {
                    completion.complete(error("Office 转换进程无法连接，请重试。"));
                    death.countDown();
                }
            }
        }

        @Override public void onServiceDisconnected(ComponentName name) {
            completion.complete(error("Office 转换进程异常退出，请检查文件或拆分后重试。"));
            death.countDown();
        }

        @Override public void onNullBinding(ComponentName name) {
            completion.complete(error("Office 转换服务无法启动，请重试。"));
        }

        @Override public void onBindingDied(ComponentName name) {
            completion.complete(error("Office 转换服务已退出，请重试。"));
            death.countDown();
        }
    };

    public DocumentClient(Context context, String request) {
        this.context = context.getApplicationContext();
        this.request = request;
        bound = this.context.bindService(new Intent(this.context, DocumentService.class), connection,
            Context.BIND_AUTO_CREATE);
        if (!bound) completion.complete(error("Office 转换服务无法启动，请重试。"));
    }

    public String result(long timeoutMillis) {
        try {
            return completion.get(timeoutMillis, TimeUnit.MILLISECONDS);
        } catch (TimeoutException failure) {
            return error("Office 转换超过 30 秒，已终止；请先拆分文件。");
        } catch (InterruptedException failure) {
            Thread.currentThread().interrupt();
            return error("Office 转换已取消。");
        } catch (ExecutionException failure) {
            return error("Office 转换服务失败，请重试。");
        }
    }

    @Override public void close() {
        boolean connected;
        synchronized (this) {
            if (closed) return;
            closed = true;
            completion.complete(error("Office 转换已取消。"));
            // Unbind before killing, so Android does not restart a still-bound service.
            if (bound) {
                context.unbindService(connection);
                bound = false;
            }
            connected = service != null;
            if (workerPid != 0) {
                Process.killProcess(workerPid);
            } else if (service != null) {
                try {
                    service.send(Message.obtain(null, STOP));
                } catch (RemoteException ignored) {
                    // The parser has already died; the death recipient releases the waiter.
                }
            }
        }
        if (connected) {
            try {
                death.await(1, TimeUnit.SECONDS);
            } catch (InterruptedException failure) {
                Thread.currentThread().interrupt();
            }
        }
    }

    static String error(String detail) {
        try {
            return new JSONObject().put("error", detail).toString();
        } catch (JSONException impossible) {
            throw new IllegalStateException(impossible);
        }
    }
}
