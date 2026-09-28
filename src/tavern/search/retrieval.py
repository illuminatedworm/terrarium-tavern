"""Roleplay Runtime retrieval: admission, fusion and content-addressed indexes."""
import asyncio
import hashlib
import json
import math
import re
import threading
import time
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from tavern.search.retrieval_text import query_text

_token_lock = threading.Lock()
_keyword_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lore-keyword")
STOP_WORDS = frozenset(('a an the of to in on at is are was were be by for from with and or '
                        'who what where which how can does do its it only that this as '
                        '的 了 在 是 和 与 及 谁 什么 哪里 怎么 哪种 哪个 能 有 把 用 会 '
                        'の に は を で が と へ も から だけ する いる なる ある こと もの').split())


@lru_cache(maxsize=1)
def tokenizers():
    import jieba
    from sudachipy import dictionary, tokenizer
    chinese = jieba.Tokenizer()
    chinese.initialize()
    return chinese, dictionary.Dictionary(dict="core").create(), tokenizer.Tokenizer.SplitMode.B


@lru_cache(maxsize=16384)
def tokenize(text):
    text = unicodedata.normalize("NFKC", text).lower()
    chinese, japanese, mode = tokenizers()
    words = []
    with _token_lock:
        for span in re.findall(r"[\u3400-\u9fff\u3040-\u30ffー]+|[^\W_]+", text):
            if re.search(r"[\u3040-\u30ff]", span):
                words.extend(t.normalized_form() for t in japanese.tokenize(span, mode)
                             if t.part_of_speech()[0] not in ("助詞", "助動詞", "補助記号"))
            elif re.search(r"[\u3400-\u9fff]", span):
                words.extend(chinese.cut_for_search(span))
            else:
                words.append(span)
    return tuple(w for w in words if w.strip() and w not in STOP_WORDS)


def grams(text):
    spans = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).lower())
    return [s[i:i + 2] for s in spans for i in range(len(s) - 1)]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def entry_order(entry):
    eid = str(entry["id"])
    return (entry.get("insertion_order", entry.get("order", 100)),
            (0, int(eid)) if eid.isdigit() else (1, eid))


class BM25:
    def __init__(self, documents):
        self.lengths = [len(tokens) for tokens in documents]
        self.average = sum(self.lengths) / max(1, len(documents)) or 1
        self.postings = defaultdict(list)
        for index, tokens in enumerate(documents):
            for term, count in Counter(tokens).items():
                self.postings[term].append((index, count))

    def score(self, query, k1, b):
        scores = defaultdict(float)
        size = len(self.lengths)
        for term in set(query):
            posting = self.postings.get(term, [])
            idf = math.log(1 + (size - len(posting) + .5) / (len(posting) + .5))
            for index, count in posting:
                scores[index] += idf * count * (k1 + 1) / (
                    count + k1 * (1 - b + b * self.lengths[index] / self.average))
        return scores


class SparseIndex:
    def __init__(self, entries, config):
        self.entries = {e["id"]: e for e in entries}
        self.fragments = []
        for entry in entries:
            title = " ".join([str(entry.get("name", "")), str(entry.get("comment", "")), *entry.get("keys", [])])
            tokens = tokenize(entry.get("content", ""))
            stride = config.chunk_tokens - config.chunk_overlap
            for start in range(0, max(1, len(tokens)), stride):
                text = title + " " + " ".join(tokens[start:start + config.chunk_tokens])
                self.fragments.append({"id": entry["id"], "text": text})
        self.words = BM25([tokenize(f["text"]) for f in self.fragments])
        self.characters = BM25([grams(f["text"]) for f in self.fragments])

    def search(self, query, config):
        query_tokens = {token for token in tokenize(query) if len(token) >= 2 or token.isdigit()}
        words = self.words.score(query_tokens, config.bm25_k1, config.bm25_b)
        chars = self.characters.score(grams(query), config.bm25_k1, config.bm25_b)
        # Normalize each field independently; raw scores across token vocabularies are incomparable.
        wm, cm = max(words.values(), default=1), max(chars.values(), default=1)
        scores = defaultdict(float)
        for i in words.keys() | chars.keys():
            score = (1 - config.gram_weight) * words.get(i, 0) / wm + config.gram_weight * chars.get(i, 0) / cm
            matched = query_tokens & set(tokenize(self.fragments[i]["text"]))
            informative = {term for term in matched if len(self.words.postings[term]) < 3
                           or len(self.words.postings[term]) / max(1, len(self.fragments)) <= .25}
            # Relative rank alone cannot establish relevance: require meaningful lexical coverage.
            if not informative or len(matched) / max(1, len(query_tokens)) < config.bm25_min_coverage:
                continue
            eid = self.fragments[i]["id"]
            scores[eid] = max(scores[eid], score)
        scores = {eid: score for eid, score in scores.items() if score >= config.bm25_min_score}
        return sorted(scores, key=lambda eid: (-scores[eid], entry_order(self.entries[eid]))), dict(scores)


_indexes = OrderedDict()
_index_lock = threading.Lock()


def sparse_index(entries, config):
    key = digest([entries, config.chunk_tokens, config.chunk_overlap, "jieba-sudachi-v1"])
    with _index_lock:
        if key not in _indexes:
            _indexes[key] = SparseIndex(entries, config)
        _indexes.move_to_end(key)
        while len(_indexes) > 16:
            _indexes.popitem(last=False)
        return key, _indexes[key]


def fuse(rankings, entries, config):
    if len(rankings) == 1:
        return list(next(iter(rankings.values())))[:config.fusion_count]
    scores = defaultdict(float)
    for channel, ids in rankings.items():
        for rank, eid in enumerate(dict.fromkeys(ids), 1):
            scores[eid] += config.weights.get(channel, 1) / (config.rrf_k + rank)
    by_id = {e["id"]: e for e in entries}
    return sorted(scores, key=lambda eid: (-scores[eid], entry_order(by_id[eid])))[:config.fusion_count]


def fusion_scores(rankings, config):
    scores = defaultdict(float)
    for channel, ids in rankings.items():
        for rank, eid in enumerate(dict.fromkeys(ids), 1):
            scores[eid] += config.weights.get(channel, 1) / (config.rrf_k + rank)
    return dict(scores)


def lexical_recall(document, history, user_input, snapshot, settings, session, seed, *, cached_only=False, turn_number=None):
    from tavern.character.cards import lore_entries, normalize
    from tavern.conversation.context import activate_lore, get_path, macros, matches
    config = settings.retrieval
    state = snapshot.get("state", {})
    timeline = get_path(state, "世界.当前时间线", "")
    card = normalize(document)
    entries = [dict(e, content=macros(e.get("content", ""), card, session.get("persona", ""), state))
               for e in lore_entries(document, "writer") if e["enabled"] and not e.get("constant")
               and "[initvar]" not in e.get("comment", "").lower()
               and (not timeline or not e.get("timelines") or timeline in (
                   [e["timelines"]] if isinstance(e["timelines"], str) else e["timelines"]))]
    clues = [{**m, "model": m.get("retrieval_query", ""), "raw": ""} for m in history]
    visible_ids = {m["id"] for m in history}
    retained = {r["id"] for r in snapshot.get("lore_injections", []) if r["anchor_id"] in visible_ids}
    exiting = {eid for eid, old in snapshot.get("lore", {}).items()
               if isinstance(old, dict) and old.get("present") and eid not in retained}
    suppressed = retained | exiting if session.get("lore_injection_mode") == "conversation" else set()
    query = query_text(history, user_input, config)
    from tavern.search.lore_admission import keyword_evidence
    lexical_ids, evidence = keyword_evidence(entries, query, user_input)
    if clues and not any(m.get("retrieval_query") for m in history) and user_input.strip().lower() in (
            "", "继续", "继续剧情", "continue", "続けて"):
        clues[-1]["model"] = query
    def keyword_channel():
        start = time.perf_counter()
        found, notes, _ = activate_lore(document, clues, user_input, snapshot.get("lore", {}), seed,
            session_id=session.get("id", ""), recursive=False, timeline=timeline, turn_number=turn_number)
        return found, notes, round((time.perf_counter() - start) * 1000, 2)

    keyword_future = _keyword_pool.submit(keyword_channel)
    ranks, diagnostics, timings, sparse_scores = {}, [], {}, {}
    if "bm25" in config.channels:
        bm25_started = time.perf_counter()
        try:
            if cached_only:
                cache_key = digest([entries, config.chunk_tokens, config.chunk_overlap, "jieba-sudachi-v1"])
                with _index_lock:
                    index = _indexes.get(cache_key)
                if index is None:
                    raise ValueError("当前世界书索引未准备，只读预览不建立索引")
            else:
                _, index = sparse_index(entries, config)
            ranks["bm25"], sparse_scores = index.search(query, config)
        except Exception as error:
            diagnostics.append({"reason": "BM25 召回降级", "detail": str(error)})
        timings["bm25"] = round((time.perf_counter() - bm25_started) * 1000, 2)
    keyword, keyword_notes, timings["keyword"] = keyword_future.result()
    keyword = [e for e in keyword if not e.get("constant") and e["id"] not in suppressed]
    diagnostics.extend({"id": e["id"], "reason": "关键词缺少明确主体或有效词汇证据，未注入"}
                       for e in keyword if e["id"] not in lexical_ids)
    keyword = [e for e in keyword if e["id"] in lexical_ids]

    def keyword_order(entry):
        heading = str(entry.get("name", "")) + " " + entry["content"].split("\n", 1)[0]
        subject = any(matches(k, heading, entry) and matches(k, user_input, entry) for k in entry.get("keys", []))
        return (not entry.get("direct_mention"), not subject,
                -max(map(len, entry.get("matched_keys", [])), default=0), entry_order(entry))

    keyword.sort(key=keyword_order)
    diagnostics.extend(n for n in keyword_notes if n["id"] not in {e["id"] for e in keyword} | suppressed)
    if "keyword" in config.channels:
        ranks["keyword"] = [e["id"] for e in keyword][:config.candidate_count]
    # Gate before per-channel truncation and fusion, including in read-only previews.
    for channel in ("bm25",):
        if channel in ranks:
            eligible, notes, _ = activate_lore(document, clues, user_input, snapshot.get("lore", {}), seed,
                session_id=session.get("id", ""), recursive=False, timeline=timeline,
                turn_number=turn_number,
                retained=retained | exiting if session.get("lore_injection_mode") == "conversation" else (),
                retrieval={"ids":ranks[channel]}, query=query)
            allowed = {e["id"] for e in eligible}
            ranks[channel] = [eid for eid in ranks[channel] if eid in allowed][:config.candidate_count]
            diagnostics.extend(n for n in notes if n["id"] not in allowed)
    return {"query": query, "rankings": ranks, "entries": entries, "keyword_only": config.channels == ["keyword"],
            "evidence": evidence, "bm25_scores": sparse_scores,
            "lexical_ids": sorted(lexical_ids),
            "channel_ms": timings,
            "query_sources": [m["id"] for m in history if m.get("retrieval_query")][-config.query_lookback:]
                             if config.query_lookback else [],
            "keyword": [e["id"] for e in keyword], "diagnostics": diagnostics}


class RetrievalEngine:
    def __init__(self, store, models):
        self.store, self.models = store, models
        self.cache = OrderedDict()
        self.dense_cache = OrderedDict()
        with store.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS retrieval_vectors (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS retrieval_indexes (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS retrieval_collections (session TEXT PRIMARY KEY, keys TEXT NOT NULL)")

    async def prepare(self, document, history, user_input, snapshot, settings, session, seed, *, turn_number=None, index_only=False):
        started = time.perf_counter()
        turn_number = turn_number or sum(m["role"] == "user" for m in session.get("messages", history)) + 1
        key = digest([document, [(m["id"], m.get("retrieval_query", "")) for m in history], user_input,
                      snapshot, settings.retrieval.model_dump(), settings.local_models.model_dump(), session.get("persona"), seed, turn_number])
        if not index_only and key in self.cache:
            return self.cache[key]
        async def embedding_query():
            started_query = time.perf_counter()
            if index_only or "embedding" not in settings.retrieval.channels:
                return None, None, 0
            try:
                query = await asyncio.to_thread(query_text, history, user_input, settings.retrieval)
                value = await self.models.infer("embedding", "embed", [query], settings,
                    timeout=settings.retrieval.embedding_timeout, query=True)
                return value, None, (time.perf_counter() - started_query) * 1000
            except Exception as error:
                return None, error, (time.perf_counter() - started_query) * 1000

        result, (query_vectors, query_error, query_ms) = await asyncio.gather(
            asyncio.to_thread(lexical_recall, document, history, user_input, snapshot, settings, session, seed,
                              turn_number=turn_number),
            embedding_query())
        ranks, entries = result["rankings"], result["entries"]
        entry_by_id = {e["id"]: e for e in entries}
        config = settings.retrieval
        index_key = digest([entries, config.model_dump(), "jieba-sudachi-v1"])
        with self.store.connection() as db:
            exists = db.execute("SELECT 1 FROM retrieval_indexes WHERE key=?", (index_key,)).fetchone()
        if not exists and "bm25" in ranks:
            _, sparse = await asyncio.to_thread(sparse_index, entries, config)
            with self.store.connection() as db:
                db.execute("INSERT OR IGNORE INTO retrieval_indexes VALUES (?,?)",
                           (index_key, json.dumps({"entries": [e["id"] for e in entries],
                                                  "fragments": sparse.fragments}, ensure_ascii=False)))
        if "embedding" in config.channels:
            result["channel_ms"]["embedding_query"] = round(query_ms, 2)
            try:
                config_key = digest([settings.local_models.embedding.model_dump(), self.models.revision(settings),
                                     config.chunk_tokens, config.chunk_overlap])
                with self.store.connection() as db:
                    # A queued index task may finish after its session was deleted.
                    # Check ownership in the same write statement, not in a racy preflight.
                    sid = session.get("id", "")
                    db.execute("""INSERT OR REPLACE INTO retrieval_collections(session,keys)
                        SELECT ?,? WHERE EXISTS (SELECT 1 FROM sessions WHERE id=?)""",
                               (sid, json.dumps([index_key] + [digest([config_key, e]) for e in entries]), sid))
                vectors, missing = [], []
                collection_key = digest([config_key, entries])
                if collection_key in self.dense_cache:
                    vectors = self.dense_cache[collection_key]
                    self.dense_cache.move_to_end(collection_key)
                else:
                    import numpy as np
                    with self.store.connection() as db:
                        for entry in entries:
                            cache_key = digest([config_key, entry])
                            row = db.execute("SELECT value FROM retrieval_vectors WHERE key=?", (cache_key,)).fetchone()
                            if row:
                                value = json.loads(row[0])
                                value["vectors"] = np.asarray(value["vectors"], dtype=np.float32)
                                vectors.append((entry["id"], value))
                            else:
                                missing.append((cache_key, entry))
                    if not missing:
                        self.dense_cache[collection_key] = vectors
                        while len(self.dense_cache) > 4:
                            self.dense_cache.popitem(last=False)
                if missing:
                    self.models.schedule_index(self, missing, settings)
                    result["diagnostics"].append({"reason": "向量索引尚未完整就绪，暂时降级；构建完成后原子切换", "missing": len(missing)})
                if vectors and not missing and not index_only:
                    import numpy as np
                    if query_error:
                        raise query_error
                    q = query_vectors
                    scores = {}
                    result["fragment_hits"] = {}
                    for eid, value in vectors:
                        similarities = np.asarray(value["vectors"], dtype=np.float32) @ np.asarray(q[0])
                        scores[eid] = float(similarities.max())
                        order = np.argsort(-similarities)[:settings.local_models.rerank_fragments]
                        result["fragment_hits"][eid] = [value["fragments"][int(i)] for i in order]
                    scores = {eid: score for eid, score in scores.items() if score >= config.embedding_min_similarity}
                    result["embedding_scores"] = scores
                    ranks["embedding"] = sorted(scores, key=lambda eid: (-scores[eid], entry_order(entry_by_id[eid])))
            except Exception as error:
                if index_only:
                    raise
                result["diagnostics"].append({"reason": "向量召回降级", "detail": str(error)})
        if "embedding" not in config.channels:
            with self.store.connection() as db:
                db.execute("INSERT OR REPLACE INTO retrieval_collections VALUES (?,?)",
                           (session.get("id", ""), json.dumps([index_key])))
        if index_only:
            return result
        if not ranks:
            ranks["keyword"] = result["keyword"][:config.candidate_count]
            result["diagnostics"].append({"reason": "选定通道不可用，使用关键词应急召回"})
        # Gate before fusion/truncation so excluded candidates do not occupy the shortlist.
        from tavern.conversation.context import activate_lore
        eligible, gate_notes, _ = activate_lore(document, history, user_input, snapshot.get("lore", {}), seed,
            session_id=session.get("id", ""), recursive=False,
            turn_number=turn_number,
            timeline=(snapshot.get("state", {}).get("世界", {}) or {}).get("当前时间线", ""),
            retained=({r["id"] for r in snapshot.get("lore_injections", [])} |
                      {eid for eid, old in snapshot.get("lore", {}).items() if isinstance(old, dict) and old.get("present")})
                     if session.get("lore_injection_mode") == "conversation" else (),
            retrieval={"ids": list(dict.fromkeys(eid for ids in ranks.values() for eid in ids)), "diagnostics": []},
            query=result["query"])
        allowed = {e["id"] for e in eligible}
        result["diagnostics"].extend(n for n in gate_notes if n["id"] not in allowed)
        ranks = {name: [eid for eid in ids if eid in allowed][:config.candidate_count] for name, ids in ranks.items()}
        result["rankings"] = ranks
        result["fusion_method"] = "single" if len(ranks) == 1 else "weighted_rrf"
        if len(ranks) > 1:
            result["fusion_scores"] = fusion_scores(ranks, config)
        result["ids"] = fuse(ranks, entries, config)
        if settings.local_models.reranker_enabled and result["ids"]:
            rerank_started = time.perf_counter()
            try:
                by_id = {e["id"]: e for e in entries}
                pairs = [[result["query"], result.get("fragment_hits", {}).get(eid) or
                          " ".join([str(by_id[eid].get("comment", "")), *by_id[eid].get("keys", []),
                                    by_id[eid]["content"]])] for eid in result["ids"]]
                scores = await self.models.infer("reranker", "rerank", pairs, settings,
                                                 timeout=config.rerank_timeout)
                result["rerank_scores"] = dict(zip(result["ids"], scores, strict=True))
                result["ids"].sort(key=lambda eid: (-result["rerank_scores"][eid], entry_order(by_id[eid])))
                result["diagnostics"].extend({"id":eid, "reason":"重排相关性低于阈值，未注入",
                                              "score":result["rerank_scores"][eid]}
                                             for eid in result["ids"] if result["rerank_scores"][eid] < config.rerank_min_score)
                result["ids"] = [eid for eid in result["ids"] if result["rerank_scores"][eid] >= config.rerank_min_score]
            except Exception as error:
                # Failure must not turn an unverified semantic suggestion into an
                # injection. Keep only independently supported lexical candidates.
                supported = set(result["lexical_ids"]) | set(result["bm25_scores"])
                result["ids"] = [eid for eid in result["ids"] if eid in supported]
                result["diagnostics"].append({"reason": "重排不可用，仅保留独立词法证据", "detail": str(error)})
            result["channel_ms"]["reranker"] = round((time.perf_counter() - rerank_started) * 1000, 2)
        # All expansion precedes compilation and passes the original-query gate.
        # Examined IDs include rejected candidates: a rejection is final this turn.
        from tavern.search.lore_admission import recursive_candidates
        examined = set(eid for ids in ranks.values() for eid in ids)
        frontier = list(result["ids"])
        for depth in range(1, settings.lore_recursion_depth + 1 if settings.lore_recursive else 1):
            if not frontier or len(result["ids"]) >= config.fusion_count:
                break
            expanded = recursive_candidates(entries, frontier, examined, depth)
            candidate_ids = list(expanded)[:config.candidate_count]
            examined.update(candidate_ids)
            if not candidate_ids:
                break
            eligible, notes, _ = activate_lore(
                document, history, user_input, snapshot.get("lore", {}), seed,
                session_id=session.get("id", ""), recursive=False, turn_number=turn_number,
                timeline=(snapshot.get("state", {}).get("世界", {}) or {}).get("当前时间线", ""),
                retained={r["id"] for r in snapshot.get("lore_injections", [])}
                         if session.get("lore_injection_mode") == "conversation" else (),
                retrieval={"ids": candidate_ids}, query=result["query"])
            allowed = {e["id"] for e in eligible}
            result["diagnostics"].extend(notes)
            candidate_ids = [eid for eid in candidate_ids if eid in allowed]
            supported = set(result["lexical_ids"]) | set(result["bm25_scores"])
            accepted = [eid for eid in candidate_ids if eid in supported]
            if settings.local_models.reranker_enabled and candidate_ids:
                try:
                    scores = await self.models.infer("reranker", "rerank",
                        [[result["query"], entry_by_id[eid]["content"]] for eid in candidate_ids], settings,
                        timeout=config.rerank_timeout)
                    scored = dict(zip(candidate_ids, scores, strict=True))
                    result.setdefault("rerank_scores", {}).update(scored)
                    accepted = [eid for eid in candidate_ids if scored[eid] >= config.rerank_min_score]
                    accepted.sort(key=lambda eid: -scored[eid])
                except Exception as error:
                    result["diagnostics"].append({"reason": "递归重排不可用，仅保留独立词法证据", "detail": str(error)})
            for eid in candidate_ids:
                result["evidence"].setdefault(eid, {}).update(expanded[eid])
                if eid not in accepted:
                    result["diagnostics"].append({"id": eid, "reason": "递归候选与原始查询相关性不足，未注入"})
            frontier = accepted[:max(0, config.fusion_count - len(result["ids"]))]
            result["ids"].extend(frontier)
        for eid in result["ids"]:
            result["evidence"].setdefault(eid, {}).update(
                channels=[name for name, ids in ranks.items() if eid in ids],
                admitted=True, bm25_score=result["bm25_scores"].get(eid),
                embedding_score=result.get("embedding_scores", {}).get(eid),
                rerank_score=result.get("rerank_scores", {}).get(eid))
        if "fragment_hits" in result:
            result["fragment_hits"] = {eid: hits for eid, hits in result["fragment_hits"].items() if eid in result["ids"]}
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        self.cache[key] = result
        while len(self.cache) > 32:
            self.cache.popitem(last=False)
        return result
