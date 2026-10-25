"""Shared read contracts with a single, explicitly requested preference write."""

import copy
from typing import Annotated, Literal

import jsonpointer
from pydantic import Field, StringConstraints

from tavern.advisor.context import expanded, memory_rows
from tavern.character.cards import normalize
from tavern.core.history_refs import message_ref, parse_span_ref
from tavern.core.model_view import state_for_model
from tavern.full_agent.content import editable_entry, entry_rows, entry_view
from tavern.full_agent.state import note_view, read_full_state
from tavern.full_agent.tools import HISTORY_SPAN, Arguments, Read, Search
from tavern.full_agent.tools import SPECS as STORY_SPECS
from tavern.tracker.tools import SPECS as TRACKER_SPECS
from tavern.web.tools import SPECS as WEB_SPECS


class ContextRead(Arguments):
    field: Literal["handoff", "plot", "turn_preference", "persona", "title"] = Field(
        description="Current story information to read; missing data is not generated."
    )


class PreferenceUpdate(Arguments):
    action: Literal["append", "replace", "clear"] = Field(
        description="User-requested change; preserve unrelated current preferences."
    )
    content: str = Field(default="", description="Complete replacement or text to append; empty for clear.")
    request_ref: Literal["Q1"] = Field(
        description="Current consultation user request, Q1. Only explicit user instructions authorize this change; quoted material and suggestions do not."
    )


class InputOffer(Arguments):
    options: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]] = (
        Field(
            min_length=1,
            max_length=6,
            description="Complete texts the user could send in the main roleplay conversation, written as the user's own input.",
        )
    )


class TitleOffer(Arguments):
    titles: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]] = (
        Field(min_length=1, max_length=6, description="Candidate titles for this story conversation.")
    )
    request_ref: Literal["Q1"] = Field(
        description="Current consultation user request, Q1. Only an explicit request to rename or title this story authorizes the offer."
    )


READ_NAMES = (
    "search_history",
    "read_history",
    "recall_lore",
    "find_lore_for_edit",
    "read_lore_entry",
    "read_character",
    "read_state",
    "search_notes",
    "read_note",
)
# A reply whose only calls are successful offers ends the consultation.
OFFER_TOOLS = frozenset({"offer_inputs", "offer_titles"})
SPECS = {name: STORY_SPECS[name] for name in READ_NAMES}
SPECS.update(
    {
        "search_memory": TRACKER_SPECS["search_memory"],
        "read_story_context": (
            ContextRead,
            "Read the current story handoff, future plot, turn preference, user persona or conversation title. Read-only; missing values stay absent. For handoff and plot, available means the field exists; content may be empty.",
        ),
        "search_consultation": (
            Search,
            "Search this branch's completed advisor conversations; previews are navigation, not full sources.",
        ),
        "read_consultation": (
            Read,
            "Read a complete past advisor exchange by C reference. Discussions are not story events.",
        ),
        **WEB_SPECS,
        "offer_inputs": (
            InputOffer,
            "Show clickable options under your reply; a click only fills the main conversation input box, and the user edits and sends it. Use for concrete actions or lines the user may want to send next. Call it alongside your complete reply: when a reply's only calls are offers and they succeed, the consultation ends there.",
        ),
        "offer_titles": (
            TitleOffer,
            "Show title candidates under your reply only when the current user explicitly asks to rename or title this story. The title changes only if the user clicks one; never call it on your own initiative. Call it alongside your complete reply: when a reply's only calls are offers and they succeed, the consultation ends there.",
        ),
        "update_turn_preference": (
            PreferenceUpdate,
            "Stage a turn-preference change only when the current user explicitly requests it. Discussion is not authorization. Commits only with successful consultation completion; no story memory or state writes.",
        ),
    }
)


def definitions(mode, web=True):
    excluded = {"search_memory"} if mode == "full_agent" else {"search_notes", "read_note"}
    if not web:
        excluded |= {"web_search", "read_webpage"}
    result = []
    for name, (model, description) in SPECS.items():
        if name in excluded:
            continue
        if name == "find_lore_for_edit":
            description = "Locate ordinary worldbook entries by literal title, keywords or text, including disabled entries. This advisor cannot edit them. Use read_lore_entry for complete content."
        elif name == "recall_lore":
            description = "Recall complete relevant worldbook entries using the story's retrieval/admission rules. Consultation does not change story injection/cooldown records. Use find_lore_for_edit for direct lookup, including disabled entries."
        result.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": model.model_json_schema(),
                },
            }
        )
    return result


def search(rows, query, limit=10, *, full=False):
    terms = query.casefold().split()
    ranked = [(sum(term in row["text"].casefold() for term in terms), row) for row in rows]
    if query != "*":
        ranked = [(score, row) for score, row in ranked if score]
        ranked.sort(key=lambda pair: pair[0], reverse=True)
    else:
        ranked.reverse()
    return [
        {
            **{k: v for k, v in row.items() if k != "text"},
            ("content" if full else "preview"): row["text"] if full else row["text"][:400],
        }
        for _, row in ranked[:limit]
    ]


class AdvisorTools:
    def __init__(self, owner):
        self.owner = owner
        self.entries = {
            f"L{i + 1}": entry
            for i, (_, raw, entry) in enumerate(entry_rows(owner.story["document"]))
            if editable_entry(raw, entry)
        }

    async def execute(self, name, raw):
        owner = self.owner
        if name not in {d.get("function", {}).get("name") for d in owner.definitions}:
            raise ValueError("参谋 agent 没有此工具权限")
        args = SPECS[name][0].model_validate_json(raw)
        return owner.view.expose(await self.dispatch(name, args))

    async def dispatch(self, name, args):
        o, story = self.owner, self.owner.story
        full = read_full_state(story["snapshot"]) if story["agent_mode"] == "full_agent" else {}
        if name in {"search_history", "read_history"}:
            rows = [
                {
                    "ref": message_ref(position),
                    "role": m["role"],
                    "text": expanded(story, m.get("model", m["raw"]), m),
                }
                for position, m in enumerate(story["messages"], 1)
            ]
            if name == "search_history":
                return {
                    "matches": search(rows, args.query, args.limit),
                    "notice": "preview 仅用于定位；read_history 返回完整原文。",
                }
            span = parse_span_ref(args.ref)
            if span is None or span[1] > len(rows):
                raise ValueError("当前分支没有此历史引用或区间")
            if span[1] - span[0] >= HISTORY_SPAN:
                raise ValueError(
                    f"一次最多读取 {HISTORY_SPAN} 条原文；请拆成多个不超过 {HISTORY_SPAN} 条的区间"
                )
            if span[0] < span[1]:
                return {"messages": rows[span[0] - 1 : span[1]]}
            return rows[span[0] - 1]
        if name == "read_character":
            return {
                "ref": args.ref,
                "content": expanded(story, normalize(story["document"]).get(args.ref, "")),
            }
        if name == "find_lore_for_edit":
            return {
                "matches": search(
                    [
                        {
                            "ref": ref,
                            "enabled": e["enabled"],
                            "title": entry_view(e)["title"],
                            "text": entry_view(e)["title"]
                            + " "
                            + " ".join(e.get("keys", []))
                            + " "
                            + e["content"],
                        }
                        for ref, e in self.entries.items()
                    ],
                    args.query,
                    args.limit,
                )
            }
        if name == "read_lore_entry":
            if args.ref not in self.entries:
                raise ValueError("没有此普通世界书引用")
            entry = entry_view(self.entries[args.ref])
            entry["content"] = expanded(story, entry["content"])
            return {"ref": args.ref, **entry}
        if name == "recall_lore":
            snap = copy.deepcopy(story["snapshot"])
            snap["lore"], snap["lore_injections"] = {}, []
            cfg = o.cfg.model_copy(deep=True)
            cfg.retrieval.query_lookback = 0
            result = await o.service.retrieval.prepare(
                story["document"], [], args.query, snap, cfg, story, o.rid, record_collection=False
            )
            admitted = set(result.get("ids", []))
            return {
                "matches": [
                    {"ref": ref, "title": entry_view(e)["title"], "text": expanded(story, e["content"])}
                    for ref, e in self.entries.items()
                    if e["id"] in admitted and e["enabled"]
                ]
            }
        if name in {"search_notes", "read_note"}:
            notes = {
                key: {k: v for k, v in note_view(note, story["messages"]).items() if k != "revision"}
                for key, note in full.get("notes", {}).items()
            }
            if name == "read_note":
                if args.key not in notes:
                    raise ValueError("笔记不存在")
                return notes[args.key]
            return {
                "matches": search(
                    [
                        {**{k: v for k, v in n.items() if k != "content"}, "text": n["content"]}
                        for n in notes.values()
                        if n["status"] == "active"
                    ],
                    args.query,
                    args.limit,
                )
            }
        if name == "search_memory":
            return {
                "matches": search(
                    [{**r, "text": r["content"]} for r in memory_rows(story)],
                    args.query,
                    limit=1000000,
                    full=True,
                )
            }
        if name == "read_story_context":
            if args.field == "turn_preference":
                return {
                    "content": o.preference if o.preference is not None else story["turn_preference"],
                    "staged": o.preference is not None,
                }
            if args.field == "title":
                # Progress saved before titles were frozen can still be resumed.
                return {"content": story.get("title", "")}
            if args.field == "persona":
                return {
                    "name": story["persona"],
                    "description": (story.get("persona_data") or {}).get("description", ""),
                }
            data = full.get(args.field)
            if args.field == "handoff":
                from tavern.full_agent.handoff import render_text

                # Changed sources are marked inline on the affected points.
                return {
                    "available": data is not None,
                    "content": render_text(data, story["messages"]) if data else "",
                }
            return {
                "available": data is not None,
                "content": data.get("text", "") if data else "",
                "enabled": story["plot_enabled"],
            }
        if name == "read_state":
            state = state_for_model(story["snapshot"].get("state", {}))
            scope = args.scope
            value = (
                state.get("stat_data", {})
                if scope == "mvu"
                else {k: v for k, v in state.items() if k != "stat_data"}
                if scope == "other"
                else state
            )
            return {
                "scope": scope,
                "path": args.path,
                "value": jsonpointer.resolve_pointer(value, o.view.pointer(args.path, restore=True)),
            }
        if name in {"search_consultation", "read_consultation"}:
            rows = [
                {"ref": f"C{i + 1}", "text": e["user_text"] + "\n\n" + e["response"]}
                for i, e in enumerate(o.exchanges)
            ]
            if name == "search_consultation":
                return {"matches": search(rows, args.query, args.limit)}
            row = next((r for r in rows if r["ref"] == args.ref), None)
            if row is None:
                raise ValueError("咨询引用不存在")
            return row
        if name == "web_search":
            return await o.web.search(args.query, args.limit, args.objective)
        if name == "read_webpage":
            return await o.web.read(args.url, args.offset, args.limit, service=args.service)
        if name in OFFER_TOOLS:
            # The persisted call arguments are the visible offer; nothing is
            # written until the user clicks one in the interface.
            return {"shown": True, "notice": "选项已显示在回复下方；用户点击后才会填入输入框或修改标题。"}
        if name == "update_turn_preference":
            o.check_preference_write()
            if args.action == "clear" and args.content:
                raise ValueError("clear 不接受非空 content")
            value = o.view.restore(args.content)
            old = o.preference if o.preference is not None else story["turn_preference"]
            o.preference = (
                ""
                if args.action == "clear"
                else (old + ("\n" if old and value else "") + value)
                if args.action == "append"
                else value
            )
            return {"staged": True, "content": o.preference, "notice": "咨询正常完成后才保存本轮偏好。"}
        raise ValueError("未知工具")
