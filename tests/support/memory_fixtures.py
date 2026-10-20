"""Source-valid deterministic Tracker proposals for orchestration tests.

These fixtures verify lifecycle mechanics, not model semantic accuracy. Actual
semantic evaluation uses separate synthetic stories and real model responses.
"""

import json


def memory_output(messages, content="过去的剧情", *, invalid=False):
    payload = json.loads(messages[-1]["content"])
    source = payload["messages"][-1]
    return json.dumps(
        {
            "operations": [
                {
                    "op": "add",
                    "record": {
                        "category": "episode",
                        "content": content,
                        "sources": ["S999999" if invalid else source["passages"][0]["ref"]],
                    },
                }
            ],
            "questions": [],
        },
        ensure_ascii=False,
    )


def memory_snapshot(*texts):
    from tavern.memories.memory_records import Record, install_memory

    snapshot = {}
    install_memory(
        snapshot,
        {
            "version": 2,
            "records": [Record(category="manual", content=text, manual=True).model_dump() for text in texts],
        },
    )
    return snapshot


def wire_proposal(messages, proposal):
    """Build a simulated model response using only handles exposed by this request."""
    import copy

    result = copy.deepcopy(proposal)
    for op in result["operations"]:
        record = op.get("record", {})
        record["sources"] = [
            next(
                ref
                for ref, item in messages.references.sources.items()
                if item["message_id"] == source["message_id"] and source["excerpt"] in item["excerpt"]
            )
            for source in record.get("sources", [])
        ]
    return json.dumps(result, ensure_ascii=False)
