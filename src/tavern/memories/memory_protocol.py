"""Short handles keep mechanical citation work out of the model.

The wire view is deliberately different from persistent memory. Only Runtime
knows the map from H passages, R records and E settings to stored identities.
H refs are branch positions (tavern.core.history_refs), stable across requests,
but every batch/review owns a fresh map of the passages it actually showed: an
earlier batch's passage is not citable again. The map is never serialized into
a snapshot or accepted from the model. Resolution precedes the existing
branch/hash/excerpt validator, so a short reference does not relax validation.
"""

import copy
import re
import uuid

from tavern.conversation.context import CARD_FIELDS, wrap_card_field, wrap_lorebook
from tavern.core.history_refs import message_positions, message_ref, passage_ref
from tavern.memories.memory_records import is_retained_record


def split_passages(text):
    """Lossless, bounded passages; splitting never drops a long message's tail."""
    pending = ""
    for part in re.split(r"(?<=[。！？.!?；;\n])", text):
        if pending and len(pending) + len(part) > 600:
            yield pending
            pending = ""
        pending += part
        while len(pending) > 600:
            yield pending[:600]
            pending = pending[600:]
    if pending:
        yield pending


class MemoryRequest(list):
    """Ordinary provider messages with an out-of-band, Runtime-owned reference map."""

    def __init__(self, messages, references, context=None):
        super().__init__(messages)
        self.references = references
        # Validation inputs stay local; none of this is serialized to the model.
        self.context = context or {}


def exposed_excerpts(message):
    """Texts of the passages a history tool returned; None when fully supplied."""
    parts = message.get("exposed_passages")
    if parts is None:
        return None
    text = message.get("model", message.get("raw", ""))
    return [passage for part, passage in enumerate(split_passages(text), 1) if part in parts]


class References:
    def __init__(self, history=()):
        # One branch-wide position map: batch input, tool results and review
        # render the same message with the same H ref. Callers pass the branch
        # prefix that every supplied or recallable message belongs to.
        self.positions = message_positions(history)
        self.sources = {}
        self.records = {}
        self.settings = {}
        self.versions = {}

    def passages(self, message, parts=None):
        """Number every passage of the model-visible text; show and register only ``parts``."""
        position = self.positions[message["id"]]
        shown = []
        for part, text in enumerate(split_passages(message.get("model", message.get("raw", ""))), 1):
            if parts is not None and part not in parts:
                continue
            ref = passage_ref(position, part)
            self.sources[ref] = {"message_id": message["id"], "excerpt": text}
            shown.append({"ref": ref, "text": text})
        return shown

    def message(self, message):
        # Tool results expose disjoint passages. Never make the unreturned
        # passages of a searched message citable in review.
        return {
            "ref": message_ref(self.positions[message["id"]]),
            "role": message["role"],
            "passages": self.passages(message, message.get("exposed_passages")),
        }

    def previous(self, memory):
        records = []
        for record in memory["records"]:
            if not is_retained_record(record):
                continue
            alias = next(
                (key for key, rid in self.records.items() if rid == record["id"]), f"R{len(self.records) + 1}"
            )
            self.records[alias] = record["id"]
            # Old citations are not newly supplied evidence. Hide persistent
            # IDs, hashes and tombstone internals, while keeping protection and
            # uncertainty visible. Runtime retains the complete original record.
            row = {
                k: copy.deepcopy(record[k])
                for k in (
                    "category",
                    "content",
                    "kind",
                    "people",
                    "story_time",
                    "known_to",
                    "status",
                    "manual",
                    "callback",
                    "source_stale",
                )
                if k in record
            }
            records.append({"id": alias, **row})
        return {"version": 1, "records": records}

    def evidence(self, evidence):
        result = []
        for item in evidence:
            alias = f"E{len(self.settings) + 1}"
            self.settings[alias] = item["id"]
            # Identity, anchor and version hashes remain in Runtime. A local
            # version label distinguishes historical versions without copying
            # a hash into either the prompt or the model's response.
            version = str(item.get("version", "unknown"))
            self.versions.setdefault(version, f"V{len(self.versions) + 1}")
            row = {
                "id": alias,
                "version": self.versions[version],
                **{k: item[k] for k in ("kind", "text", "historical") if k in item},
            }
            if item.get("kind") == "character" and item.get("identity") in CARD_FIELDS:
                row["text"] = wrap_card_field(item["identity"], item["text"])
            elif item.get("kind") in ("lore", "frozen_lore"):
                row["text"] = wrap_lorebook(item["text"])
            result.append(row)
        return result

    def questions(self, questions):
        inverse = {v: k for k, v in self.records.items()}
        return [{**q, "record_ids": [inverse[r] for r in q["record_ids"]]} for q in questions]

    def resolve(self, parsed, *, on_invalid=None):
        if not isinstance(parsed, dict) or not isinstance(parsed.get("operations"), list):
            raise ValueError("记忆返回格式无效：需要 operations 列表")
        result = copy.deepcopy(parsed)
        aliases = dict(self.records)
        # A question may use the ordinal of an unnamed add (N1 = first add),
        # as well as an explicitly named N handle. Explicit handles win if
        # both forms occur in one response; neither form is persisted.
        unnamed = {}
        add_number = 0
        for op in result["operations"]:
            if not isinstance(op, dict):
                raise ValueError("记忆操作必须是对象")
            rid = op.get("id")
            if op.get("op") == "add":
                add_number += 1
                if rid is None:
                    op["id"] = uuid.uuid4().hex
                    unnamed[f"N{add_number}"] = op["id"]
                elif isinstance(rid, str) and re.fullmatch(r"N[1-9]\d*", rid):
                    if rid in aliases:
                        raise ValueError("本次新增记忆短编号重复")
                    aliases[rid] = uuid.uuid4().hex
        for alias, persistent_id in unnamed.items():
            aliases.setdefault(alias, persistent_id)
        kept = []
        for op in result["operations"]:
            rid = op.get("id")
            if isinstance(rid, str):
                op["id"] = aliases.get(rid, rid)
            if "merge_ids" in op:
                op["merge_ids"] = [aliases.get(r, r) for r in op["merge_ids"]]
            record = op.get("record")
            if not isinstance(record, dict):
                kept.append(op)
                continue  # The normal validator reports malformed records.
            if "sources" in record:
                resolved = []
                unknown = next(
                    (s for s in record["sources"] if isinstance(s, str) and s not in self.sources), None
                )
                if unknown is not None:
                    if (
                        op.get("op") == "add"
                        and record.get("callback")
                        and record.get("category") == "episode"
                    ):
                        if on_invalid:
                            on_invalid(
                                {
                                    "count": 1,
                                    "message": f"已丢弃一条无效的可选原话记录：未知原文片段编号 {unknown}，本次未提供",
                                }
                            )
                        continue
                    raise ValueError(
                        f"未知原文片段编号 {unknown}：本次未提供该片段，sources 只能选择本次展示过的片段编号"
                        "（如 H52#3）；旧记忆及覆盖水位保留"
                    )
                for source in record["sources"]:
                    if not isinstance(source, str):
                        raise ValueError("原文来源必须使用本次请求的短编号")
                    source = copy.deepcopy(self.sources[source])
                    if source not in resolved:
                        resolved.append(source)
                record["sources"] = resolved
            if "setting_refs" in record:
                record["setting_refs"] = [self.settings.get(r, r) for r in record["setting_refs"]]
            kept.append(op)
        result["operations"] = kept
        for q in result.get("questions", []):
            if isinstance(q, dict) and isinstance(q.get("record_ids"), list):
                q["record_ids"] = [aliases.get(r, r) for r in q["record_ids"]]
        return result
