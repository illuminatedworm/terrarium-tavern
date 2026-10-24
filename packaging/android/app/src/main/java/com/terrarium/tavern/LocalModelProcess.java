package com.terrarium.tavern;

import com.chaquo.python.PyObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Public bridge: Chaquopy must not invoke hidden ProcessEnvironment/UNIXProcess implementations. */
public final class LocalModelProcess {
    private final Process process;
    private final int pid;
    private final BufferedReader output;
    private final ArrayDeque<String> logTail = new ArrayDeque<>();
    private final ArrayDeque<String> diagnostics = new ArrayDeque<>();
    private PyObject readyCallback;
    private static final Pattern LISTENING = Pattern.compile("listening on (http://127\\.0\\.0\\.1:\\d+)");

    private LocalModelProcess(Process process, PyObject readyCallback) throws IOException {
        this.process = process;
        this.readyCallback = readyCallback;
        output = new BufferedReader(new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8));
        // Android's public Process API has no pid(), and destroyForcibly() only
        // repeats SIGTERM. The launcher reports its own PID before exec, then
        // waits for our acknowledgement so failed setup cannot orphan the engine.
        try {
            pid = Integer.parseInt(output.readLine());
        } catch (NumberFormatException failure) {
            throw new IOException("Android model launcher did not report its process ID", failure);
        }
    }

    private void launch() throws IOException {
        process.getOutputStream().write('\n');
        process.getOutputStream().close();
        // Do not hold a Python thread inside a blocking Java readLine call. Only the
        // one-shot readiness notification crosses back into Python.
        Thread reader = new Thread(this::readOutput, "tavern-model-output");
        reader.setDaemon(true);
        reader.start();
    }

    public static LocalModelProcess start(String[] command, String nativeDirectory, PyObject readyCallback) throws IOException {
        ArrayList<String> launch = new ArrayList<>(Arrays.asList(
            "/system/bin/sh", "-c", "echo $$; read -r ready || exit; exec \"$@\"", "tavern-model"));
        launch.addAll(Arrays.asList(command));
        ProcessBuilder builder = new ProcessBuilder(launch).redirectErrorStream(true);
        Map<String, String> environment = builder.environment();
        environment.keySet().removeIf(key -> key.startsWith("LLAMA_") || key.startsWith("GGML_"));
        // An exec child has no Java classloader namespace. Public vendor drivers
        // declared in the manifest must also be on its linker search path.
        environment.put("LD_LIBRARY_PATH", nativeDirectory + ":/vendor/lib64");
        environment.put("ADSP_LIBRARY_PATH", nativeDirectory);
        environment.put("CDSP_LIBRARY_PATH", nativeDirectory);
        // No unbounded driver-kernel cache in the app's private home directory.
        environment.put("GGML_OPENCL_KERNEL_CACHE_DIR", "off");
        Process process = builder.start();
        LocalModelProcess result = null;
        try {
            result = new LocalModelProcess(process, readyCallback);
            result.launch();
            return result;
        } catch (IOException failure) {
            if (result != null) android.os.Process.killProcess(result.pid);
            process.destroy(); // Before acknowledgement the child is still the waiting shell.
            throw failure;
        }
    }

    private void readOutput() {
        try {
            String line;
            while ((line = output.readLine()) != null) {
                synchronized (logTail) {
                    if (logTail.size() == 80) logTail.removeFirst();
                    logTail.addLast(line);
                }
                if (line.contains("using device ") || line.contains("offloaded ")
                        || line.contains("model buffer size") || line.contains("compute buffer size")) {
                    synchronized (diagnostics) {
                        if (diagnostics.size() == 24) diagnostics.removeFirst();
                        diagnostics.addLast(line);
                    }
                }
                Matcher match = LISTENING.matcher(line);
                if (match.find()) ready(match.group(1));
            }
        } catch (IOException failure) {
            synchronized (logTail) { logTail.addLast(failure.toString()); }
        } finally {
            ready(null);
        }
    }

    private synchronized void ready(String address) {
        PyObject callback = readyCallback;
        readyCallback = null; // Break the Python/Java reference cycle even on failed startup.
        if (callback != null) callback.call((Object) address);
    }

    public String getLogTail() {
        synchronized (logTail) { return String.join("\n", logTail); }
    }

    public boolean isAlive() { return process.isAlive(); }

    public String getDiagnostics() {
        synchronized (diagnostics) { return String.join("\n", diagnostics); }
    }

    public int waitForExit(int seconds) throws IOException, InterruptedException {
        if (!process.waitFor(seconds, TimeUnit.SECONDS)) throw new IOException("Native device probe timed out");
        return process.exitValue();
    }

    public int getPid() { return pid; }

    public void close() throws IOException, InterruptedException {
        process.destroy();
        if (!process.waitFor(1, TimeUnit.SECONDS)) {
            synchronized (process) {
                if (process.isAlive()) android.os.Process.killProcess(pid);
            }
            if (!process.waitFor(2, TimeUnit.SECONDS)) {
                throw new IOException("Android native model process did not exit");
            }
        }
        output.close();
        ready(null);
    }
}
