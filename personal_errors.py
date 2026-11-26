"""Translate provider failures without exposing provider payloads."""
import json

import httpx
from fastapi import HTTPException

_BODY_LIMIT = 8192

_ERROR_CODES = {
    "invalid_api_key": "服务商拒绝了密钥，请检查此连接的密钥。",
    "authentication_error": "服务商拒绝了身份验证，请检查此连接的密钥。",
    "model_not_found": "服务商找不到此模型，或当前密钥没有该模型的访问权限。",
    "insufficient_quota": "此连接的服务商额度不足，请查看服务商账户的余额及限额。",
    "billing_not_active": "此连接的服务商账户尚未启用计费。",
    "rate_limit_exceeded": "服务商限制了请求频率，请稍后重试。",
    "context_length_exceeded": "消息或附件超出了模型上下文长度，请缩短后重试。",
    "unsupported_country_region_territory": "服务商限制了此请求所在地区的访问。",
}
_STATUS_DETAILS = {
    400: "服务商拒绝了请求，请检查模型支持的参数和消息格式。",
    401: "服务商拒绝了身份验证，请检查此连接的密钥。",
    403: "服务商拒绝访问，请检查此连接的访问权限。",
    404: "服务商找不到接口或模型，请检查服务地址和模型 ID。",
    429: "服务商限制了请求，请检查调用频率和账户额度。",
}
_PUBLIC_PARAMS = frozenset({
    "model", "messages", "stream", "stream_options", "temperature", "top_p",
    "max_tokens", "max_completion_tokens", "stop", "tools", "tool_choice",
    "parallel_tool_calls", "response_format", "seed", "presence_penalty",
    "frequency_penalty", "reasoning_effort", "modalities", "audio", "logprobs",
    "top_logprobs", "logit_bias",
})


async def provider_error(response: httpx.Response) -> HTTPException:
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes(chunk_size=_BODY_LIMIT):
            body.extend(chunk)
            if len(body) == _BODY_LIMIT:
                break
    except httpx.HTTPError:
        body.clear()

    status = response.status_code
    detail = _STATUS_DETAILS.get(status, "服务商未能处理请求。")
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        for field in ("code", "type"):
            value = error.get(field)
            if isinstance(value, str) and value in _ERROR_CODES:
                detail = _ERROR_CODES[value]
                break
        else:
            param = error.get("param")
            if status in (400, 422) and isinstance(param, str) and param in _PUBLIC_PARAMS:
                detail = f"服务商拒绝了参数 {param}，请检查该模型支持的参数和值。"
    elif body and payload is None:
        if b"cloudflare" in body.lower():
            detail = "Cloudflare 网关返回了错误，请检查中转站到服务商的网络连接。"
        elif "text/html" in response.headers.get("content-type", "").lower() or body.lstrip().startswith(b"<"):
            detail = "上游返回了网页错误，请检查中转站到服务商的网络连接和服务地址。"

    return HTTPException(502, f"个人模型服务返回 HTTP {status}。{detail}")
