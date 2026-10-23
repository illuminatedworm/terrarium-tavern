package com.terrarium.tavern;

import android.app.Service;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Message;
import android.os.Messenger;
import android.os.Process;
import android.os.RemoteException;
import android.system.ErrnoException;
import android.system.Os;
import com.chaquo.python.Python;

/** Runs in :documents; neither a hung parser nor its Python GIL can block the backend. */
public final class DocumentService extends Service {
    private boolean started;
    private final Messenger endpoint = new Messenger(new Handler(Looper.getMainLooper(), message -> {
        if (message.what == DocumentClient.STOP) {
            Process.killProcess(Process.myPid());
        } else if (message.what == DocumentClient.CONVERT) {
            Messenger reply = message.replyTo;
            if (started) {
                sendResult(reply, DocumentClient.error("Office 转换服务繁忙，请重试。"));
                return true;
            }
            started = true;
            try {
                Message pid = Message.obtain(null, DocumentClient.PID);
                pid.arg1 = Process.myPid();
                reply.send(pid);
            } catch (RemoteException failure) {
                Process.killProcess(Process.myPid());
                return true;
            }
            String request = message.getData().getString("request");
            new Thread(() -> convert(request, reply), "office-parser").start();
        }
        return true;
    }));

    private void convert(String request, Messenger reply) {
        String result;
        try {
            Os.setenv("TAVERN_PLATFORM", "android", true);
            if (!Python.isStarted()) {
                Python.start(new com.chaquo.python.android.AndroidPlatform(this));
            }
            result = Python.getInstance().getModule("tavern.character.editor_file_worker")
                .callAttr("convert_request", request).toString();
        } catch (RuntimeException | ErrnoException failure) {
            result = DocumentClient.error("Office 解析服务失败，请检查文件是否损坏或拆分后重试。");
        }
        sendResult(reply, result);
    }

    private static void sendResult(Messenger reply, String result) {
        Message response = Message.obtain(null, DocumentClient.RESULT);
        Bundle data = new Bundle();
        data.putString("result", result);
        response.setData(data);
        try {
            reply.send(response);
        } catch (RemoteException failure) {
            Process.killProcess(Process.myPid());
        }
    }

    @Override public IBinder onBind(Intent intent) {
        return endpoint.getBinder();
    }

    @Override public void onDestroy() {
        super.onDestroy();
        // A parser may ignore interruption. Ending its process is the cancellation boundary.
        Process.killProcess(Process.myPid());
    }
}
