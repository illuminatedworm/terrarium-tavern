"""Bounded, source-aware Tracker consolidation shared by all archive triggers.

The generator returns a proposal, never committed state. A whole invocation is
atomic: failed later batches cannot advance the coverage of earlier draft batches.
CallBudget belongs to the Runtime turn, so replanning a window cannot reset it.
"""
import copy
import json
import time
from dataclasses import dataclass, field

from tavern.character.cards import normalize
from tavern.conversation.context import macros
from tavern.conversation.scene import archive_history, parse_json
from tavern.core.tokens import count_text, request_tokens
from tavern.memories.history_recall import HISTORY_TOOL, HistoryBudget
from tavern.memories.memory_evidence import evidence_pool, select_evidence
from tavern.memories.memory_protocol import MemoryRequest, References
from tavern.memories.memory_records import Record, digest, is_retained_record, source_hash
from tavern.memories.memory_retention import retain_memory

PROTOCOL = """【程序输出契约】
本节统一规定返回格式、来源和操作边界。上文的整理原则只约束内容取舍，不得改为完整快照或其他输出格式。
输入 previous_result 是现有分类记忆；messages 是本批新增原文；background_messages 仅用于理解，不能当成新发展；evidence 是设定/状态参考，不是已发生的剧情。
historical=false 的材料是当前版本回退，不能证明过去设定。状态快照不能证明原因、角色知情或事件时间。
operations 仅包含本次增量修改，未提及的旧条目原样保留。previous_result 中 status=deleted 只表示用户手工删除：用户不希望它作为独立记忆保留，通常认为它不重要或冗余；这不否定剧情原文。不要恢复、修改或引用该条目。程序自动移除的条目已彻底移走，不会作为 deleted 返回。op 为 add/revise/complete/merge/remove。新增 add 可不写 id，程序分配永久ID；questions 可用 N1、N2 等按本次 add 出现顺序引用新条目（不计其他操作），也可在 add.id 显式填写 N 编号。显式编号优先且不能重复。修改旧条目只选 previous_result 的 R1、R2 等短编号。merge 另附 merge_ids，使用旧条目短编号，不能合并或修改手工、已删除、原文已改动的条目。完成事项须有本批证据，不删除仍有效约定或关键疑问。
record 的 category 为 current/episode/open/uncertain，kind 为 fact/claim/inference；status 如需填写只用 active/completed。移除用 remove 操作，合并用 merge 操作；superseded 只是程序内部待移除标记，deleted 只由用户手工删除产生，模型都不要填写。未知 people/known_to 使用 []，未知 story_time 使用“未知”；known_to 只包含原文明确支持的知情对象。
每个新增或修改的 record 必须有 sources，格式为 ["S1","S2"]，只选择支持该内容的原文片段 ref，且至少一处来自 messages。不要抄写原话、message_id、excerpt 或 fingerprint：真实消息ID、逐字摘录和指纹全部由程序回填。S 编号对应输入 passages，H 编号对应工具实际返回的 passages；M 编号只标识消息，不能作为 sources。setting_refs 只选择 evidence 的 E1、E2 等短编号，没有引用可省略。所有编号只在本次请求中有效，不沿用上批编号。
已有记忆不足以理解本批变化时，可用只读 tavern_recall_history 搜索旧剧情或按搜索返回的段编号读取原文；整次维护合计最多3次，分批及复核共享额度。补查只作背景或补充依据，不替代分类记录、不把旧事当作本批新进展，也不自动修复已编辑的旧记忆。引用补查只选返回的 H 编号，变化仍须至少引用一处本批新增原文。
callback 是 record 内的布尔字段，只能用于 episode；最多三条、合计300 tokens，只补充本批原话/动作，不为此请求复核。
questions 仅用于关键局部疑问，最多八项；topic 为 ownership/identity/knowledge/injury/relationship/completion，record_ids 指向本次已有的非手工 active、uncertain 条目。证据不足的结论先保存为 uncertain，不能一边请求核验一边当作确定事实。
需要补查时先调用提供的只读工具，最终答复只返回一个合法 JSON 对象，顶层必须为 operations 和 questions；无修改且无疑问时返回 {"operations":[],"questions":[]}。
最小格式示例（S1须换成实际支持该内容的片段编号）：{"operations":[{"op":"add","record":{"category":"episode","content":"她把戒指借给了用户。","sources":["S1"]}}],"questions":[]}。新增时 kind 默认 fact，status 默认 active，people/known_to 默认 []，story_time 默认“未知”，callback 默认 false；修订时省略的内容字段继承旧值。sources 每次必填，setting_refs 每次默认 []。不确定或角色说法必须明确填写 kind，不能用默认值冒充事实。
疑问格式：{"topic":"ownership","query":"待核对的对象与属性","record_ids":["对应待确认条目ID"]}。不返回完整快照、text/memories、Markdown或解释文字。引用成立只代表原文存在，不代表说法客观为真。
"""

REVIEW_PROTOCOL = """这是同一次整理的唯一疑难复核。只处理 questions 对应的 uncertain 条目，依据新增 evidence 和提供的原文。
只能返回 revise 操作，不能新增条目、改写无关记录或再次请求复核。无充分证据就保持 uncertain；相关性不等于证实。
解决争议须引用本次提供的设定片段 ID；仍为 uncertain 可以不修改。输出 {"operations":[],"questions":[]}。
"""


@dataclass
class CallBudget:
    manual: bool = False
    primary: int = 0
    review: int = 0
    history: HistoryBudget = field(default_factory=HistoryBudget)

    def reserve(self, phase):
        if phase == "memory_review":
            if self.review >= 1:
                raise ValueError("本次整理已使用一次疑难复核")
            self.review += 1
        else:
            if not self.manual and self.primary >= 2:
                raise ValueError("自动整理已达到两次分批请求上限；请手动重建或调整输入预算")
            self.primary += 1


def boundaries(history, start):
    return [i + 1 for i in range(start, len(history)) if history[i]["role"] == "assistant"]


def context_start(history, start):
    users = [i for i in range(start) if history[i]["role"] == "user"]
    return users[-2] if len(users) >= 2 else 0


def _merge_history_sources(supplied, additional):
    for mid, message in additional.items():
        if mid not in supplied:
            supplied[mid] = copy.deepcopy(message)
        elif "exposed_excerpts" in supplied[mid]:
            # Review may reveal another fragment of an already searched long
            # message. Keep all actually exposed fragments, never the unseen gap.
            supplied[mid]["exposed_excerpts"] = list(dict.fromkeys([
                *supplied[mid]["exposed_excerpts"], *message.get("exposed_excerpts", [])]))


def apply_operations(memory, parsed, originals, expanded, evidence, *, fresh_ids, settings, review_ids=None,
                     on_invalid=None, on_suppressed=None, suppressed_ids=None, defer_retention=False):
    if not isinstance(parsed, dict) or not isinstance(parsed.get("operations"), list):
        raise ValueError("记忆返回格式无效：需要 operations 列表；自定义提示词不得要求旧 text/memories 协议")
    if len(parsed["operations"]) > 100:
        raise ValueError("单次记忆操作超过100条")
    result = copy.deepcopy(memory)
    result["records"] = [r for r in result["records"] if is_retained_record(r)]
    by_id = {r["id"]: r for r in result["records"]}
    supplied = {r["id"] for r in evidence}
    blocked_ids = suppressed_ids if suppressed_ids is not None else set()

    def suppress(rid, message):
        blocked_ids.add(rid)
        if on_suppressed:
            on_suppressed({"count": 1, "message": message})

    for op in parsed["operations"]:
        if not isinstance(op, dict):
            raise ValueError("记忆操作必须是对象；原记忆保留")
        action, rid = op.get("op"), op.get("id")
        # Only optional *new texture* may be discarded independently. A bogus
        # update of an existing continuity item must still reject the batch.
        optional = op.get("record", {})
        if action == "add" and optional.get("callback") and optional.get("category") == "episode":
            try:
                candidate = Record.model_validate({**optional, "id": rid})
                if not candidate.sources or not any(s.message_id in fresh_ids for s in candidate.sources):
                    raise ValueError("回调缺少本批原文")
                if any(s.message_id not in expanded or s.excerpt not in expanded[s.message_id].get(
                        "model", expanded[s.message_id]["raw"]) for s in candidate.sources):
                    raise ValueError("回调摘录与原文不匹配")
                if any("exposed_excerpts" in expanded[s.message_id] and not any(
                        s.excerpt in part for part in expanded[s.message_id]["exposed_excerpts"]) for s in candidate.sources):
                    raise ValueError("回调摘录未在补查结果中展示")
                if not set(candidate.setting_refs) <= supplied:
                    raise ValueError("回调引用了未提供的设定")
            except (ValueError, TypeError) as error:
                if on_invalid:
                    on_invalid({"count": 1, "message": "已丢弃一条无效的可选原话记录：" + str(error)})
                continue
        if action not in ("add", "revise", "complete", "merge", "remove") or not isinstance(rid, str) or not 0 < len(rid) <= 100:
            raise ValueError("记忆操作或条目ID无效；原记忆保留")
        if rid in blocked_ids or (action == "merge" and any(other in blocked_ids for other in op.get("merge_ids", []))):
            suppress(rid, "已忽略一条依赖手工删除内容的记忆操作")
            continue
        old = by_id.get(rid)
        if review_ids is not None and (action != "revise" or rid not in review_ids):
            raise ValueError("复核不能修改无关条目")
        # A tombstone is an expected consequence of a user's delete. Ignore a
        # model's attempt to touch it without throwing away unrelated changes.
        if old and old.get("status") == "deleted":
            suppress(rid, "已忽略一条涉及手工删除记忆的操作")
            continue
        if action == "add" and old:
            raise ValueError("新增记忆ID已存在")
        if action != "add" and not old:
            raise ValueError("修改的记忆条目不存在")
        if old and (old.get("manual") or old.get("source_stale")):
            raise ValueError("不可自动修改手工或原文已改动的记忆；请另记待确认内容")
        data = {**(old or {}), **op.get("record", {}), "id": rid, "manual": False}
        data["setting_refs"] = op.get("record", {}).get("setting_refs", [])
        if data.get("category") not in ("current", "episode", "open", "uncertain"):
            raise ValueError("自动记忆必须使用四种剧情分类")
        record = Record.model_validate(data).model_dump()
        if not record["content"].strip() or not record["sources"]:
            raise ValueError("关键记忆缺少内容或来源；不推进覆盖水位")
        if record["status"] == "deleted":
            raise ValueError("自动操作不得制造手工删除状态")
        if record["status"] == "superseded" and action != "remove":
            raise ValueError("模型不能直接设置自动移除状态；请使用 remove 或 merge 操作")
        for source in record["sources"]:
            mid = source["message_id"]
            if mid not in expanded or source["excerpt"] not in expanded[mid].get("model", expanded[mid]["raw"]):
                raise ValueError("记忆摘录不在本批提供的原文中；原记忆保留")
            fragments = expanded[mid].get("exposed_excerpts")
            if fragments is not None and not any(source["excerpt"] in part for part in fragments):
                raise ValueError("记忆摘录未在历史工具返回片段中展示")
            source["fingerprint"] = source_hash(originals[mid])
        if not any(s["message_id"] in fresh_ids for s in record["sources"]):
            raise ValueError("变化必须引用本批新增剧情，不能重复应用背景历史")
        if not set(record["setting_refs"]) <= supplied:
            raise ValueError("记忆引用了未提供的设定片段")
        if any(r.get("status") == "deleted" and (
            (r.get("deleted_hash") == digest(record["content"].strip()) and (
                not r.get("deleted_sources") or any(s["message_id"] == d["message_id"]
                    for s in record["sources"] for d in r["deleted_sources"]))) or
            any(s["message_id"] == d["message_id"] and digest(s["excerpt"]) == d["excerpt_hash"]
                for s in record["sources"] for d in r.get("deleted_sources", [])))
               for r in result["records"]):
            suppress(rid, "已忽略一条试图恢复手工删除内容的记忆操作")
            continue
        if action == "merge" and any(by_id.get(other, {}).get("status") == "deleted"
                                     for other in op.get("merge_ids", [])):
            suppress(rid, "已忽略一条涉及手工删除记忆的合并操作")
            continue
        record["setting_sources"] = [{k: e[k] for k in (
            "id", "identity", "version", "reference", "anchor", "kind", "historical", "offset") if k in e}
            for e in evidence if e["id"] in record["setting_refs"]]
        if review_ids is not None and record["category"] != "uncertain" and not record["setting_refs"]:
            raise ValueError("消除争议必须引用复核提供的补充材料")
        if record["kind"] == "inference" and record["category"] == "current":
            record["category"] = "uncertain"
        if action == "complete":
            record["status"] = "completed"
        if record["status"] == "completed" and (record["category"] == "open" or (old or {}).get("category") == "open"):
            record["category"] = "episode"
        if action == "remove":
            record["status"] = "superseded"
        if record["callback"] and record["category"] != "episode":
            raise ValueError("原话回调只能存为经历")
        if action == "merge":
            for other in op.get("merge_ids", []):
                target = by_id.get(other)
                if other == rid or not target or target.get("manual") or target["status"] == "deleted":
                    raise ValueError("合并来源无效或受手工保护")
                target["status"] = "superseded"
        if old:
            old.clear()
            old.update(record)
        else:
            result["records"].append(record)
            by_id[rid] = record
    return result if defer_retention else retain_memory(result, list(originals.values()), settings)


def _messages(memory, original, expanded, start, end, evidence, settings, document=None, persona=None):
    beginning = context_start(expanded, start)
    references = References()
    previous = references.previous(memory)
    instruction = settings.memory_system_prompt
    if document is not None and persona is not None:
        # Preserve custom prompt macros, evaluated at this batch's boundary,
        # not with a later snapshot that would leak future changes into history.
        snapshot = original[end - 1].get("snapshot", {})
        source_document = (snapshot.get("content") or {}).get("document", document)
        instruction = macros(instruction, normalize(source_document), persona, snapshot.get("state", {}))
    return MemoryRequest([
        # One explicit contract follows the content policy. Keep the same
        # arrangement for defaults and custom policies; never ask the model to
        # reconcile two competing output schemas in separate system messages.
        {"role": "system", "content": instruction.rstrip() + "\n\n" + PROTOCOL},
        {"role": "user", "content": json.dumps({
            "previous_result": previous, "messages": [references.message(m) for m in expanded[start:end]],
            "background_messages": [references.message(m) for m in expanded[beginning:start]],
            "evidence": references.evidence(evidence),
            "capacity_tokens": settings.memory_capacity_tokens,
        }, ensure_ascii=False)},
    ], references)


def plan_batch(history, expanded, start, memory, document, settings, persona=None):
    limit = min(settings.memory_input_tokens, settings.context_tokens - settings.memory_output_tokens - 2048)
    best = None
    for end in boundaries(history, start):
        messages = _messages(memory, history, expanded, start, end, [], settings, document, persona)
        cost = request_tokens(messages, settings, [HISTORY_TOOL])
        if cost > limit:
            if best is None:
                best = end  # One complete exchange remains eligible even when oversized.
            break
        best = end
    if best is None:
        raise ValueError("没有可整理的完整交流")
    end = best
    pool = evidence_pool(history, document, start, end)
    query = "\n".join(m.get("model", m["raw"]) for m in expanded[start:end])
    evidence = select_evidence(pool, query, settings)
    messages = _messages(memory, history, expanded, start, end, evidence, settings, document, persona)
    return end, messages, pool, evidence


def estimate_batches(history, memory, document, persona, settings):
    expanded = archive_history(history, document, persona)
    count, start = 0, 0
    while start < len(history):
        start = plan_batch(history, expanded, start, memory, document, settings, persona)[0]
        count += 1
    return count


async def consolidate(history, settings, generate, *, memory, start=0, document=None,
                      persona=None, budget=None, event=None):
    budget = budget or CallBudget()
    event = event or (lambda kind, payload: None)
    memory = copy.deepcopy(memory)
    memory["records"] = [r for r in memory["records"] if is_retained_record(r)]
    expanded = archive_history(history, document, persona)
    originals = {m["id"]: m for m in history}
    while start < len(history):
        end, messages, pool, evidence = plan_batch(history, expanded, start, memory, document, settings, persona)
        budget.reserve("memory")
        event("memory_batch", {"purpose": "memory", "from": start, "to": end,
                               "source_ids": [m["id"] for m in history[start:end]],
                               "estimated_input_tokens": request_tokens(messages, settings, [HISTORY_TOOL]),
                               "evidence": evidence, "candidates": len(pool),
                               "primary_calls": budget.primary})
        if request_tokens(messages, settings, [HISTORY_TOOL]) > settings.memory_input_tokens:
            event("context_warning", {"purpose": "memory", "message": "记忆请求估算超出输入预算，仍完整发送"})
        begun = time.perf_counter()
        response = await generate(messages, phase="memory", output_tokens=settings.memory_output_tokens)
        if count_text(response, settings) > settings.memory_output_tokens:
            raise ValueError("记忆模型响应超过整理输出上限")
        parsed = messages.references.resolve(parse_json(response),
            on_invalid=lambda p: event("memory_records_discarded", p))
        event("memory_batch_completed", {"purpose": "memory", "duration_s": time.perf_counter() - begun,
                                         "covered_count": end})
        beginning = context_start(expanded, start)
        supplied = {m["id"]: m for m in expanded[beginning:end]}
        _merge_history_sources(supplied, getattr(response, "history_sources", {}))
        fresh = {m["id"] for m in expanded[start:end]}
        suppressed_ids = set()
        memory = apply_operations(memory, parsed, originals, supplied, evidence, fresh_ids=fresh, settings=settings,
                                  on_invalid=lambda p: event("memory_records_discarded", p),
                                  on_suppressed=lambda p: event("memory_tombstone_suppressed", p),
                                  suppressed_ids=suppressed_ids, defer_retention=True)
        questions = parsed.get("questions", [])
        if not isinstance(questions, list):
            raise ValueError("questions 必须是列表")
        if len(questions) > 8:
            raise ValueError("一次整理最多提出八个局部疑难问题")
        questions = [q for q in questions if isinstance(q, dict) and q.get("topic") in (
            "ownership", "identity", "knowledge", "injury", "relationship", "completion")]
        # Neither a suppressed add nor a deleted old record can be reviewed.
        # Keep unrelated questions and the rest of this batch.
        deleted_ids = {r["id"] for r in memory["records"] if r["status"] == "deleted"}
        questions = [q for q in questions if not (isinstance(q.get("record_ids"), list) and
                     any(isinstance(r, str) and (r in suppressed_ids or r in deleted_ids)
                         for r in q["record_ids"]))]
        available_ids = {r["id"] for r in memory["records"] if not r.get("manual") and r["status"] == "active"}
        for question in questions:
            refs = question.get("record_ids")
            if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in available_ids for r in refs):
                raise ValueError("疑难问题必须指向实际存在的待确认记忆，不能静默丢弃核验请求")
        ids = {rid for q in questions for rid in q.get("record_ids", [])}
        targets = [r for r in memory["records"] if r["id"] in ids and not r.get("manual")]
        # A requested verification is itself evidence of uncertainty. The first
        # pass may not inject a disputed conclusion as a confident current fact.
        for r in targets:
            r["category"] = "uncertain"
        if questions:
            reason = "复核关闭或本次操作已使用复核"
            if settings.memory_review_enabled and not budget.review and targets:
                extra = select_evidence(pool, "\n".join(str(q.get("query", "")) for q in questions), settings,
                                        exclude={e["id"] for e in evidence})
                already_read = {" ".join(e["text"].split()) for e in evidence}
                extra = [e for e in extra if " ".join(e["text"].split()) not in already_read]
                reason = "没有新增材料，保留待确认内容"
                if extra:
                    refs = References()
                    payload = {"previous_result": refs.previous({"version": 1, "records": targets}),
                               "questions": refs.questions(questions),
                               "messages": [refs.message(m) for m in supplied.values() if m["id"] in fresh],
                               "background_messages": [refs.message(m) for m in supplied.values() if m["id"] not in fresh],
                               "evidence": refs.evidence(evidence + extra)}
                    # Reuse this batch's resolved content policy and contract so
                    # review cannot silently fall back to a different default
                    # when the user has customized the memory instructions.
                    review = MemoryRequest([
                        {"role": "system", "content": messages[0]["content"] + "\n\n" + REVIEW_PROTOCOL},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], refs)
                    if request_tokens(review, settings, [HISTORY_TOOL]) > settings.memory_input_tokens:
                        event("context_warning", {"purpose": "memory_review",
                                                  "message": "记忆复核估算超出输入预算，仍完整发送"})
                    budget.reserve("memory_review")
                    event("memory_review_started", {"purpose": "memory_review", "questions": questions,
                                                     "evidence": extra})
                    try:
                        review_text = await generate(review, phase="memory_review",
                                                     output_tokens=min(2048, settings.memory_output_tokens))
                        _merge_history_sources(supplied, getattr(review_text, "history_sources", {}))
                        response = refs.resolve(parse_json(review_text))
                        if response.get("questions"):
                            raise ValueError("疑难复核不能再次请求核验")
                        memory = apply_operations(memory, response, originals, supplied, evidence + extra,
                            fresh_ids=fresh, settings=settings, review_ids={r["id"] for r in targets},
                            defer_retention=True)
                        reason = "疑难复核完成；未解决的条目仍保持待确认"
                    except Exception as error:
                        reason = "疑难复核失败，保留待确认内容：" + str(error)
            event("memory_review_result", {"purpose": "memory_review", "message": reason})
        memory = retain_memory(memory, history[:end], settings, event=event)
        start = end
    return memory
