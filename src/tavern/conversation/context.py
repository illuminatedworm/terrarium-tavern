"""Roleplay Runtime context compilation: consume admitted lore and Tracker memory."""

import copy
import hashlib
import json
import re

from tavern.character.cards import lore_entries, normalize
from tavern.core.model_view import state_for_model
from tavern.core.models import ContextRecipe
from tavern.core.tokens import REPLY_PRIMING, count_text, counting_info, message_tokens


def get_path(value, path, default=None):
    if not path:
        return value
    for key in path.split("."):
        if isinstance(value, dict):
            value = value.get(key, default)
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            return default
    return value


def grams(text):
    text = re.sub(r"\s+", "", text.lower())
    return {text[i : i + 2] for i in range(max(1, len(text) - 1))}


def search(items, query, field="content", limit=8):
    q = grams(query)
    scored = []
    for item in items:
        text = str(item.get(field, "")) + " " + str(item.get("comment", ""))
        score = len(q & grams(text)) / max(1, len(q)) + (2 if query.lower() in text.lower() else 0)
        if score:
            scored.append({**item, "score": round(score, 3)})
    return sorted(scored, key=lambda x: -x["score"])[:limit]


def macros(text, character, persona, state):
    text = str(text)
    if "{{" not in text:
        return text
    # Macros feed model-bound prose too. An explicit macro must not reintroduce
    # initialization ledgers/hash-bearing internals omitted from the state block.
    state = state_for_model(state)
    replacements = {"char": character.get("nickname") or character.get("name", ""), "user": persona}

    def sub(m):
        key = m[1].strip()
        if key in replacements:
            return replacements[key]
        if key.startswith(("getvar::", "getglobalvar::")):
            value = get_path(state, key.split("::", 1)[1], None)
            return str(value) if value is not None else m[0]
        return m[0]

    return re.sub(r"\{\{([^{}]+)}}", sub, text)


def wrap_lorebook(content):
    return f"<lorebook>\n{content}\n</lorebook>"


def wrap_card_description(content):
    return f"<card_description>\n{content}\n</card_description>" if content else ""


def character_content(card):
    return "\n\n".join((str(card.get("system_prompt", "")),
                        wrap_card_description(str(card.get("description", ""))),
                        str(card.get("personality", "")), str(card.get("scenario", ""))))


def matches(key, text, entry):
    key = str(key)
    # Bare Latin ability codes require explicit word matching or a /regex/ pattern.
    short_ascii = bool(re.fullmatch(r"[a-zA-Z0-9]{1,2}", key))
    if re.fullmatch(r"[a-zA-Z]", key) and not entry.get("matchWholeWords", False):
        return False
    if (entry.get("use_regex") and not short_ascii) or (key.startswith("/") and key.rfind("/") > 0):
        # Python fallback for simple worldbook patterns. Complex JS patterns are diagnosed.
        if len(key) > 512 or re.search(r"\([^)]*[+*][^)]*\)[+*]", key):
            raise ValueError("世界书正则复杂度超限")
        pattern = key
        flags = 0 if entry.get("caseSensitive") else re.I
        if key.startswith("/"):
            end = key.rfind("/")
            pattern = key[1:end]
            flags = re.I if "i" in key[end + 1 :] else 0
        return bool(re.search(pattern, text[:100000], flags))
    if not entry.get("caseSensitive", entry.get("case_sensitive", False)):
        key, text = key.lower(), text.lower()
    if entry.get("matchWholeWords", False) or re.fullmatch(r"[a-zA-Z0-9]{1,2}", key):
        return bool(re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", text))
    return bool(key) and key in text


def lore_recovery(
    entry: dict, old: dict, turn_number: int, direct: bool, session_id: str, parent_id: str
) -> dict:
    """Compute a recovery gate without consuming or changing branch state."""
    scale = max(0, int(entry.get("cooldown", 0) or 0))
    removed_at = old.get("removed_at")
    elapsed = max(0, turn_number - removed_at) if removed_at is not None else 0
    probability = 1.0
    if removed_at is not None and scale and elapsed < 4 * scale:
        probability = 1 - 2 ** (-elapsed / scale)
    boost = 0.75 * (1 - probability) if direct else 0.0
    material = json.dumps(
        ["lore-recovery", session_id, parent_id, turn_number, entry["id"]], ensure_ascii=False
    )
    roll = (int(hashlib.sha256(material.encode()).hexdigest()[:16], 16) >> 11) / (1 << 53)
    return {
        "recovery_elapsed": elapsed if removed_at is not None else None,
        "direct_mention": direct,
        "recovery_boost": boost * 100,
        "recovery_probability": (probability + boost) * 100,
        "recovery_roll": roll * 100,
        "recovery_passed": roll < probability + boost,
    }


def activate_lore(
    document, history, user_input, prior, seed, requested=(), *,
    session_id="", turn_number=None, retained=(), recursive_sources=(),
    max_entries=None, max_chars=None, expand_content=str, recursive=True, recursion_depth=3, timeline="", retrieval=None, query=None,
):
    entries = lore_entries(document, "writer")
    retrieval_ids = {eid: i for i, eid in enumerate(retrieval["ids"])} if retrieval is not None else {}
    selected, notes, checked = {}, [], set()
    # Normalize a copy; only the final context compiler records actual usage.
    updated = {
        eid: {k: v for k, v in old.items() if k in ("present", "removed_at")} if isinstance(old, dict) else {}
        for eid, old in prior.items()
    }
    turn_number = turn_number or sum(x["role"] == "user" for x in history) + 1
    parent_id = history[-1]["id"] if history else "empty-history"
    recursive_text = "\n".join(recursive_sources)
    lore_chars, lore_count = 0, 0
    budget_priority = None

    def order(entry):
        eid = entry["id"]
        return (entry.get("insertion_order", entry.get("order", 100)),
                (0, int(eid)) if eid.isdigit() else (1, eid))

    for iteration in range(1 + (recursion_depth if recursive else 0)):
        added = False
        candidates = []
        for e in entries:
            eid = e["id"]
            if (eid in retained and not e.get("constant")) or eid in selected or eid in checked or not e["enabled"] or "[initvar]" in e.get("comment", "").lower():
                continue
            timelines = e.get("timelines", [])
            timelines = [timelines] if isinstance(timelines, str) else timelines
            if not e.get("constant") and timeline and isinstance(timelines, list) and timelines and timeline not in timelines:
                checked.add(eid)
                notes.append({"id": eid, "reason": "条目不适用于当前时间线，未注入", "timeline": timeline})
                continue
            if iteration and e.get("excludeRecursion"):
                continue
            depth = max(0, int(e.get("scan_depth", e.get("scanDepth", 4)) or 0))
            scan = (
                "\n".join(x.get("model", x.get("raw", "")) for x in (history[-depth:] if depth else []))
                + "\n"
                + user_input
            )
            if iteration:
                scan += "\n" + recursive_text
            matched_keys = [k for k in e["keys"] if matches(k, scan, e)]
            direct_keys = [k for k in e["keys"] if matches(k, user_input, e)]
            primary = bool(matched_keys)
            secondary = [matches(k, scan, e) for k in e["secondary_keys"]]
            logic = int(e.get("selectiveLogic", 0))
            secondary_ok = (
                not secondary
                or [any(secondary), not all(secondary), not any(secondary), all(secondary)][
                    logic if 0 <= logic <= 3 else 0
                ]
            )
            hybrid = iteration == 0 and eid in retrieval_ids
            if hybrid and query is not None:
                secondary = [matches(k, query, e) for k in e["secondary_keys"]]
                secondary_ok = not secondary or [any(secondary), not all(secondary), not any(secondary), all(secondary)][logic if 0 <= logic <= 3 else 0]
            reason = (
                "Tracker 授权检索"
                if eid in requested
                else "常驻"
                if e.get("constant")
                else "混合召回" if hybrid and not retrieval.get("keyword_only") else "关键词"
            )
            if not (eid in requested or e.get("constant") or ((hybrid if retrieval is not None and not iteration else primary) and secondary_ok)):
                if hybrid and not secondary_ok:
                    notes.append({"id": eid, "reason": "次级关键词条件不满足，未注入"})
                continue
            # Prefer explicit current-input subjects over broad body keywords.
            # Heading matches are a lightweight signal, not semantic understanding.
            heading = str(e.get("name", "")) + " " + e.get("content", "").split("\n", 1)[0]
            heading_keys = [k for k in e["keys"] if matches(k, heading, e)]
            # A short body-only word should not activate a named subject on its own.
            # Preserve explicit regex/whole-word author rules and simple keyword books.
            if (reason == "关键词" and heading_keys and matched_keys
                    and all(len(k) <= 2 for k in matched_keys)
                    and not any(k in heading_keys for k in matched_keys)
                    and not e.get("matchWholeWords")
                    and not (e.get("use_regex") and any(re.search(r"[\.^$*+?{}\[\]|()]", k)
                                                       for k in matched_keys))
                    and not any(k.startswith("/") and k.rfind("/") > 0 for k in matched_keys)):
                notes.append({"id": eid, "reason": "仅命中正文短关键词，未提及条目主体，未注入",
                              "matched_keys": matched_keys, "recursion_depth": iteration})
                continue
            subject = any(k in heading_keys for k in direct_keys)
            tier = 0 if eid in requested or e.get("constant") else 1 if direct_keys else 2 if not iteration else 3
            rank = (tier, -int(subject), -max((len(k) for k in direct_keys), default=0), *order(e))
            if hybrid and not retrieval.get("keyword_only"):
                rank = (1, 0, retrieval_ids[eid], *order(e))
            candidates.append((rank, e, reason, matched_keys, bool(direct_keys)))
        for rank, e, reason, matched_keys, direct in sorted(candidates, key=lambda x: x[0]):
            eid = e["id"]
            old = prior.get(eid, {})
            already_present = old.get("present", False) or bool(e.get("constant"))
            checked.add(eid)
            if budget_priority is not None and rank[:2] > budget_priority and not e.get("constant"):
                notes.append({"id": eid, "reason": "优先条目预算不足，不以较低相关条目填充剩余额度"})
                continue
            recovery = {}
            if not already_present:
                recovery = lore_recovery(e, old, turn_number, direct, session_id, parent_id)
                notes.append({"id": eid, "reason": "冷却恢复判断", **recovery})
            probability = float(e.get("probability", 100) if e.get("useProbability", True) else 100)
            roll = int(hashlib.sha256(f"{seed}:{eid}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF * 100
            if not already_present and roll >= probability:
                notes.append({"id": eid, "reason": "概率未命中", "roll": roll})
                continue
            if not already_present and not recovery["recovery_passed"]:
                notes.append({"id": eid, "reason": "冷却概率未命中", **recovery})
                continue
            content = expand_content(e.get("content", ""))
            cost = len(content) if content.strip() and not e.get("constant") else 0
            if cost and ((max_entries is not None and lore_count >= max_entries)
                         or (max_chars is not None and lore_chars + cost > max_chars)):
                if direct and rank[1] == -1:
                    budget_priority = rank[:2] if budget_priority is None else min(budget_priority, rank[:2])
                notes.append({"id": eid, "reason": "本次世界书新增条目数量或字符上限，未注入"})
                continue
            lore_chars += cost
            lore_count += bool(cost)
            selected[eid] = {**e, "reason": reason, "roll": roll, **recovery,
                             "matched_keys": matched_keys, "recursion_depth": iteration,
                             "match_source": "requested" if eid in requested else "constant" if e.get("constant")
                             else "input" if direct else "history" if not iteration else "recursive"}
            if not e.get("constant") and not e.get("preventRecursion"):
                recursive_text += "\n" + e.get("content", "")
            added = True
        if not added and not (iteration == 0 and recursive_sources):
            break
    return sorted(selected.values(), key=order), notes, updated


def compile_context(
    document,
    session,
    history,
    snapshot,
    user_input,
    settings,
    seed,
    requested=(),
    generate=None,
    *,
    enforce_budget=True,
    turn_instruction="",
    input_message_id="",
    retrieval=None,
    retrieval_protocol=True,
    read_only=False,
):
    from tavern.memories.memory import render_memory, window_history

    card = normalize(document)
    recipe = ContextRecipe.model_validate(session["recipe"])
    persona = session["persona"]
    state = snapshot.get("state", {})
    visible_history, story_summary = window_history(history, snapshot, enabled=settings.chunk_mode != "off")
    protected_memory = render_memory(snapshot, history) if len(visible_history) < len(history) else ""
    gen = generate or {}
    overrides = gen.get("overrides", {})
    history_override = overrides.get("chat_history", {})
    if "prompts" in history_override:
        visible_history = [
            {"id": f"override:{i}", "role": m["role"], "raw": m["content"], "model": m["content"]}
            for i, m in enumerate(history_override["prompts"])
        ]
    max_history = gen.get("max_chat_history", "all")
    if isinstance(max_history, int):
        visible_history = visible_history[-max_history:] if max_history > 0 else []
    if "ordered_prompts" in gen and not any(p in ("chat_history", "history") for p in gen["ordered_prompts"] if isinstance(p, str)):
        visible_history = []
    if not any(b.source == "history" and b.enabled and (not b.condition or get_path(state, b.condition)) for b in recipe.blocks):
        visible_history = []
    conversation_lore = session.get("lore_injection_mode", "st_top") == "conversation"
    input_message_id = input_message_id or f"pending:{seed}"
    turn_number = sum(x["role"] == "user" for x in history) + 1
    visible_ids = {m["id"] for m in visible_history}
    writer_ids = {e["id"] for e in lore_entries(document, "writer")}
    excluded_ids = {e["id"] for e in lore_entries(document)} - writer_ids
    constants = {e["id"] for e in lore_entries(document, "writer") if e.get("constant")}
    records = [copy.deepcopy(r) for r in snapshot.get("lore_injections", [])
               if (r["anchor_id"] in visible_ids or (r.get("manually_removed") and r["anchor_id"] == input_message_id))
               and r["id"] not in constants and r["id"] not in excluded_ids]
    retained_ids = {r["id"] for r in records}
    prior = {eid: copy.deepcopy(old) if isinstance(old, dict) else {} for eid, old in snapshot.get("lore", {}).items()}
    exiting_ids = set()
    if conversation_lore:
        for eid, old in prior.items():
            if old.get("present") and eid not in retained_ids and eid not in constants:
                old.update(present=False, removed_at=turn_number)
                exiting_ids.add(eid)
    from tavern.search.retrieval import fuse, lexical_recall
    from tavern.search.retrieval_text import bounded, query_instruction
    if retrieval is None:
        retrieval = lexical_recall(document, visible_history, bounded(user_input, settings.retrieval.input_chars),
                                   snapshot, settings, session, seed, cached_only=read_only, turn_number=turn_number)
        retrieval.pop("channel_ms", None)  # Keep pure context compilation deterministic.
        retrieval["ids"] = fuse(retrieval["rankings"], retrieval["entries"], settings.retrieval)
        if "embedding" in settings.retrieval.channels or settings.local_models.reranker_enabled:
            retrieval["diagnostics"].append({"reason": "只读编译未运行本地模型；本预览不包含完整神经召回"})
    clue_history = [{**m, "model": m.get("retrieval_query", ""), "raw": ""} for m in visible_history]
    entries, diagnostics, next_lore = activate_lore(
        document, clue_history, bounded(user_input, settings.retrieval.input_chars), prior, seed,
        set(requested) & set(retrieval["ids"]),
        retrieval=retrieval, query=retrieval["query"],
        session_id=session.get("id", ""), turn_number=turn_number,
        retained=retained_ids | exiting_ids if conversation_lore else (),
        max_entries=settings.lore_max_entries, max_chars=settings.lore_max_chars,
        recursive=False,
        timeline=get_path(state, "世界.当前时间线", ""),
        expand_content=lambda text: macros(text, card, persona, state),
    )
    diagnostics.extend(retrieval.get("diagnostics", []))
    diagnostics.extend({"id": eid, "reason": "条目离开本次正文请求，开始冷却；本次不重新注入", "removed_at": turn_number}
                       for eid in sorted(exiting_ids))

    def expand(text):
        return macros(text, card, persona, state)

    sources = {
        "rules": settings.system_prompt,
        "character": character_content(card),
        "persona": "玩家名字：" + persona + "\n" + session.get("persona_data", {}).get("description", ""),
        "examples": card.get("mes_example", ""),
        "post": card.get("post_history_instructions", ""),
        "memory": "\n".join(part for part in (story_summary, render_memory(snapshot, history)) if part),
        "state": "当前剧情状态：\n" + json.dumps(state_for_model(state), ensure_ascii=False),
        "input": user_input,
        "lore_before": "",
        "lore_after": "",
        "author_note": "",
    }
    positioned = []
    constant_entries = []
    source_lore_ids = {}
    for e in entries:
        if not expand(e.get("content", "")).strip():
            continue
        if e.get("constant"):
            constant_entries.append(e)
            continue
        if conversation_lore and not e.get("constant"):
            first_turn = not any(m["role"] == "user" for m in history)
            greeting = next((m for m in visible_history if m["role"] == "assistant" and m.get("model", m.get("raw", "")).strip()), None)
            records.append({
                "id": e["id"], "content": expand(e.get("content", "")),
                "title": str(e.get("comment") or e.get("name") or f"lore · {e['id']}"),
                "anchor_id": greeting["id"] if first_turn and greeting else input_message_id,
                "source_ids": [m["id"] for m in visible_history] + [input_message_id],
                "prevent_recursion": bool(e.get("preventRecursion")),
                "evidence": copy.deepcopy(retrieval.get("evidence", {}).get(e["id"], {})),
            })
            continue
        pos = e.get("position", 0)
        if pos in ("before_char", 0):
            sources["lore_before"] += "\n" + wrap_lorebook(e.get("content", ""))
            source_lore_ids.setdefault("lore_before", []).append(e["id"])
        elif pos in ("after_char", 1):
            sources["lore_after"] += "\n" + wrap_lorebook(e.get("content", ""))
            source_lore_ids.setdefault("lore_after", []).append(e["id"])
        elif pos in (2, 3):
            sources["author_note"] += "\n" + wrap_lorebook(e.get("content", ""))
            source_lore_ids.setdefault("author_note", []).append(e["id"])
        elif pos in (5, 6):
            positioned.append({**e, "anchor": "examples", "side": "before" if pos == 5 else "after"})
        elif pos == 7:
            outlet = e.get("outletName", "")
            if outlet:
                sources["outlet:" + outlet] = (
                    sources.get("outlet:" + outlet, "") + "\n" + wrap_lorebook(e.get("content", ""))
                )
                source_lore_ids.setdefault("outlet:" + outlet, []).append(e["id"])
            else:
                diagnostics.append({"id": e["id"], "reason": "outlet 缺少名称"})
        else:
            positioned.append(e)
    if "author_note" in history_override:
        sources["author_note"] = str(history_override["author_note"])
        source_lore_ids.pop("author_note", None)
    mapping = {
        "world_info_before": "lore_before",
        "world_info_after": "lore_after",
        "persona_description": "persona",
        "dialogue_examples": "examples",
    }
    for key, source in mapping.items():
        if key in overrides:
            sources[source] = overrides[key]
            source_lore_ids.pop(source, None)
    if sources["persona"]:
        sources["persona"] = "<user_character>\n" + sources["persona"] + "\n</user_character>"
    for key, field in (
        ("char_description", "description"),
        ("char_personality", "personality"),
        ("scenario", "scenario"),
    ):
        if key in overrides:
            card[field] = overrides[key]
    if any(k in overrides for k in ("char_description", "char_personality", "scenario")):
        sources["character"] = character_content(card)
    messages, pending_depth = [], []

    def add(role, content, source, priority=50, required=False, **extra):
        if content:
            messages.append(
                {
                    "role": role,
                    "content": expand(content),
                    "source": source,
                    "priority": priority,
                    "required": required,
                    **extra,
                }
            )

    for block in recipe.blocks:
        if not block.enabled or (block.condition and not get_path(state, block.condition)):
            continue
        if block.source == "history":
            limited = visible_history
            for msg in limited:
                add(
                    msg["role"],
                    msg.get("model", msg["raw"]),
                    "history",
                    block.priority,
                    True,
                    message_id=msg["id"],
                )
        else:
            content = sources.get(block.source, block.content)
            if block.budget and count_text(expand(content), settings) > block.budget and not block.required:
                diagnostics.append({"id": block.id, "reason": "内容块 token 预算不足，整块省略"})
                continue
            if block.depth is not None:
                pending_depth.append(
                    (block.depth, block.role, content, block.id, block.priority, block.required,
                     source_lore_ids.get(block.source, []))
                )
            else:
                add(
                    block.role,
                    content,
                    block.id,
                    block.priority,
                    block.required or (block.source == "memory" and bool(protected_memory)),
                    lore_ids=source_lore_ids.get(block.source, []),
                    **({"message_id": input_message_id} if block.source == "input" else {}),
                )
    for e in positioned:
        role = e.get("role", "system")
        if isinstance(role, int):
            role = {0: "system", 1: "user", 2: "assistant"}.get(role, "system")
        if e.get("anchor"):
            anchors = [i for i, m in enumerate(messages) if m["source"] == e["anchor"]]
            at = (
                anchors[-1] + 1
                if anchors and e["side"] == "after"
                else anchors[0]
                if anchors
                else next((i for i, m in enumerate(messages) if m["source"] == "history"), len(messages))
            )
            messages.insert(
                at,
                {
                    "role": role,
                    "content": wrap_lorebook(expand(e.get("content", ""))),
                    "source": "lore:" + e["id"],
                    "lore_ids": [e["id"]],
                    "priority": 80,
                    "required": False,
                },
            )
            continue
        if not history_override.get("with_depth_entries", True):
            continue
        pending_depth.append(
            (int(e.get("depth", 0) or 0), role, wrap_lorebook(e.get("content", "")), "lore:" + e["id"], 80, False,
             [e["id"]])
        )
    for depth, role, content, source, priority, required, lore_ids in pending_depth:
        dialogue_indices = [i for i, m in enumerate(messages) if m["source"] in ("history", "input")]
        at = (
            dialogue_indices[-depth]
            if depth and len(dialogue_indices) >= depth
            else (dialogue_indices[0] if depth and dialogue_indices else len(messages))
        )
        messages.insert(
            at,
            {
                "role": role,
                "content": expand(content),
                "source": source,
                "lore_ids": lore_ids,
                "insertion_depth": depth,
                "priority": priority,
                "required": required,
            },
        )
    if "ordered_prompts" in gen:
        reordered = [m for m in messages if m["source"] == "rules"]
        placeholders = {
            **mapping,
            "chat_history": "history",
            "user_input": "input",
            "char_description": "character",
            "char_personality": "character",
            "scenario": "character",
        }
        seen = set()
        for item in gen["ordered_prompts"]:
            if isinstance(item, str):
                individual = {
                    "char_description": "description",
                    "char_personality": "personality",
                    "scenario": "scenario",
                }
                if item in individual:
                    reordered.append(
                        {
                            "role": "system",
                            "content": expand(wrap_card_description(card.get("description", ""))
                                              if item == "char_description" else card.get(individual[item], "")),
                            "source": item,
                            "priority": 100,
                            "required": False,
                        }
                    )
                    continue
                source = placeholders.get(item, item)
                if source not in seen:
                    reordered.extend(m for m in messages if m["source"] == source)
                    seen.add(source)
            elif isinstance(item, dict) and item.get("role") in ("system", "assistant", "user"):
                reordered.append(
                    {
                        "role": item["role"],
                        "content": expand(item.get("content", "")),
                        "source": "custom",
                        "priority": 50,
                        "required": False,
                    }
                )
        messages = reordered
    for injection in gen.get("injects", []):
        role = injection.get("role", "system")
        if role not in ("system", "user", "assistant", "developer"):
            raise ValueError("无效注入角色")
        indices = [i for i, m in enumerate(messages) if m["source"] in ("history", "input")]
        depth = max(0, int(injection.get("depth", 0)))
        at = (
            indices[-depth]
            if depth and len(indices) >= depth
            else indices[0]
            if depth and indices
            else len(messages)
        )
        messages.insert(
            at,
            {
                "role": role,
                "content": expand(injection.get("content", "")),
                "source": "inject",
                "priority": 80,
                "required": False,
            },
        )
    input_sources = {b.id for b in recipe.blocks if b.source == "input"}
    for message in messages:
        if message["source"] in input_sources:
            message.setdefault("message_id", input_message_id)
    # Insert after every recipe/compatibility reordering so no block separates an
    # injection from its real dialogue anchor. Frozen content is never re-expanded.
    actual_records = []
    if conversation_lore:
        for record in records:
            at = next((i for i, m in enumerate(messages) if m.get("message_id") == record["anchor_id"]), None)
            if at is None:
                continue
            if record.get("manually_removed"):
                actual_records.append(record)
                continue
            messages.insert(at, {
                "role": "system", "content": wrap_lorebook(record["content"]), "title": record["title"],
                "source": "lore:" + record["id"], "lore_ids": [record["id"]],
                "anchor_id": record["anchor_id"], "priority": 60, "required": record["id"] in retained_ids,
            })
            actual_records.append(record)
    if protected_memory and not any(m["source"] == "memory" for m in messages):
        at = next((i for i, m in enumerate(messages) if m["source"] == "history"), len(messages))
        messages.insert(
            at,
            {
                "role": "system",
                "content": protected_memory,
                "source": "memory",
                "priority": 100,
                "required": True,
            },
        )
    preference = session.get("turn_preference", "").strip()
    if preference:
        messages.insert(0, {
            "role": "system",
            "content": settings.preference_prompt + "\n" + preference,
            "source": "session_preference",
            "priority": 100,
            "required": True,
        })
    if turn_instruction.strip():
        messages.append(
            {
                "role": "system",
                "content": settings.regenerate_prompt + "\n" + turn_instruction.strip(),
                "source": "turn_instruction",
                "priority": 100,
                "required": True,
            }
        )
    if retrieval_protocol:
        messages.insert(0, {"role": "system", "content": query_instruction(settings.retrieval),
                            "source": "retrieval_protocol", "priority": 100, "required": True})
    # These application-owned instructions always lead in a fixed order.
    leading_sources = ("rules", "retrieval_protocol", "session_preference")
    messages = ([m for source in leading_sources for m in messages if m["source"] == source]
                + [m for m in messages if m["source"] not in leading_sources])
    available = settings.context_tokens - settings.reply_tokens - 2048
    # Constants form one protected request region. Card positions are local to
    # this region, never offsets into conversation history or recipe blocks.
    position_order = {"before_char": 0, "after_char": 1}

    def constant_order(entry):
        position = position_order.get(entry.get("position"), entry.get("position", 0))
        return (int(position), -int(entry.get("depth", 0) or 0) if position == 4 else 0)

    constant_messages = []
    for entry in sorted(constant_entries, key=constant_order):
        role = entry.get("role", "system")
        if isinstance(role, int):
            role = {0: "system", 1: "user", 2: "assistant"}.get(role, "system")
        constant_messages.append({
            "role": role, "content": wrap_lorebook(expand(entry["content"])),
            "source": "lore:" + entry["id"], "lore_ids": [entry["id"]],
            "lore_region": "constant", "priority": 100, "required": True,
        })
    # Keep only the global rules/preferences before the contiguous constant region.
    at = 0
    while at < len(messages) and messages[at]["source"] in ("rules", "session_preference", "retrieval_protocol"):
        at += 1
    messages[at:at] = constant_messages
    total = REPLY_PRIMING + sum(message_tokens(m, settings) for m in messages)
    # Old history and low-priority optional blocks are removed before essentials.
    removed = set()
    for i in sorted(range(len(messages)), key=lambda i: (messages[i]["priority"], i)):
        if not enforce_budget or total <= available:
            break
        m = messages[i]
        if not m["required"]:
            total -= message_tokens(m, settings)
            removed.add(i)
            diagnostics.append({"id": m["source"], "reason": "可用输入空间不足，裁剪可选内容"})
    if enforce_budget and total > available:
        largest = sorted(messages, key=lambda m: message_tokens(m, settings), reverse=True)[:3]
        detail = "、".join(f"{m['source']}={message_tokens(m, settings)} tokens" for m in largest)
        raise ValueError(f"必要上下文超出 token 预算（估算 {total} tokens，可用 {available} tokens；主要占用：{detail}），请缩短设定/状态或扩大窗口")
    messages = [m for i, m in enumerate(messages) if i not in removed]
    lore_by_id = {entry["id"]: entry for entry in entries}
    injected_ids = set()
    for message in messages:
        ids = message.get("lore_ids", [])
        if ids:
            injected_ids.update(ids)
            message.setdefault("title", " · ".join(
                str(lore_by_id[eid].get("comment") or lore_by_id[eid].get("name") or f"lore · {eid}")
                for eid in ids if eid in lore_by_id
            ))
    for entry in entries:
        eid = entry["id"]
        if eid not in injected_ids:
            diagnostics.append({"id": eid, "reason": "候选未注入，未计入使用记录"})
    suppressed_ids = {r["id"] for r in actual_records if r.get("manually_removed")
                      and any(m.get("message_id") == r["anchor_id"] for m in messages)}
    for eid in set(next_lore) | injected_ids:
        old = next_lore.setdefault(eid, {})
        if eid in injected_ids or eid in suppressed_ids:
            old.update(present=True)
            old.pop("removed_at", None)
        elif old.get("present"):
            old.update(present=False, removed_at=turn_number)
    return {
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        "trace": [
            {**m, "position": i, "estimated_tokens": message_tokens(m, settings),
             "content_tokens": count_text(m["content"], settings), "unit": "tokens"}
            for i, m in enumerate(messages)
        ],
        "lore": entries,
        "retrieval": {k: v for k, v in retrieval.items() if k != "entries"},
        "diagnostics": diagnostics,
        "next_lore": next_lore,
        "next_lore_injections": [r for r in actual_records if r["id"] in injected_ids or r["id"] in suppressed_ids],
        "estimated_tokens": total,
        "token_counting": counting_info(settings),
        "daily_context_budget": min(settings.daily_context_budget, available),
        "budget_sources": {
            **{source: sum(message_tokens(m, settings) for m in messages if m["source"] == source)
               for source in dict.fromkeys(m["source"] for m in messages)},
            "chat_framing": REPLY_PRIMING,
        },
        "window_message_count": sum(m["role"] in ("user", "assistant")
                                    and (m["source"] == "history" or m["source"] in input_sources)
                                    for m in messages),
    }
