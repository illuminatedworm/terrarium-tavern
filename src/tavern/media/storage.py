"""Durable media intents and assets. Only the story commit publishes new intents."""

import hashlib
import json
import time

from tavern.character.cards import strip_card_png_data
from tavern.core.errors import NotFound
from tavern.media.config import (
    GeminiSpeechProfile,
    MediaConfig,
    canonical_voice,
    image_reference_limit,
    regenerate_image_size,
    validate_voice,
)
from tavern.media.routing import (
    ModelKey,
    StoryMediaSettings,
    VoiceTable,
    story_key,
    upgrade_selection,
    validate_channels,
)


def initialize(store):
    with store.connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS media_assets(
                id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                message TEXT, ref INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
                description TEXT NOT NULL, mime TEXT NOT NULL, blob TEXT NOT NULL,
                size INTEGER NOT NULL, selected INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                UNIQUE(session,ref));
            CREATE INDEX IF NOT EXISTS media_assets_message ON media_assets(session,message);
            CREATE INDEX IF NOT EXISTS media_assets_selected ON media_assets(session,selected,kind);
            CREATE TABLE IF NOT EXISTS media_jobs(
                id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                message TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
                request TEXT NOT NULL, asset TEXT, previous TEXT, error TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL, updated REAL NOT NULL, seq INTEGER NOT NULL, ordinal INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS media_jobs_message ON media_jobs(session,message);
            CREATE INDEX IF NOT EXISTS media_jobs_queue ON media_jobs(status,created);
            CREATE TABLE IF NOT EXISTS media_actions(
                session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                operation TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
                job TEXT NOT NULL REFERENCES media_jobs(id) ON DELETE CASCADE,
                PRIMARY KEY(session,operation));
        """)


def next_sequence(db, sid):
    return db.execute(
        "UPDATE sessions SET media_sequence=media_sequence+1 WHERE id=? RETURNING media_sequence", (sid,)
    ).fetchone()[0]


def selection_row(db, sid):
    row = db.execute(
        "SELECT media_config,model_selection,channel_origin FROM sessions WHERE id=?", (sid,)
    ).fetchone()
    if row is None:
        raise NotFound("会话不存在")
    return row


def config(store, sid):
    with store.connection() as db:
        row = selection_row(db, sid)
        document = store.settings_document(db=db)
        return StoryMediaSettings.model_validate_json(row["media_config"]).resolve(
            document.story.media, story_key(row, document)
        )


def editable(db, sid, revision):
    from tavern.storage.store import Conflict

    if db.execute(
        "SELECT 1 FROM context_operations WHERE session=? AND status IN ('queued','running')", (sid,)
    ).fetchone():
        raise Conflict("请先完成或取消上下文整理")

    if db.execute(
        "SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')", (sid,)
    ).fetchone():
        raise Conflict("请先完成或取消当前剧情回合")
    if (
        db.execute(
            "UPDATE sessions SET revision=revision+1 WHERE id=? AND revision=?", (sid, revision)
        ).rowcount
        != 1
    ):
        raise Conflict("会话已改变，请刷新后重试")


LOCAL_HTTP_ADAPTERS = ("gemini_tts", "gemini_image", "openai_speech", "openai_images")


def check_media_key(provider, adapter):
    if provider.auth_mode == "chatgpt_subscription":
        raise ValueError("媒体服务需要 API Key 连接，不能使用 ChatGPT 登录订阅")
    if not provider.api_key:
        if provider.is_loopback and adapter in LOCAL_HTTP_ADAPTERS:
            return
        raise ValueError("媒体连接尚未填写 API Key；本机免密 HTTP 服务可留空")


def connection_snapshot(settings, options):
    provider = next((p for p in settings.providers if p.id == options.provider_id), None)
    if provider is None or not options.model.strip():
        raise ValueError("请先在当前生效的媒体设置中选择连接并填写模型")
    check_media_key(provider, options.adapter)
    parameters = options.model_dump(exclude={"enabled", "voice_names"})
    if isinstance(options, GeminiSpeechProfile):
        parameters["instruction_format"] = options.effective_format()
    return {
        **parameters,
        "provider_name": provider.name,
        "base_url": provider.base_url,
        "auth_mode": provider.auth_mode,
    }


def selection_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def save_config(
    store, sid, revision, value: StoryMediaSettings | MediaConfig | dict, *, expected_selection=None
):
    from tavern.storage.store import Conflict, dump

    # Credentials remain in the existing provider settings, never in media jobs or exports.
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        editable(db, sid, revision)
        document = store.settings_document(db=db)
        row = selection_row(db, sid)
        # Manual regeneration can bind a new voice while a story turn owns its revision.
        if expected_selection is not None and expected_selection != selection_token(row["media_config"]):
            raise Conflict("角色表或媒体设置已改变，请重新载入")
        text_model = story_key(row, document)
        if not isinstance(value, StoryMediaSettings):
            raw = value.model_dump() if isinstance(value, MediaConfig) else value
            value = upgrade_selection(
                raw, row, document, StoryMediaSettings.model_validate_json(row["media_config"])
            )
        for part in (value.speech, value.image):
            if part.custom:
                validate_channels(part.custom, document.shared.providers, allow_missing=True)
        effective = value.resolve(document.story.media, text_model)
        for options in (effective.speech, effective.image):
            if options.enabled:
                connection_snapshot(document.shared, options)
        if effective.speech.enabled:
            validate_voice(effective.speech, effective.speech.default_voice)
            for voice in set(effective.voices.values()):
                validate_voice(effective.speech, voice)
        db.execute("UPDATE sessions SET media_config=? WHERE id=?", (dump(value.model_dump()), sid))
        sequence = next_sequence(db, sid)
        for kind, enabled in (("speech", value.speech.enabled), ("image", value.image.enabled)):
            if not enabled:
                db.execute(
                    "UPDATE media_jobs SET status='cancelled',error='此故事已关闭生成',updated=?,seq=? "
                    "WHERE session=? AND kind=? AND status='queued'",
                    (time.time(), sequence, sid, kind),
                )
    store.notify_media(sid)
    return value


def asset_view(row):
    return {
        key: row[key]
        for key in (
            "id",
            "session",
            "message",
            "ref",
            "kind",
            "name",
            "description",
            "mime",
            "size",
            "selected",
            "created",
        )
    } | {"ref": "I" + str(row["ref"])}


def assets(store, sid):
    with store.connection() as db:
        return [
            dict(r) for r in db.execute("SELECT * FROM media_assets WHERE session=? ORDER BY ref", (sid,))
        ]


def image_catalog(store, sid, message_ids, *, selected=False, unbound=False):
    """Fetch only relevant image metadata, never every voice clip in a long story."""
    ids = list(message_ids)
    fixed = " OR ".join(
        part for part, enabled in (("selected=1", selected), ("message IS NULL", unbound)) if enabled
    )
    result = []
    with store.connection() as db:
        if fixed:
            result.extend(
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM media_assets WHERE session=? AND kind='image' AND ({fixed})", (sid,)
                )
            )
        for offset in range(0, len(ids), 400):
            batch = ids[offset : offset + 400]
            # Stay within Android SQLite's parameter limit even for whole-history reads.
            result.extend(
                dict(row)
                for row in db.execute(
                    "SELECT * FROM media_assets WHERE session=? AND kind='image' "
                    + (f"AND NOT ({fixed}) " if fixed else "")
                    + f"AND message IN ({','.join('?' for _ in batch)})",
                    (sid, *batch),
                )
            )
    return sorted(result, key=lambda item: item["ref"])


def asset_by_ref(store, sid, ref):
    import re

    if not re.fullmatch(r"I[1-9]\d*", ref):
        raise ValueError("请选择目录中的图片编号")
    with store.connection() as db:
        row = db.execute(
            "SELECT * FROM media_assets WHERE session=? AND ref=?", (sid, int(ref[1:]))
        ).fetchone()
    if row is None or row["kind"] != "image":
        raise ValueError(f"图片 {ref} 不存在或不属于此故事")
    return dict(row)


def add_asset(store, db, sid, *, data, kind, mime, name, description="", message=None, selected=False):
    from tavern.storage.store import uid

    if mime == "image/png":
        data = strip_card_png_data(data)
    identity = uid()
    ref = db.execute("SELECT coalesce(max(ref),0)+1 FROM media_assets WHERE session=?", (sid,)).fetchone()[0]
    blob = store.blob(data, db=db)
    db.execute(
        "INSERT INTO media_assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            identity,
            sid,
            message,
            ref,
            kind,
            name,
            description,
            mime,
            blob,
            len(data),
            int(selected),
            time.time(),
        ),
    )
    return identity


def create_job(db, sid, mid, kind, request, *, previous=None):
    from tavern.storage.store import dump, uid

    identity, now = uid(), time.time()
    ordinal = db.execute(
        "SELECT coalesce(max(ordinal),0)+1 FROM media_jobs WHERE session=? AND message=?", (sid, mid)
    ).fetchone()[0]
    db.execute(
        "INSERT INTO media_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            identity,
            sid,
            mid,
            kind,
            "queued",
            dump(request),
            None,
            previous,
            "",
            now,
            now,
            next_sequence(db, sid),
            ordinal,
        ),
    )
    return identity


def bind_speaker(selection, tables, story, request):
    conn = request["connection"]
    speech = ModelKey(provider_id=conn["provider_id"], model=conn["model"])
    key = (story, speech)
    table = tables.get(key)
    if table is None:
        table = VoiceTable(story_model=story, speech_model=speech, voices={})
        selection.voice_tables.append(table)
        tables[key] = table
    if request["speaker"] in table.voices:
        return False
    table.voices[request["speaker"]] = canonical_voice(conn["adapter"], request["voice"])
    return True


def commit(db, sid, mid, draft):
    if not draft or not draft["plans"]:
        return
    # Freeze the first voice in the transaction that accepts the story and its media jobs.
    row = selection_row(db, sid)
    selection = StoryMediaSettings.model_validate_json(row["media_config"])
    tables = {(t.story_model, t.speech_model): t for t in selection.voice_tables}
    changed = False
    legacy_story = None
    for plan in draft["plans"]:
        kind, request = plan["kind"], plan["request"]
        jid = create_job(db, sid, mid, kind, request)
        if not getattr(selection, kind).enabled:
            db.execute(
                "UPDATE media_jobs SET status='cancelled',error='此故事已关闭生成，未发起请求' WHERE id=?",
                (jid,),
            )
        if kind == "speech":
            if "story_model" in request:
                story = ModelKey.model_validate(request["story_model"])
            else:
                # Only pending turns saved before v12 lack a text-model identity.
                if legacy_story is None:
                    from tavern.core.settings import SettingsDocument

                    doc = SettingsDocument.model_validate_json(
                        db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]
                    )
                    legacy_story = story_key(row, doc)
                story = legacy_story
            changed |= bind_speaker(selection, tables, story, request)
    if changed:
        db.execute("UPDATE sessions SET media_config=? WHERE id=?", (selection.model_dump_json(), sid))


def _job_view(db, row):
    result = dict(row)
    result["request"] = json.loads(result["request"])
    result["output"] = (
        asset_view(db.execute("SELECT * FROM media_assets WHERE id=?", (result["asset"],)).fetchone())
        if result["asset"]
        else None
    )
    return result


def _job_row(db, sid, jid):
    row = db.execute("SELECT * FROM media_jobs WHERE id=? AND session=?", (jid, sid)).fetchone()
    if row is None:
        raise NotFound("媒体任务不存在")
    return row


def job(store, sid, jid):
    with store.connection() as db:
        return _job_view(db, _job_row(db, sid, jid))


def jobs(store, sid, message_ids):
    if not message_ids:
        return []
    with store.connection() as db:
        rows = db.execute(
            f"SELECT * FROM media_jobs WHERE session=? AND message IN ({','.join('?' for _ in message_ids)}) "
            "ORDER BY ordinal",
            (sid, *message_ids),
        ).fetchall()
        return [_job_view(db, row) for row in rows]


def copy_media(db, source, destination, ids):
    """References keep their short names in forks; bytes are immutable and shared."""
    from tavern.storage.store import dump, insert_row, uid

    source_jobs, messages = [], list(ids)
    for offset in range(0, len(messages), 400):
        batch = messages[offset : offset + 400]
        source_jobs.extend(
            dict(row)
            for row in db.execute(
                f"SELECT * FROM media_jobs WHERE session=? AND message IN ({','.join('?' for _ in batch)})",
                (source, *batch),
            )
        )
    needed = {ref for row in source_jobs for ref in json.loads(row["request"]).get("references", [])}
    source_assets = db.execute("SELECT * FROM media_assets WHERE session=?", (source,)).fetchall()
    if not source_assets and not source_jobs:
        return
    seen_refs = set()
    if source != destination and any(row["kind"] == "image" for row in source_assets):
        # Project only the small image receipts. Loading and decoding complete
        # worldbook/tool payloads again would double the cost of story forks.
        for trace in db.execute(
            "SELECT message_id,json_extract(payload,'$._media_image_refs'),"
            "json_extract(payload,'$._media_catalog_refs') FROM story_trace "
            "WHERE session=? AND (json_type(payload,'$._media_image_refs')='array' "
            "OR json_type(payload,'$._media_catalog_refs')='array')",
            (source,),
        ):
            if trace[0] in ids:
                for references in trace[1:]:
                    if references is not None:
                        seen_refs.update(json.loads(references))
    asset_ids = {}
    for row in source_assets:
        item = dict(row)
        if source == destination and item["message"] not in ids:
            continue
        if source != destination and not (
            item["message"] in ids
            or item["selected"]
            or item["id"] in needed
            or "I" + str(item["ref"]) in seen_refs
        ):
            continue
        old = item["id"]
        item.update(id=uid(), session=destination, message=ids.get(item["message"]))
        if source == destination:
            item["ref"] = db.execute(
                "SELECT coalesce(max(ref),0)+1 FROM media_assets WHERE session=?", (destination,)
            ).fetchone()[0]
        insert_row(db, "media_assets", item)
        asset_ids[old] = item["id"]
    job_ids = {row["id"]: uid() for row in source_jobs}
    for row in source_jobs:
        item = dict(row)
        request = json.loads(item["request"])
        if "references" in request:
            request["references"] = [asset_ids.get(a, a) for a in request["references"]]
        item.update(
            id=job_ids[row["id"]],
            session=destination,
            message=ids[item["message"]],
            asset=asset_ids.get(item["asset"]),
            request=dump(request),
            previous=job_ids.get(row["previous"]),
        )
        if item["status"] in ("queued", "running"):
            item.update(status="interrupted", error="复制进度未重复发起付费生成；可手动重试")
        insert_row(db, "media_jobs", item)


def retry(store, sid, jid, regenerate=False, operation_id=None):
    from tavern.storage.store import Conflict

    action = "regenerate" if regenerate else "retry"
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if operation_id:
            repeated = db.execute(
                "SELECT * FROM media_actions WHERE session=? AND operation=?", (sid, operation_id)
            ).fetchone()
            if repeated:
                if repeated["source"] != jid or repeated["kind"] != action:
                    raise Conflict("操作编号已用于另一个媒体请求")
                return _job_view(db, _job_row(db, sid, repeated["job"]))
        original = _job_row(db, sid, jid)
        if original["status"] in ("queued", "running"):
            raise Conflict("此媒体任务仍在进行")
        # The feature switch and job status must be read under the same lock
        # that saves configuration and enqueues manual operations.
        row = selection_row(db, sid)
        selection = StoryMediaSettings.model_validate_json(row["media_config"])
        document = store.settings_document(db=db)
        request = json.loads(original["request"])
        text_model = (
            ModelKey.model_validate(request["story_model"])
            if "story_model" in request
            else story_key(row, document)
        )
        value = selection.resolve(document.story.media, text_model)
        options = getattr(value, original["kind"])
        if not options.enabled:
            raise ValueError("请先开启此故事的媒体功能")
        if regenerate:
            if original["kind"] == "speech":
                request["story_model"] = text_model.model_dump()
                request["voice"] = value.voices.get(request["speaker"], options.default_voice)
                validate_voice(options, request["voice"])
            else:
                if len(request["references"]) > image_reference_limit(options):
                    raise ValueError("原任务的参考图数量超过当前生图模型上限")
                request["size"] = regenerate_image_size(request, options)
            request["connection"] = connection_snapshot(document.shared, options)
            if original["kind"] == "speech" and request["speaker"] not in value.voices:
                tables = {(t.story_model, t.speech_model): t for t in selection.voice_tables}
                bind_speaker(selection, tables, text_model, request)
                db.execute(
                    "UPDATE sessions SET media_config=? WHERE id=?", (selection.model_dump_json(), sid)
                )
            new_id = create_job(db, sid, original["message"], original["kind"], request, previous=jid)
        else:
            if original["status"] == "completed":
                raise ValueError("此任务已完成；需要新产物请使用重新生成")
            new_id = jid
            db.execute(
                "UPDATE media_jobs SET status='queued',error='',updated=?,seq=? WHERE id=?",
                (time.time(), next_sequence(db, sid), jid),
            )
        if operation_id:
            db.execute(
                "INSERT INTO media_actions VALUES (?,?,?,?,?)",
                (sid, operation_id, jid, action, new_id),
            )
    store.notify_media(sid)
    return job(store, sid, new_id)
