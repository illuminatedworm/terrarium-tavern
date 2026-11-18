"""Durable play cache statistics. Raw provider usage stays in the work records."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from functools import cache
from typing import TYPE_CHECKING

from tavern.core.cache_policy import fingerprint
from tavern.core.channels import provider_channel
from tavern.core.model_selection import select_runtime
from tavern.core.usage import aggregate_cache_usage
from tavern.scripting.request_context import response_origin

if TYPE_CHECKING:
    from sqlite3 import Connection

    from tavern.core.models import Settings
    from tavern.core.settings import SettingsDocument
    from tavern.core.usage import CacheUsage
    from tavern.storage.store import Store

IDLE_SECONDS = 30 * 60


def story_cache_window(snapshot: dict, strategy: str) -> str:
    state = snapshot["story"]
    checkpoint = None
    if strategy == "openai_compact":
        items = (state.get("compaction") or {}).get("items", ())
        checkpoint = next(
            (
                item["encrypted_content"]
                for item in items
                if item.get("type") == "compaction" and item.get("encrypted_content")
            ),
            None,
        )
    return fingerprint([strategy, state["window"]["id"], checkpoint])


def _load(db, sid):
    row = db.execute("SELECT payload FROM story_cache_stats WHERE session=?", (sid,)).fetchone()
    return json.loads(row[0]) if row else None


def _save(db, sid, state):
    db.execute(
        "INSERT INTO story_cache_stats(session,payload) VALUES (?,?) "
        "ON CONFLICT(session) DO UPDATE SET payload=excluded.payload",
        (sid, json.dumps(state, separators=(",", ":"))),
    )


def _segment(window):
    return {"window": window, "usage": None}


class StoryCache:
    def __init__(self, store: Store):
        self.store = store

    @contextmanager
    def _transaction(self):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def begin(
        self, sid: str, job: str, settings: Settings, snapshot: dict, *, resume: str | None = None
    ) -> None:
        now = time.time()
        origin = fingerprint(response_origin(settings))
        strategy = settings.context_strategy
        window = story_cache_window(snapshot, strategy)
        base, draft = _segment(window), None
        with self._transaction() as db:
            previous = _load(db, sid)
            if (
                previous
                and previous["origin"] == origin
                and previous["base"]["window"] == window
                and now - previous["activity"] < IDLE_SECONDS
            ):
                base = previous["base"]
                if resume == previous["job"]:
                    draft = previous["draft"]
            _save(
                db,
                sid,
                {
                    "origin": origin,
                    "strategy": strategy,
                    "base": base,
                    "draft": draft,
                    # Keep the public total fixed while individual responses accrue.
                    "before_usage": base["usage"],
                    "job": job,
                    "status": "running",
                    "activity": now,
                },
            )

    def record(self, db: Connection, sid: str, job: str, window: str, usage: dict | None) -> None:
        state = _load(db, sid)
        if state is None or state["job"] != job or state["status"] != "running":
            return
        # Old-window work is real usage even when the round fails. New-window
        # work stays separate until the snapshot that uses it is committed.
        segment = state["base"]
        if window != segment["window"]:
            if state["draft"] is None or state["draft"]["window"] != window:
                state["draft"] = _segment(window)
            segment = state["draft"]
        samples = [usage] if segment["usage"] is None else [segment["usage"], usage]
        segment["usage"] = aggregate_cache_usage(samples)
        state["activity"] = time.time()
        _save(db, sid, state)

    def finish(
        self,
        db: Connection,
        sid: str,
        job: str,
        status: str,
        *,
        snapshot: dict | None = None,
        strategy: str | None = None,
        keep_segment: bool = False,
    ) -> None:
        state = _load(db, sid)
        if state is None or state["job"] != job:
            return
        if state["status"] == "running":
            state["activity"] = time.time()
            state.pop("before_usage", None)
        state["status"] = status
        if snapshot is not None:
            window = story_cache_window(snapshot, strategy or state["strategy"])
            if keep_segment:
                # A completed compact without a usable checkpoint can leave
                # plaintext active. Its work still belongs to the current segment.
                state["base"]["window"] = window
            if window != state["base"]["window"]:
                if state["draft"] is None or state["draft"]["window"] != window:
                    state["draft"] = _segment(window)
                if status == "committed":
                    state["base"], state["draft"] = state["draft"], None
            else:
                state["draft"] = None
        _save(db, sid, state)

    def recover(self, db: Connection) -> None:
        # A hard stop cannot provide an end time. Preserve the last durable
        # activity as the idle origin instead of treating restart as activity.
        db.execute(
            "UPDATE story_cache_stats SET payload=json_remove(json_set(payload,'$.status','interrupted'),"
            "'$.before_usage') "
            "WHERE json_extract(payload,'$.status')='running'"
        )

    def reset(self, db: Connection, sid: str) -> None:
        db.execute("DELETE FROM story_cache_stats WHERE session=?", (sid,))

    def change_origin(self, db: Connection, sid: str, settings: Settings) -> None:
        state = _load(db, sid)
        if state is not None and state["origin"] != fingerprint(response_origin(settings)):
            self.reset(db, sid)

    def settings_changed(self, db: Connection, document: SettingsDocument) -> None:
        # Resolve each distinct connection once, using the document being saved
        # in this transaction. Other connections would still read the old settings.
        @cache
        def channel_settings(channel):
            return self.store.settings(channel, document=document)

        @cache
        def origin(channel, provider_id, model):
            settings = channel_settings(channel)
            if provider_id is not None:
                provider = next((p for p in settings.providers if p.id == provider_id), None)
                if provider is None or provider_channel(provider) != channel:
                    return None  # A restored settings document can retire a local selection.
                settings = select_runtime(
                    settings.model_copy(), "story", {"provider_id": provider_id, "model": model}, channel
                )
            return fingerprint(response_origin(settings))

        rows = db.execute(
            """SELECT s.id, s.channel_origin, json_extract(s.model_selection,'$.provider_id') AS provider,
                      json_extract(s.model_selection,'$.model') AS model, json_extract(c.payload,'$.origin') AS origin
               FROM story_cache_stats c JOIN sessions s ON s.id=c.session"""
        )
        changed = [
            (row["id"],)
            for row in rows
            if origin(row["channel_origin"], row["provider"], row["model"]) != row["origin"]
        ]
        db.executemany("DELETE FROM story_cache_stats WHERE session=?", changed)

    def read(self, sid: str, settings: Settings, snapshot: dict) -> tuple[CacheUsage | None, float | None]:
        with self.store.connection() as db:
            state = _load(db, sid)
        if (
            state is None
            or state["origin"] != fingerprint(response_origin(settings))
            or state["base"]["window"] != story_cache_window(snapshot, settings.context_strategy)
        ):
            return None, None
        if state["status"] == "running":
            # A schema-31 job interrupted during upgrade has no published baseline.
            return state.get("before_usage"), None
        expires = state["activity"] + IDLE_SECONDS
        if time.time() >= expires:
            return None, None
        segment = (state["draft"] if state["status"] == "awaiting_review" else None) or state["base"]
        return segment["usage"], expires if segment["usage"] is not None else None
