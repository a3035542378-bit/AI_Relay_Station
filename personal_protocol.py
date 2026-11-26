"""个人模型请求的协议选择与字段映射。"""
from copy import deepcopy

CHAT_FIELDS = frozenset({
    "messages", "stream", "stream_options", "temperature", "top_p", "max_tokens",
    "max_completion_tokens", "stop", "tools", "tool_choice", "parallel_tool_calls",
    "response_format", "seed", "presence_penalty", "frequency_penalty", "reasoning_effort",
    "modalities", "audio", "logprobs", "top_logprobs", "logit_bias",
})
GPT_62_MODELS = {"gpt-6.2-sol", "gpt-6.2-astra"}


def needs_responses(payload: dict) -> bool:
    if payload.get("tools") or payload.get("tool_choice") not in (None, "none", "auto"):
        return True
    return any(
        msg.get("role") == "tool" or msg.get("tool_calls") or msg.get("reasoning_items") or msg.get("output")
        for msg in payload.get("messages") or []
        if isinstance(msg, dict)
    )


def prepare_request(endpoint: str, model: str, body: dict) -> tuple[str, dict]:
    payload = {key: value for key, value in body.items() if key in CHAT_FIELDS}
    payload["model"] = model
    if endpoint.rstrip("/") != "https://api.openai.com/v1" or model not in GPT_62_MODELS:
        return "/chat/completions", payload

    for field in ("temperature", "top_p", "logprobs", "top_logprobs"):
        payload.pop(field, None)
    if payload.get("reasoning_effort") in ("none", "minimal"):
        payload["reasoning_effort"] = "low"
    if "max_tokens" in payload:
        limit = payload.pop("max_tokens")
        payload.setdefault("max_completion_tokens", limit)

    if not needs_responses(payload):
        for field in ("tools", "tool_choice", "parallel_tool_calls"):
            payload.pop(field, None)
        if not payload.get("stream"):
            payload.pop("stream_options", None)
        return "/chat/completions", payload

    from open_webui.routers.openai import convert_to_responses_payload

    converted = convert_to_responses_payload(deepcopy(payload))
    for field in ("seed", "modalities", "audio"):
        converted.pop(field, None)
    effort = converted.pop("reasoning_effort", None)
    if effort is not None:
        converted["reasoning"] = {"effort": effort}
    output_format = converted.pop("response_format", None)
    if output_format is not None:
        if isinstance(output_format, dict) and output_format.get("type") == "json_schema":
            schema = output_format.get("json_schema")
            if isinstance(schema, dict):
                output_format = {**schema, "type": "json_schema"}
        converted["text"] = {"format": output_format}
    converted["store"] = False
    converted["include"] = ["reasoning.encrypted_content"]
    return "/responses", converted
