"""Transfer story-specific options, without archiving disposable consultations."""
import json

from tavern.advisor.runtime import OptionsRequest
from tavern.storage.store import dump


def export_bundle(db, sid):
    row = db.execute("SELECT options FROM advisor_conversations WHERE session=?", (sid,)).fetchone()
    if not row:
        return None
    return {"options": json.loads(row[0]), "exchanges": []}


def validate_bundle(value, channel):
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"options", "exchanges"} or not isinstance(value["exchanges"], list):
        raise ValueError("参谋 agent 导出数据格式无效")
    options = OptionsRequest.model_validate({"expected_revision": 0, **value["options"]})
    if options.context_strategy == "openai_compact" and channel != "chatgpt":
        raise ValueError("此渠道不能导入参谋 agent 云端上下文配置")
    for row in value["exchanges"]:
        if not isinstance(row, dict) or set(row) != {"user_text", "response", "story_head", "created", "trace", "story_sources"}:
            raise ValueError("参谋 agent 咨询记录格式无效")
        if any(not isinstance(row[key], str) for key in ("user_text", "response")) or not isinstance(row["trace"], list) or not isinstance(row["story_sources"], dict):
            raise ValueError("参谋 agent 咨询记录内容无效")
        if row["story_head"] is not None and not isinstance(row["story_head"], str):
            raise ValueError("参谋 agent 剧情引用无效")
        if type(row["created"]) not in (int, float) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in row["story_sources"].items()):
            raise ValueError("参谋 agent 来源记录无效")
        from tavern.conversation.compaction import replay_groups
        if any(not isinstance(m, dict) or m.get("role") not in {"assistant", "tool"} or not isinstance(m.get("content", ""), str) for m in row["trace"]):
            raise ValueError("参谋 agent 工具过程无效")
        try:
            replay_groups(row["trace"])
        except (TypeError, KeyError, AttributeError, ValueError):
            raise ValueError("参谋 agent 工具过程不完整") from None


def import_bundle(db, sid, value, ids):
    if value is None:
        return
    db.execute("INSERT INTO advisor_conversations(session,options) VALUES (?,?)", (sid, dump(value["options"])))
    # Older packages can contain consultations; do not revive them in the new story.
