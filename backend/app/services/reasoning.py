"""Reasoning fields shared by account inference and connection checks."""


class ReasoningError(ValueError):
    pass


def reasoning_payload(protocol: str, model: dict, level: str | None = None) -> dict:
    capability = model.get("reasoning") or {}
    if not capability.get("supported"):
        if level is not None:
            raise ReasoningError("REASONING_UNSUPPORTED：当前模型不支持思考强度。")
        return {}
    levels = capability.get("levels") or []
    if level is None:
        level = capability.get("default_level")
        if level is None and levels:
            level = levels[0]["label"]
    for item in levels:
        if item["label"] == level:
            value = item["api_value"]
            if protocol == "responses" or capability.get("parameter") == "reasoning.effort":
                return {"reasoning": {"effort": value}}
            return {"reasoning_effort": value}
    raise ReasoningError("REASONING_LEVEL_UNSUPPORTED：当前模型不支持该思考档位。")
