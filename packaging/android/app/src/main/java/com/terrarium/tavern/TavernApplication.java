package com.terrarium.tavern;

import android.app.Application;
import android.webkit.WebView;

public final class TavernApplication extends Application {
    @Override public void onCreate() {
        super.onCreate();
        if (Application.getProcessName().equals(getPackageName())) {
            WebView.setDataDirectorySuffix("host");
        }
        // Auxiliary services start only their own runtime; never start the story server here.
    }
}
