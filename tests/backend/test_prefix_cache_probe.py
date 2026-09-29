"""Ensure the paid diagnostic changes only its declared experimental variable."""
from tools.probe_prefix_cache import cache_metrics, cases, protocol_body


def test_probe_controlled_differences():
    matrix = dict(cases("fixed"))
    a = matrix["baseline"]
    assert a == matrix["exact_repeat"]
    b = matrix["user_tail_changed"]
    assert [i for i, (x, y) in enumerate(zip(a["messages"], b["messages"])) if x != y] == [3]
    c = matrix["system_tail_changed"]
    assert [i for i, (x, y) in enumerate(zip(b["messages"], c["messages"])) if x != y] == [4]
    assert c == matrix["changed_exact_repeat"]
    assert matrix["append_exchange"]["messages"][:-2] == c["messages"]
    assert all(body["max_tokens"] == 64 for body in matrix.values())


def test_probe_markers_are_separate_protocol_experiments():
    native = dict(cases("fixed", "native"))["baseline"]
    instructions = dict(cases("fixed", "instructions"))["baseline"]
    default = dict(cases("fixed", "default"))["baseline"]
    assert native["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "prompt_cache_options" not in native
    assert instructions["messages"][0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert instructions["prompt_cache_options"] == {"mode": "explicit"}
    assert "prompt_cache_options" not in default


def test_status_role_comparison_changes_only_role():
    system = cases("fixed", status_role="system")
    user = cases("fixed", status_role="user")
    for (name_a, a), (name_b, b) in zip(system, user):
        assert name_a == name_b
        assert a["messages"][4]["role"] == "system"
        assert b["messages"][4]["role"] == "user"
        b["messages"][4]["role"] = "system"
        assert a == b


def test_cache_metrics_observed_relay_aliases_without_double_counting():
    metrics = cache_metrics({"prompt_tokens_details": {"cached_tokens": 66, "cached_creation_tokens": 5225},
                             "claude_cache_creation_5_m_tokens": 5225, "claude_cache_creation_1_h_tokens": 0})
    assert metrics["read_tokens"] == 66
    assert metrics["write_tokens"] == 5225
    assert not metrics["write_counter_conflict"]
    assert cache_metrics({})["write_tokens"] is None
    assert cache_metrics({"claude_cache_creation_5_m_tokens": 0,
                          "claude_cache_creation_1_h_tokens": 0})["write_tokens"] == 0
    assert cache_metrics({"prompt_tokens_details": {"cache_write_tokens": 0, "cached_creation_tokens": 10}})[
        "write_counter_conflict"]


def test_responses_conversion_preserves_order_and_explicit_marker():
    original = dict(cases("fixed", "instructions"))["baseline"]
    result = protocol_body(original, "responses")
    assert "messages" not in result and "max_tokens" not in result
    assert result["max_output_tokens"] == 64 and result["store"] is False
    assert result["input"][1:] == original["messages"][1:]
    assert result["input"][0]["content"][0]["type"] == "input_text"
    assert result["input"][0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert original["messages"][0]["content"][0]["type"] == "text"
    assert cache_metrics({"input_tokens_details": {"cached_tokens": 123}})["read_tokens"] == 123
    assert cache_metrics({"input_tokens_details": {"cache_write_tokens": 7}})["write_sources"] == {
        "input_tokens_details.cache_write_tokens": 7}


def test_native_messages_keeps_static_system_breakpoint_separate_from_dynamic_system():
    original = dict(cases("fixed", "native"))["baseline"]
    result = protocol_body(original, "messages")
    assert len(result["system"]) == 2
    assert result["system"][0] == original["messages"][0]["content"][0]
    assert result["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in result["system"][1]
    assert result["messages"] == original["messages"][1:4]
    assert cache_metrics({"cache_read_input_tokens": 123, "cache_creation_input_tokens": 456})[
        "write_tokens"] == 456


def test_semantic_probe_has_observable_user_and_system_changes():
    matrix = {name: protocol_body(body, "messages")
              for name, body in cases("fixed", "native", verify_tail=True)}
    assert matrix["baseline"]["messages"][-1]["content"] == "Translate: red."
    assert matrix["user_tail_changed"]["messages"][-1]["content"] == "Translate: blue."
    assert matrix["user_tail_changed"]["system"][-1]["text"] == "Use lowercase letters."
    assert matrix["system_tail_changed"]["system"][-1]["text"] == "Use uppercase letters."
    assert matrix["append_exchange"]["messages"][-1]["content"] == "Translate: green."
    assert len({digest["system"][0]["text"] for digest in matrix.values()}) == 1


def test_responses_developer_marker_is_not_hoisted_into_instructions():
    original = dict(cases("fixed", "instructions"))["baseline"]
    original["messages"][0]["role"] = "developer"
    result = protocol_body(original, "responses")
    assert "instructions" not in result
    assert result["input"][0]["role"] == "developer"
    assert result["input"][0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert result["input"][-1] == original["messages"][-1]
