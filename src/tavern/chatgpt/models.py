"""Account-advertised capabilities intersected with the direct Responses contract."""

DIRECT_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


def model_details(item):
    result = {"id": item["slug"], "display_name": item.get("display_name") or item["slug"]}
    levels = item.get("supported_reasoning_levels")
    if isinstance(levels, list):
        supported, unavailable, seen = [], [], set()
        for level in levels:
            if (
                not isinstance(level, dict)
                or not isinstance(level.get("effort"), str)
                or level["effort"] in seen
            ):
                continue
            effort = level["effort"]
            seen.add(effort)
            if effort in DIRECT_EFFORTS:
                supported.append({"effort": effort, "description": str(level.get("description", ""))})
            else:
                unavailable.append({"effort": effort, "reason": "当前订阅直连接口不支持此档位"})
        result["supported_reasoning_levels"] = supported
        result["unavailable_reasoning_levels"] = unavailable
        if item.get("default_reasoning_level") in {level["effort"] for level in supported}:
            result["default_reasoning_level"] = item["default_reasoning_level"]
    return result
