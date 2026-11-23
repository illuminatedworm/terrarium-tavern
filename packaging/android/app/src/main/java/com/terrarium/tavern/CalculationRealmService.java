package com.terrarium.tavern;

/** Only one calculation owns this disposable renderer at a time. */
public final class CalculationRealmService extends RealmService {
    @Override protected String dataDirectorySuffix() { return "realm_calculation"; }
}
