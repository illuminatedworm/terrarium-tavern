package com.terrarium.tavern;

/** Card Author validation and dependency bundling, in :realm_tools apart from story scripts. */
public final class ToolRealmService extends RealmService {
    @Override protected String dataDirectorySuffix() { return "realm_tools"; }
}
