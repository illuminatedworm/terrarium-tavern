package com.terrarium.tavern;

import android.app.Activity;
import android.app.Instrumentation;
import android.app.KeyguardManager;
import android.content.Intent;
import android.os.Bundle;
import android.os.PowerManager;
import android.system.Os;
import android.util.Log;
import com.chaquo.python.Python;
import org.json.JSONObject;

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
        Activity progress = null;
        try {
            if (!getTargetContext().getSystemService(PowerManager.class).isInteractive()
                    || getTargetContext().getSystemService(KeyguardManager.class).isKeyguardLocked()) {
                throw new IllegalStateException("请先解锁手机并保持亮屏，再运行开发版自检");
            }
            progress = startActivitySync(new Intent(getTargetContext(), DevSmokeActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            Os.setenv("TAVERN_PLATFORM", "android", true);
            Os.setenv("TAVERN_NATIVE_LIB_DIR", getTargetContext().getApplicationInfo().nativeLibraryDir, true);
            Python.start(new com.chaquo.python.android.AndroidPlatform(getTargetContext()));
            JSONObject report = new JSONObject(Python.getInstance().getModule("tavern_dev_smoke")
                    .callAttr("run", getTargetContext().getCacheDir().getAbsolutePath(), suite).toString());
            if (suite.equals("all") || suite.equals("restart")) {
                // Runs last: the real backend shares this process and sets its own environment.
                report.getJSONArray("checks").put(new BackendRestartProbe(this).run());
                Log.i("TavernDev", "PASS: backend_restart");
            }
            result.putString("report", report.toString());
            code = Activity.RESULT_OK;
        } catch (Exception failure) {
            // Preserve the actual Python/Java exception instead of reducing it to an HTTP 500.
            String error = Log.getStackTraceString(failure);
            Log.e("TavernDev", error);
            result.putString("error", error);
        } finally {
            Activity activity = progress;
            if (activity != null) runOnMainSync(activity::finish);
        }
        finish(code, result);
    }
}
