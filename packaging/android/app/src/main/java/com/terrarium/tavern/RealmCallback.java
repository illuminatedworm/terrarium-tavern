package com.terrarium.tavern;

/** Implemented by a Chaquopy dynamic proxy on the Python host process. */
public interface RealmCallback {
    void onMessage(String message);
}
