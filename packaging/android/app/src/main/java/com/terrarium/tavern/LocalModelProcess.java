package com.terrarium.tavern;

import com.chaquo.python.PyObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Public bridge: Chaquopy must not invoke hidden ProcessEnvironment/UNIXProcess implementations. */
public final class LocalModelProcess {
    private final Process process;
    private final BufferedReader output;
    private final ArrayDeque<String> logTail = new ArrayDeque<>();
    private PyObject readyCallback;
    private static final Pattern LISTENING = Pattern.compile("listening on (http://127\\.0\\.0\\.1:\\d+)");

    private LocalModelProcess(Process process, PyObject readyCallback) throws IOException {
        this.process = process;
        this.readyCallback = readyCallback;
        process.getOutputStream().close();
        output = new BufferedReader(new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8));
        // Do not hold a Python thread inside a blocking Java readLine call. Only the
        // one-shot readiness notification crosses back into Python.
        Thread reader = new Thread(this::readOutput, "tavern-model-output");
        reader.setDaemon(true);
        reader.start();
    }

    public static LocalModelProcess start(String[] command, String nativeDirectory, PyObject readyCallback) throws IOException {
        ProcessBuilder builder = new ProcessBuilder(command).redirectErrorStream(true);
        Map<String, String> environment = builder.environment();
        environment.keySet().removeIf(key -> key.startsWith("LLAMA_"));
        environment.put("LD_LIBRARY_PATH", nativeDirectory);
        Process process = builder.start();
        try {
            return new LocalModelProcess(process, readyCallback);
        } catch (IOException failure) {
            process.destroyForcibly();
            throw failure;
        }
    }

    private void readOutput() {
        try {
            String line;
            while ((line = output.readLine()) != null) {
                synchronized (logTail) {
                    if (logTail.size() == 40) logTail.removeFirst();
                    logTail.addLast(line);
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

    public void close() throws IOException, InterruptedException {
        process.destroy();
        if (!process.waitFor(1, TimeUnit.SECONDS)) {
            process.destroyForcibly();
            if (!process.waitFor(1, TimeUnit.SECONDS)) {
                throw new IOException("Android native model process did not exit");
            }
        }
        output.close();
        ready(null);
    }
}
