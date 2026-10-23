package com.terrarium.tavern;

import android.app.Activity;
import android.app.Instrumentation;
import android.os.Bundle;
import android.system.Os;
import android.util.Log;
import com.chaquo.python.Python;

/** Runs on the instrumentation thread while the main Looper serves real realm WebViews. */
public final class DevSmokeInstrumentation extends Instrumentation {
    private String suite;

    @Override public void onCreate(Bundle arguments) {
        super.onCreate(arguments);
        suite = arguments == null ? "all" : arguments.getString("suite", "all");
        start();
    }

    @Override public void onStart() {
        Bundle result = new Bundle();
        int code = Activity.RESULT_CANCELED;
        try {
            Os.setenv("TAVERN_PLATFORM", "android", true);
            Python.start(new com.chaquo.python.android.AndroidPlatform(getTargetContext()));
            String report = Python.getInstance().getModule("tavern_dev_smoke")
                    .callAttr("run", getTargetContext().getCacheDir().getAbsolutePath(), suite).toString();
            result.putString("report", report);
            code = Activity.RESULT_OK;
        } catch (Exception failure) {
            // Preserve the actual Python/Java exception instead of reducing it to an HTTP 500.
            String error = Log.getStackTraceString(failure);
            Log.e("TavernDev", error);
            result.putString("error", error);
        }
        finish(code, result);
    }
}
