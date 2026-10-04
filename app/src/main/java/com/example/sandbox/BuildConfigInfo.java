package com.example.sandbox;

import android.content.Context;
import android.content.pm.PackageManager;

final class BuildConfigInfo {
    private BuildConfigInfo() {}

    static String version(Context context) {
        try {
            return context.getPackageManager().getPackageInfo(context.getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException e) {
            return "?";
        }
    }
}
