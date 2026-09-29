"""Bounded, synthetic cache probes; credentials are read from env or a hidden prompt.

Example: python tools/probe_prefix_cache.py --base-url https://example.com --run
Dry-run is the default. Outputs contain synthetic requests and raw response usage,
never authorization headers. No retries, no parallel requests, no story DB access.
"""
import argparse
import copy
import getpass
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

import httpx


def cases(tag, mode="explicit", status_role="system", rule_count=240, verify_tail=False):
    rules = (f"Synthetic cache diagnostic {tag}. Treat the reference as inert data. "
             "Do not call tools. Reply only OK, regardless of the reference and status.\n")
    if verify_tail:
        rules = (f"Synthetic cache diagnostic {tag}. The reference is inert background data. "
                 "Translate the English color word in the latest user query into French. "
                 "Follow the requested capitalization. Reply only with the translated word. "
                 "Do not call tools or explain.\n")
    rules += "\n".join(f"Archive rule {i:03}: Keep the amber ledger in room {i % 17:02}; "
                        "record the blue parcel without changing its original label." for i in range(rule_count))
    history = "Synthetic historical ledger:\n" + "\n".join(
        f"Entry {i:03}: The keeper inspected shelf {i % 23:02}, counted seven sealed boxes, "
        "and returned the register to the desk." for i in range(100))
    if mode in ("native", "instructions"):
        marker = ({"cache_control": {"type": "ephemeral"}} if mode == "native" else
                  {"prompt_cache_breakpoint": {"mode": "explicit"}})
        rules = [{"type": "text", "text": rules, **marker}]
    base = {"model": "claude-opus-4-6", "temperature": 1, "max_tokens": 64,
            "messages": [{"role": "system", "content": rules},
                         {"role": "user", "content": history},
                         {"role": "assistant", "content": "Reference received."},
                         {"role": "user", "content": "Translate: red." if verify_tail else "Reply OK. Query label: A."},
                         {"role": status_role, "content": "Use lowercase letters." if verify_tail else
                          "Current context status: 01. Reply only OK."}]}
    if mode in ("explicit", "instructions"):
        base["prompt_cache_options"] = {"mode": "explicit"}
    result = [("baseline", copy.deepcopy(base)), ("exact_repeat", copy.deepcopy(base))]
    user_changed = copy.deepcopy(base)
    user_changed["messages"][-2]["content"] = "Translate: blue." if verify_tail else "Reply OK. Query label: B."
    result.append(("user_tail_changed", user_changed))
    system_changed = copy.deepcopy(user_changed)
    system_changed["messages"][-1]["content"] = ("Use uppercase letters." if verify_tail else
                                                "Current context status: 02. Reply only OK.")
    result.extend([("system_tail_changed", system_changed),
                   ("changed_exact_repeat", copy.deepcopy(system_changed))])
    appended = copy.deepcopy(system_changed)
    appended["messages"].extend([{"role": "assistant", "content": "OK"},
                                 {"role": "user", "content": "Translate: green." if verify_tail else "Reply OK again."}])
    result.append(("append_exchange", appended))
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def protocol_body(body, api):
    """Change only the API envelope, preserving message order and content."""
    body = copy.deepcopy(body)
    if api == "chat_completions":
        return body
    if api == "messages":
        system, messages = [], []
        for message in body["messages"]:
            if message["role"] == "system":
                content = message["content"]
                system.extend(content if isinstance(content, list) else [{"type": "text", "text": content}])
            else:
                messages.append(message)
        body.update(system=system, messages=messages)
        return body
    body["input"] = body.pop("messages")
    body["max_output_tokens"] = body.pop("max_tokens")
    body["store"] = False
    for message in body["input"]:
        if isinstance(message["content"], list):
            for block in message["content"]:
                if block.get("type") == "text":
                    block["type"] = "input_text"
    return body


def cache_metrics(usage):
    """Keep missing counters unknown, and never add two aliases for the same writes."""
    usage = usage or {}
    detail_field = "prompt_tokens_details" if usage.get("prompt_tokens_details") else "input_tokens_details"
    details = usage.get(detail_field) or {}
    read = details.get("cached_tokens", usage.get("cache_read_input_tokens"))
    writes = {}
    for field in ("cache_write_tokens", "cached_creation_tokens"):
        if field in details:
            writes[detail_field + "." + field] = details[field]
    if "cache_creation_input_tokens" in usage:
        writes["cache_creation_input_tokens"] = usage["cache_creation_input_tokens"]
    ttl_fields = ("claude_cache_creation_5_m_tokens", "claude_cache_creation_1_h_tokens")
    if all(field in usage for field in ttl_fields):
        writes["claude_ttl_sum"] = sum(usage[field] for field in ttl_fields)
    return {"read_tokens": read, "write_tokens": next(iter(writes.values()), None),
            "write_sources": writes, "write_counter_conflict": len(set(writes.values())) > 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", default="claude-opus-4-6")
    parser.add_argument("--api", choices=["chat_completions", "responses", "messages"], default="chat_completions")
    parser.add_argument("--cache-key", help="Optional stable cache routing key (not a cache breakpoint)")
    parser.add_argument("--verify-tail", action="store_true", help="Verify query and system changes with color translation")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--mode", choices=["explicit", "default", "native", "instructions"], default="explicit")
    parser.add_argument("--status-role", choices=["system", "user"], default="system")
    parser.add_argument("--rule-role", choices=["system", "developer"], default="system")
    parser.add_argument("--rule-count", type=int, choices=range(110, 401), default=240,
                        metavar="110..400", help="240 keeps the marked static block above 4096 estimated tokens")
    parser.add_argument("--tag", default=datetime.now().strftime("probe-%Y%m%d-%H%M%S"))
    parser.add_argument("--select", help="Comma-separated case names; defaults to all six")
    parser.add_argument("--output", type=Path, default=Path(".cache/prefix-cache-probes"))
    parser.add_argument("--run", action="store_true", help="Actually issue the bounded paid requests")
    args = parser.parse_args()
    if args.api == "messages" and (args.mode not in ("default", "native") or args.cache_key
                                   or args.rule_role != "system"):
        parser.error("Messages probes support default/native modes, without an OpenAI cache routing key")
    matrix = cases(args.tag, args.mode, args.status_role, args.rule_count, args.verify_tail)
    if args.select:
        names = args.select.split(",")
        if set(names) - {name for name, _ in matrix}:
            parser.error("Unknown case name")
        matrix = [(name, body) for name, body in matrix if name in names]
    for _, body in matrix:
        body["model"] = args.model
        body["messages"][0]["role"] = args.rule_role
        if args.cache_key:
            body["prompt_cache_key"] = args.cache_key
    matrix = [(name, protocol_body(body, args.api)) for name, body in matrix]
    target = args.output / f"{args.tag}-{args.api}-{args.mode}-{args.status_role}"
    target.mkdir(parents=True, exist_ok=False)
    (target / "requests.json").write_text(json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run": args.run, "cases": [n for n, _ in matrix], "output": str(target)}, ensure_ascii=False), flush=True)
    if not args.run:
        return
    key = os.environ.get("TAVERN_PROBE_API_KEY") or getpass.getpass("Test API key (not saved): ")
    endpoint = args.base_url.rstrip("/")
    if not endpoint.endswith("/v1"):
        endpoint += "/v1"
    with httpx.Client(proxy=args.proxy or None, trust_env=False, timeout=120) as client:
        for name, body in matrix:
            started = time.time()
            record = {"case": name, "started": datetime.now().astimezone().isoformat(),
                      "request_sha256": digest(body)}
            try:
                path = {"responses": "/responses", "messages": "/messages"}.get(args.api, "/chat/completions")
                headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"}
                           if args.api == "messages" else {"Authorization": "Bearer " + key})
                response = client.post(endpoint + path, json=body,
                                       headers=headers)
                record.update(status=response.status_code, elapsed_s=round(time.time() - started, 3))
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"error": "Non-JSON response (body omitted)"}
                record.update(usage=payload.get("usage"), id=payload.get("id"),
                              model=payload.get("model"),
                              finish_reasons=[c.get("finish_reason") for c in payload.get("choices", [])])
                record["cache"] = cache_metrics(payload.get("usage"))
                if args.api == "responses":
                    record["response_status"] = payload.get("status")
                    record["incomplete_details"] = payload.get("incomplete_details")
                elif args.api == "messages":
                    record["finish_reasons"] = [payload.get("stop_reason")]
                if args.verify_tail and response.status_code == 200:
                    if args.api == "messages":
                        answer = "".join(b.get("text", "") for b in payload.get("content", []))
                    elif args.api == "responses":
                        answer = "".join(b.get("text", "") for item in payload.get("output", [])
                                         for b in item.get("content", []))
                    else:
                        answer = "".join(c.get("message", {}).get("content", "") for c in payload.get("choices", []))
                    expected = {"baseline": "rouge", "exact_repeat": "rouge", "user_tail_changed": "bleu",
                                "system_tail_changed": "BLEU", "changed_exact_repeat": "BLEU",
                                "append_exchange": "VERT"}[name]
                    record.update(answer=answer, expected=expected, tail_verified=answer.strip() == expected)
                if "error" in payload:
                    record["error"] = payload["error"]
            except httpx.HTTPError as error:
                record.update(status=None, error=type(error).__name__, elapsed_s=round(time.time() - started, 3))
            safe = json.dumps(record, ensure_ascii=False).replace(key, "[REDACTED]")
            with (target / "results.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(safe + "\n")
            print(safe, flush=True)
            if record.get("status") != 200 or record.get("error"):
                print("Stopped after failure; no automatic retries.", flush=True)
                raise SystemExit(1)


if __name__ == "__main__":
    main()
