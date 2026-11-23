"""Worldbook selection and the text shared by story and consultation contexts."""

import hashlib
import json
import re

from tavern.character.cards import is_initvar, lore_entries, lore_title
from tavern.core.model_view import state_for_model
from tavern.core.ooc import story_of, with_ooc
from tavern.core.variable_macros import replace_variable_macros


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


def macros(text, character, persona, state, scopes=None, *, ooc=""):
    text = str(text)
    if "{{" not in text:
        return text
    # A saved user message contains both parts, but only story text expands macros.
    story = story_of(text, ooc)
    if ooc and "{{" not in story:
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

    return with_ooc(replace_variable_macros(re.sub(r"\{\{([^{}]+)}}", sub, story), state, scopes), ooc)


def wrap_lorebook(content):
    return f"<lorebook>\n{content}\n</lorebook>"


def lore_text(entry, content, with_title, expand=str, *, ref=None, preview=False):
    """Model-visible body of one entry. The title is a first body line, not a tag
    attribute: the message adapter strips and rewraps the lorebook tag per user rule."""
    title = expand(lore_title(entry)).strip() if with_title else ""
    if ref:
        content = f"[{ref} · {'开头预览' if preview else '全文'}]\n{content}"
    return f"{title}\n\n{content}" if title else content


CARD_FIELDS = {
    "system_prompt": "card_system_prompt",
    "description": "card_description",
    "personality": "card_personality",
    "scenario": "card_scenario",
    "mes_example": "card_mes_example",
    "post_history_instructions": "card_post_history_instructions",
}


def wrap_card_field(field, content):
    tag = CARD_FIELDS[field]
    return f"<{tag}>\n{content}\n</{tag}>" if content and str(content).strip() else ""


def character_content(card):
    return "\n\n".join(
        text
        for field in ("system_prompt", "description", "personality", "scenario")
        if (text := wrap_card_field(field, card.get(field, "")))
    )


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
        return bool(re.search(pattern, text, flags))
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
    document,
    history,
    user_input,
    prior,
    seed,
    *,
    session_id="",
    turn_number=None,
    retained=(),
    max_entries=None,
    expand_content=str,
    timeline="",
    retrieval=None,
    query=None,
    title_heading=True,
):
    """Apply entry admission once; RetrievalEngine owns recursive candidate discovery."""
    entries = lore_entries(document, "narrative")
    retrieval_ids = {eid: i for i, eid in enumerate(retrieval["ids"])} if retrieval is not None else {}
    selected, notes, checked = {}, [], set()
    # Normalize a copy; only the final context compiler records actual usage.
    updated = {
        eid: {k: v for k, v in old.items() if k in ("present", "removed_at")} if isinstance(old, dict) else {}
        for eid, old in prior.items()
    }
    turn_number = turn_number or sum(x["role"] == "user" for x in history) + 1
    parent_id = history[-1]["id"] if history else "empty-history"
    lore_count = 0
    budget_priority = None

    def order(entry):
        eid = entry["id"]
        return (
            entry.get("insertion_order", entry.get("order", 100)),
            (0, int(eid)) if eid.isdigit() else (1, eid),
        )

    candidates = []
    for e in entries:
        eid = e["id"]
        if (eid in retained and not e.get("constant")) or eid in checked or not e["enabled"] or is_initvar(e):
            continue
        timelines = e.get("timelines", [])
        timelines = [timelines] if isinstance(timelines, str) else timelines
        if (
            not e.get("constant")
            and timeline
            and isinstance(timelines, list)
            and timelines
            and timeline not in timelines
        ):
            checked.add(eid)
            notes.append({"id": eid, "reason": "条目不适用于当前时间线，未注入", "timeline": timeline})
            continue
        depth = max(0, int(e.get("scan_depth", e.get("scanDepth", 4)) or 0))
        scan = (
            "\n".join(x.get("model", x.get("raw", "")) for x in (history[-depth:] if depth else []))
            + "\n"
            + user_input
        )
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
        hybrid = eid in retrieval_ids
        if hybrid and query is not None:
            secondary = [matches(k, query, e) for k in e["secondary_keys"]]
            secondary_ok = (
                not secondary
                or [any(secondary), not all(secondary), not any(secondary), all(secondary)][
                    logic if 0 <= logic <= 3 else 0
                ]
            )
        reason = (
            "常驻"
            if e.get("constant")
            else "混合召回"
            if hybrid and not retrieval.get("keyword_only")
            else "关键词"
        )
        if not (e.get("constant") or ((hybrid if retrieval is not None else primary) and secondary_ok)):
            if hybrid and not secondary_ok:
                notes.append({"id": eid, "reason": "次级关键词条件不满足，未注入"})
            continue
        # Prefer explicit current-input subjects over broad body keywords.
        # Heading matches are a lightweight signal, not semantic understanding.
        heading = (lore_title(e) if title_heading else "") + " " + e.get("content", "").split("\n", 1)[0]
        heading_keys = [k for k in e["keys"] if matches(k, heading, e)]
        # A short body-only word should not activate a named subject on its own.
        # Preserve explicit regex/whole-word author rules and simple keyword books.
        if (
            reason == "关键词"
            and heading_keys
            and matched_keys
            and all(len(k) <= 2 for k in matched_keys)
            and not any(k in heading_keys for k in matched_keys)
            and not e.get("matchWholeWords")
            and not (e.get("use_regex") and any(re.search(r"[\.^$*+?{}\[\]|()]", k) for k in matched_keys))
            and not any(k.startswith("/") and k.rfind("/") > 0 for k in matched_keys)
        ):
            notes.append(
                {
                    "id": eid,
                    "reason": "仅命中正文短关键词，未提及条目主体，未注入",
                    "matched_keys": matched_keys,
                    "recursion_depth": 0,
                }
            )
            continue
        subject = any(k in heading_keys for k in direct_keys)
        tier = 0 if e.get("constant") else 1 if direct_keys else 2
        rank = (tier, -int(subject), -max((len(k) for k in direct_keys), default=0), *order(e))
        if hybrid and not retrieval.get("keyword_only"):
            rank = (1, 0, retrieval_ids[eid], *order(e))
        candidates.append((rank, e, reason, matched_keys, bool(direct_keys)))
    for rank, e, reason, matched_keys, direct in sorted(candidates, key=lambda x: x[0]):
        eid = e["id"]
        old = prior.get(eid, {})
        already_present = old.get("present", False) or bool(e.get("constant"))
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
        if cost and max_entries is not None and lore_count >= max_entries:
            if direct and rank[1] == -1:
                budget_priority = rank[:2] if budget_priority is None else min(budget_priority, rank[:2])
            notes.append({"id": eid, "reason": "本次世界书新增条目数量上限，未注入"})
            continue
        lore_count += bool(cost)
        selected[eid] = {
            **e,
            "reason": reason,
            "roll": roll,
            **recovery,
            "matched_keys": matched_keys,
            "recursion_depth": 0,
            "match_source": "constant" if e.get("constant") else "input" if direct else "history",
        }
    return sorted(selected.values(), key=order), notes, updated
