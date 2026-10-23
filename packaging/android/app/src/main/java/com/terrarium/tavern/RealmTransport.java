package com.terrarium.tavern;

import android.os.Bundle;
import android.os.Message;
import android.os.Messenger;
import android.os.ParcelFileDescriptor;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.Consumer;

/** Binder only carries descriptors: card documents, history and WASM exceed its 1 MiB limit. */
final class RealmTransport {
    // One thread per direction keeps messages in send order: a page error must reach Python
    // before the result that follows it. Separate threads keep a blocked pipe writer from
    // stalling reads, and a busy card cannot create an unbounded number of host threads.
    private static final ExecutorService READERS = Executors.newSingleThreadExecutor();
    private static final ExecutorService WRITERS = Executors.newSingleThreadExecutor();

    static void send(Messenger target, Messenger reply, String json, Consumer<Exception> failure) {
        WRITERS.execute(() -> {
            ParcelFileDescriptor[] pipe = null;
            try {
                pipe = ParcelFileDescriptor.createPipe();
                Bundle data = new Bundle();
                data.putParcelable("pipe", pipe[0]);
                Message message = Message.obtain(null, 1);
                message.setData(data);
                message.replyTo = reply;
                target.send(message);
                pipe[0].close();
                try (ParcelFileDescriptor.AutoCloseOutputStream output =
                        new ParcelFileDescriptor.AutoCloseOutputStream(pipe[1])) {
                    output.write(json.getBytes(StandardCharsets.UTF_8));
                }
            } catch (Exception error) {
                if (pipe != null) for (ParcelFileDescriptor end : pipe) {
                    try { end.close(); } catch (IOException ignored) { /* Peer already closed. */ }
                }
                failure.accept(error);
            }
        });
    }

    @SuppressWarnings("deprecation")
    static void read(Message message, Consumer<String> consumer, Consumer<Exception> failure) {
        ParcelFileDescriptor descriptor = message.getData().getParcelable("pipe");
        READERS.execute(() -> {
            try (ParcelFileDescriptor.AutoCloseInputStream input =
                    new ParcelFileDescriptor.AutoCloseInputStream(descriptor)) {
                ByteArrayOutputStream buffer = new ByteArrayOutputStream();
                byte[] block = new byte[32768];
                int count;
                while ((count = input.read(block)) != -1) buffer.write(block, 0, count);
                consumer.accept(buffer.toString(StandardCharsets.UTF_8.name()));
            } catch (Exception error) {
                failure.accept(error);
            }
        });
    }

    private RealmTransport() {}
}
