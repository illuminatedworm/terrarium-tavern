package com.terrarium.tavern;

import android.app.Activity;
import android.os.Bundle;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.TextView;

/** Keep instrumentation visible so vendor background freezers do not pause the whole UID. */
public final class DevSmokeActivity extends Activity {
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        TextView message = new TextView(this);
        message.setGravity(Gravity.CENTER);
        message.setTextSize(18);
        message.setText("Terrarium Tavern 开发自检\n\n正在验证，请保持此页面在前台。\n完成后会自动关闭。");
        setContentView(message);
    }
}
