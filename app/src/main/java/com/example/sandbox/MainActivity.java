package com.example.sandbox;

import android.app.Activity;
import android.os.Bundle;
import android.widget.TextView;

public class MainActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        TextView text = new TextView(this);
        text.setText("Hello from the release sandbox " + BuildConfigInfo.version(this));
        text.setPadding(48, 48, 48, 48);
        setContentView(text);
    }
}
