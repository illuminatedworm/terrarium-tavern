package com.terrarium.tavern;

import android.app.Instrumentation;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.os.IBinder;
import java.io.IOException;
import java.io.InputStream;
import java.net.BindException;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.URL;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.function.Predicate;
import org.json.JSONObject;

/**
 * Stops the dev app's real backend and restarts it at once. The first run's closed
 * keep-alive connection leaves the remembered port in TIME_WAIT, which must not block
 * the restart.
 */
final class BackendRestartProbe {
    private static final class State {
        final int port;
        final String token;
        final String error;

        State(HostService service) {
            port = service.getPort();
            token = service.getStartupToken();
            error = service.getError();
        }
    }

    private final Instrumentation instrumentation;
    private final Context context;
    private final LinkedBlockingQueue<State> states = new LinkedBlockingQueue<>();
    private final HostService.Listener listener = service -> states.add(new State(service));
    private HostService host;

    BackendRestartProbe(Instrumentation instrumentation) {
        this.instrumentation = instrumentation;
        context = instrumentation.getTargetContext();
    }

    JSONObject run() throws Exception {
        long started = System.nanoTime();
        CountDownLatch connected = new CountDownLatch(1);
        ServiceConnection connection = new ServiceConnection() {
            @Override public void onServiceConnected(ComponentName name, IBinder binder) {
                host = ((HostService.LocalBinder) binder).service();
                host.addListener(listener);
                connected.countDown();
            }
            @Override public void onServiceDisconnected(ComponentName name) {}
        };
        Intent intent = new Intent(context, HostService.class);
        context.startForegroundService(intent);
        if (!context.bindService(intent, connection, Context.BIND_AUTO_CREATE)) {
            throw new IllegalStateException("无法绑定本地服务");
        }
        try {
            if (!connected.await(10, TimeUnit.SECONDS)) throw new IllegalStateException("本地服务连接超时");
            State first = await(state -> state.port > 0 || state.error != null, 60, "首次启动");
            health(first);
            stop();
            // Without TIME_WAIT the restart below would pass even without SO_REUSEADDR.
            if (!heldByClosedConnections(first.port)) {
                throw new IllegalStateException("停止后端口未处于 TIME_WAIT，本检查无法覆盖端口复用");
            }
            // The finished backend thread may still be exiting; retry as the visible restart button would.
            State restarting = null;
            for (int attempt = 0; attempt < 50 && restarting == null; attempt++) {
                instrumentation.runOnMainSync(host::startBackend);
                restarting = poll(state -> state.port == 0 && state.error == null, 100);
            }
            if (restarting == null) throw new IllegalStateException("后端停止后无法重新启动");
            State second = await(state -> state.port > 0 || state.error != null, 60, "立即重启");
            if (second.port != first.port) {
                throw new IllegalStateException("重启后端口从 " + first.port + " 变为 " + second.port);
            }
            health(second);
            return new JSONObject()
                    .put("name", "backend_restart")
                    .put("seconds", Math.round((System.nanoTime() - started) / 1e6) / 1000.0)
                    .put("port", second.port);
        } finally {
            // Cleanup must not hide the probe's own failure; the service finishes stopping by itself.
            if (host != null) {
                requestStop();
                instrumentation.runOnMainSync(() -> host.removeListener(listener));
            }
            context.unbindService(connection);
        }
    }

    private void requestStop() {
        context.startService(new Intent(context, HostService.class).setAction("com.terrarium.tavern.STOP"));
    }

    private void stop() throws Exception {
        requestStop();
        await(state -> state.port == 0 && state.error != null, 30, "停止服务");
    }

    private State await(Predicate<State> condition, int seconds, String phase) throws Exception {
        State state = poll(condition, TimeUnit.SECONDS.toMillis(seconds));
        if (state == null) throw new IllegalStateException(phase + "超时");
        if (state.port == 0 && state.error != null && !phase.equals("停止服务")) {
            throw new IllegalStateException(phase + "失败：" + state.error);
        }
        return state;
    }

    private State poll(Predicate<State> condition, long millis) throws InterruptedException {
        long deadline = System.currentTimeMillis() + millis;
        for (long left = millis; left > 0; left = deadline - System.currentTimeMillis()) {
            State state = states.poll(left, TimeUnit.MILLISECONDS);
            if (state != null && condition.test(state)) return state;
        }
        return null;
    }

    /** A plain bind fails while the stopped server's closed connections still hold the port. */
    private static boolean heldByClosedConnections(int port) throws IOException {
        try (ServerSocket socket = new ServerSocket()) {
            socket.setReuseAddress(false);
            socket.bind(new InetSocketAddress("127.0.0.1", port));
            return false;
        } catch (BindException held) {
            return true;
        }
    }

    /** Leaves the connection pooled, so the server closes it when the backend stops. */
    private static void health(State state) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(
                "http://127.0.0.1:" + state.port + "/api/health").openConnection();
        connection.setRequestProperty("X-Tavern-Token", state.token);
        connection.setConnectTimeout(5000);
        connection.setReadTimeout(10000);
        int code = connection.getResponseCode();
        try (InputStream body = code < 400 ? connection.getInputStream() : connection.getErrorStream()) {
            if (body != null) while (body.read() != -1) { /* Drain for keep-alive reuse. */ }
        }
        if (code != 200) throw new IOException("健康检查返回 HTTP " + code);
    }
}
