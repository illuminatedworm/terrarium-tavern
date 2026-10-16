"""Test-process tweaks shared by every suite."""
import sqlite3

_connect = sqlite3.connect


def _connect_without_fsync(*args, **kwargs):
    # Each test owns a throwaway store, so crash durability is irrelevant here.
    # Skipping fsync roughly halves backend runtime and removes most disk bursts.
    db = _connect(*args, **kwargs)
    db.execute("PRAGMA synchronous=OFF")
    return db


sqlite3.connect = _connect_without_fsync
