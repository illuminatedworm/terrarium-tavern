"""A read-only story snapshot and a separately managed consultation window."""
import copy

from tavern.character.cards import lore_entries, normalize
from tavern.conversation.context import macros
from tavern.core.cache_policy import fingerprint
from tavern.core.channels import require_channel
from tavern.core.message_adapter import project_context
from tavern.core.model_selection import ModelSelection
from tavern.core.model_view import HANDLE, ModelView
from tavern.core.tokens import counting_info, request_tokens
from tavern.memories.memory_records import read_memory, render_document, source_hash, with_source_status
from tavern.scripting.request_context import response_origin


def runtime_config(store, session, options):
    config = store.settings_document().shared.advisor.model_copy(deep=True)
    channel = session.get("channel_origin")
    if not channel:
        raise ValueError("请先确认故事的渠道归属")
    profile = config.official_model if channel == "chatgpt" else config.model
    if profile is None:
        raise ValueError("请在设置 → 参谋 agent 中配置此渠道的模型")
    if options.get("model_selection"):
        profile = ModelSelection.model_validate(options["model_selection"]).profile(profile)
    cfg = store.settings(session["agent_mode"], channel)
    cfg.advisor = profile.model_copy(deep=True)
    cfg = cfg.for_role("advisor")
    require_channel(cfg, channel)
    cfg.channel_origin = channel
    cfg.cache_scope = "advisor:" + session["id"]
    cfg.cache_window = "consultation"
    if options.get("recent_exchanges") is not None:
        config.recent_exchanges = options["recent_exchanges"]
    if options.get("context_strategy") is not None:
        config.context_strategy = options["context_strategy"]
    if channel != "chatgpt":
        config.context_strategy = "local"
    cfg.context_strategy = "openai_compact" if config.context_strategy == "openai_compact" else "new_context"
    cfg.compact_auto = config.auto_compact
    return cfg, config


def freeze_story(store, session):
    document = (session["snapshot"].get("content") or {}).get("document") or store.session_card(session["id"])["document"]
    return {key: copy.deepcopy(session.get(key)) for key in (
        "id", "revision", "head", "agent_mode", "channel_origin", "persona", "persona_data", "turn_preference",
        "plot_enabled", "context_strategy", "snapshot", "messages", "title")} | {"document": copy.deepcopy(document)}


def sources(story):
    return {m["id"]: source_hash(m) for m in story["messages"]}


def recent_history(history, count):
    if not count:
        return []
    groups = []
    for item in history:
        if not groups or item["role"] == "user":
            groups.append([])
        groups[-1].append(item)
    # An opening is not counted as a user/assistant exchange. Keep it only
    # while there are fewer exchanges than the requested range.
    completed = [group for group in groups if any(m["role"] == "assistant" for m in group)]
    return [m for group in completed[-count:] for m in group]


def memory_rows(story):
    memory = with_source_status(read_memory(story["snapshot"]), story["messages"])
    refs = {m["id"]: f"H{i+1}" for i, m in enumerate(story["messages"])}
    return [{"ref": f"R{i+1}", **{k: r[k] for k in ("category", "kind", "content", "status", "manual", "source_stale", "people", "known_to", "story_time") if k in r},
             "sources": [refs[s["message_id"]] for s in r.get("sources", []) if s.get("message_id") in refs]}
            for i, r in enumerate(memory["records"]) if r["status"] not in ("deleted", "superseded")]


def expanded(story, text, message=None):
    snap = (message or {}).get("snapshot") or story["snapshot"]
    document = (snap.get("content") or {}).get("document") or story["document"]
    return macros(text, normalize(document), story["persona"], snap.get("state", {}), snap.get("scopes"))


def background(story, config):
    card = normalize(story["document"])
    header = [{"role": "system", "message_type": "root_system", "content": config.system_prompt}]
    for field in ("name", "description", "personality", "scenario", "system_prompt", "post_history_instructions", "mes_example"):
        if card.get(field):
            header.append({"role": "system", "content": f"<story_reference field='{field}'>\n{expanded(story, card[field])}\n</story_reference>"})
    header.append({"role": "system", "content": f"<user_character>\n{story['persona']}\n{(story.get('persona_data') or {}).get('description', '')}\n</user_character>"})
    for entry in lore_entries(story["document"], "writer"):
        if entry["enabled"] and entry.get("constant") and "[initvar]" not in entry.get("comment", "").lower():
            header.append({"role": "system", "content": "<lorebook>\n" + expanded(story, entry["content"]) + "\n</lorebook>"})
    dynamic = [{"role": "system", "content": "<turn_preference>\n" + story.get("turn_preference", "") + "\n</turn_preference>"}]
    full = story["snapshot"].get("full_agent", {})
    if story["agent_mode"] == "full_agent":
        handoff = full.get("handoff", {})
        if handoff.get("text") and story.get("context_strategy") != "openai_compact":
            dynamic.append({"role": "system", "content": "<handoff stale='" + str(bool(handoff.get("stale"))) + "'>\n" + handoff["text"] + "\n</handoff>"})
    else:
        # Full records, never a token-budget abridgement of selected memory.
        rendered = render_document({"records": memory_rows(story)})
        if rendered:
            dynamic.append({"role": "system", "content": "<story_memory>\n" + rendered + "\n</story_memory>"})
    recent = recent_history(story["messages"], config.recent_exchanges)
    refs = {m["id"]: f"H{i+1}" for i, m in enumerate(story["messages"])}
    for message in recent:
        for entry in story["snapshot"].get("lore_injections", []):
            if entry.get("anchor_id") == message["id"] and not entry.get("manually_removed"):
                dynamic.append({"role": "system", "content": "<lorebook>\n" + entry["content"] + "\n</lorebook>"})
        dynamic.append({"role": "system", "content": f"<story_history ref='{refs[message['id']]}' role='{message['role']}'>\n"
                        + expanded(story, message.get("model", message["raw"]), message) + "\n</story_history>"})
    for message in header[1:]:
        message["message_type"] = "advisor_reference"
    for message in dynamic:
        message.update(message_type="advisor_story", runtime_phase="body")
    return header, dynamic, [refs[m["id"]] for m in recent]


def context_identity(cfg, config):
    return fingerprint([1, response_origin(cfg), config.system_prompt, config.context_strategy, cfg.context_tokens,
                        cfg.message_adapter.model_dump()])


def consultation_body(exchanges, story):
    current = sources(story)
    body = []
    for exchange in exchanges:
        outdated = any(current.get(mid) != value for mid, value in exchange["story_sources"].items())
        body.append({"role": "user", "content": exchange["user_text"]})
        if outdated:
            body.append({"role": "system", "message_type": "advisor_notice", "content": "以下旧咨询基于已变更的剧情，仅供回顾讨论；不能当作当前剧情事实。"})
        body.extend(copy.deepcopy(exchange["trace"]))
    return body


def compile_context(story, config, cfg, exchanges, context, pending, definitions, view=None):
    if view is None:
        import json
        view = ModelView()
        view.reserved = set(context.get("reserved", [])) | set(HANDLE.findall(json.dumps([story, exchanges, context.get("summary", ""), pending])))
        for raw in context.get("view", []):
            view.bind(raw)
    header, dynamic, refs = background(story, config)
    identity = context_identity(cfg, config)
    current_sources = sources(story)
    usable = context.get("identity") == identity and all(current_sources.get(mid) == value for mid, value in context.get("sources", {}).items())
    # Appending new story turns keeps the older consultation summary valid as
    # discussion history. Editing/removing its source forces reconstruction.
    selected = exchanges
    if usable and context.get("through"):
        selected = [e for e in exchanges if e["seq"] > context["through"]]
    body = consultation_body(selected, story)
    if usable and context.get("summary"):
        header.append({"role": "system", "message_type": "advisor_summary", "content": "<consultation_summary>\n" + context["summary"] + "\n</consultation_summary>"})
    portable_messages = view.messages(header + dynamic + body + pending)
    if usable and context.get("checkpoint"):
        from tavern.conversation.compaction import opaque_message
        portable_messages = view.messages(header + dynamic) + [opaque_message(context["checkpoint"], cfg)] + view.messages(body + pending)
    tokens = request_tokens(portable_messages, cfg, definitions)
    result = {"messages": portable_messages, "trace": portable_messages, "estimated_tokens": tokens, "context_limit": cfg.context_tokens,
            "token_counting": counting_info(cfg), "recent_refs": refs, "recent_exchanges": config.recent_exchanges,
            "summary": context.get("summary", "") if usable else "", "context_valid": usable,
            "opaque": bool(usable and context.get("checkpoint")), "tools": definitions}
    return project_context(result, cfg, tools=definitions)
