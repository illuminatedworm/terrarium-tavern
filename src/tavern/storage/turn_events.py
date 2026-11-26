"""SQLite projections for the live reader and paged work log. Stored events stay lossless."""

import json

from tavern.conversation.work_records import compact_record


def reader_events(db, tid, after, limit):
    from tavern.story.activity import compact, event_activity

    # Keep a frame/sequence even for diagnostic-only events: a work-log observer
    # can refresh its cursor, and EventSource can reconnect past a large diagnostic.
    rows = db.execute(
        """SELECT e.turn,e.seq,e.kind,e.created,s.kemini,CASE
        WHEN e.kind IN ('provider_request','input_ready') AND json_extract(e.payload,'$.detail_expired')
            THEN e.payload
        WHEN e.kind='provider_request' THEN json_object('purpose',json_extract(e.payload,'$.purpose'),
            'cache',json_extract(e.payload,'$.cache'))
        WHEN e.kind='usage' AND json_type(e.payload,'$.context_input')='object'
            THEN json_object('context_input',json_extract(e.payload,'$.context_input'))
        WHEN e.kind='input_ready' THEN json_object('message',json_object(
            'id',json_extract(e.payload,'$.message.id'),'raw',json_extract(e.payload,'$.message.raw'),
            'display',json_extract(e.payload,'$.message.display'),'ooc',json_extract(e.payload,'$.message.ooc')),
            'lore_admissions',json(coalesce(json_extract(e.payload,'$.lore_admissions'),(
                SELECT json_group_array(json_object('id',json_extract(value,'$.id'),
                    'title',json_extract(value,'$.title'),'anchor_id',json_extract(value,'$.anchor_id')))
                FROM json_each(e.payload,'$.lore_injections')
                WHERE json_extract(value,'$.source_ids[#-1]')=json_extract(e.payload,'$.message.id')
                AND NOT coalesce(json_extract(value,'$.manually_removed'),0)))))
        WHEN e.kind IN ('text','text_reset','story_prefix_debug','error','kemini_auxiliary',
            'story_assistant','story_tool_started','story_tool','story_runtime') THEN e.payload
        ELSE '{}' END AS payload
        FROM events e JOIN turns t ON t.id=e.turn JOIN sessions s ON s.id=t.session
        WHERE e.turn=? AND e.seq>? ORDER BY e.seq LIMIT ?""",
        (tid, after, limit),
    )
    events = []
    for row in rows:
        event = dict(row)
        visuals = not event.pop("kemini")
        event["payload"] = json.loads(event["payload"])
        if event["payload"].get("detail_expired"):
            # Retention kept a summary at this sequence, with no replayable activity or input.
            event["kind"] = "detail_expired"
        item = event_activity(
            event,
            with_details=visuals
            and event["kind"] in ("story_tool_started", "story_tool")
            and event["payload"].get("name") == "present_ui",
        )
        if item is not None:
            event.update(kind="story_activity", payload=compact([item], visuals=visuals)[0])
        events.append(event)
    return events


def work_page(db, tid, after, limit):
    # Extract only label inputs. In particular neither provider request arrays nor
    # historical input snapshots need to enter Python for a page of descriptions.
    rows = db.execute(
        """SELECT seq,kind,created,coalesce(json_extract(payload,'$.purpose'),'runtime') AS purpose,
        length(CAST(payload AS BLOB)) AS bytes,
        CASE WHEN kind='input_ready' THEN substr(json_extract(payload,'$.message.raw'),1,180)
            WHEN kind='error' THEN json_extract(payload,'$.message')
            WHEN json_type(payload)!='object' THEN substr(payload,1,180)
            ELSE substr(coalesce(json_extract(payload,'$.message'),json_extract(payload,'$.reason')),1,180)
        END AS message, CASE WHEN kind='error' THEN json_extract(payload,'$.reason')
            ELSE substr(json_extract(payload,'$.reason'),1,180) END AS reason,
        json_extract(payload,'$.status') AS status,json_extract(payload,'$.kind') AS activity_kind,
        json_extract(payload,'$.rebuild_reason') AS rebuild_reason,json_extract(payload,'$.model') AS model,
        CASE WHEN kind='usage' THEN json_extract(payload,'$.context_input') END AS context_input,
        CASE WHEN kind='provider_request' THEN json_array_length(payload,'$.messages') END AS message_count,
        CASE WHEN kind='provider_request' THEN json_array_length(payload,'$.tools') END AS tool_count
        FROM events WHERE turn=? AND seq>? AND kind NOT IN ('text','text_reset') ORDER BY seq LIMIT ?""",
        (tid, after, limit + 1),
    ).fetchall()
    items = []
    for row in rows[:limit]:
        event = dict(row)
        payload = {
            key: event[key]
            for key in (
                "message",
                "reason",
                "status",
                "rebuild_reason",
                "model",
                "message_count",
                "tool_count",
            )
        }
        payload["kind"] = event["activity_kind"]
        if event["context_input"] is not None:
            payload["context_input"] = json.loads(event["context_input"])
        items.append(compact_record({**event, "payload": payload}))
    return {"items": items, "next": items[-1]["seq"] if len(rows) > limit else None}
