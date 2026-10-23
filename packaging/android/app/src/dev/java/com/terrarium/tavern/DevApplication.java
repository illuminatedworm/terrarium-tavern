package com.terrarium.tavern;

import android.app.Application;
import android.webkit.WebView;

/** Debugging belongs to the visibly separate dev app, never to a personal APK. */
public final class DevApplication extends Application {
    @Override public void onCreate() {
        super.onCreate();
        if (Application.getProcessName().equals(getPackageName())) {
            WebView.setDataDirectorySuffix("host");
            WebView.setWebContentsDebuggingEnabled(true);
        }
    }
}
