import asyncio

import httpx
import pytest

from personal_errors import provider_error


@pytest.mark.parametrize(("status", "error", "expected"), [
    (400, {"code": "unsupported_parameter", "param": "temperature"}, "参数 temperature"),
    (404, {"code": "model_not_found"}, "访问权限"),
    (429, {"type": "insufficient_quota"}, "额度不足"),
    (401, {"code": "invalid_api_key"}, "拒绝了密钥"),
    (400, {"type": {}, "code": [], "param": "secret-example"}, "请求"),
])
def test_structured_errors(status, error, expected):
    response = httpx.Response(status, json={"error": {**error, "message": "secret-example"}})
    translated = asyncio.run(provider_error(response))
    assert translated.status_code == 502
    assert f"HTTP {status}" in translated.detail
    assert expected in translated.detail
    assert "secret-example" not in translated.detail


@pytest.mark.parametrize(("body", "detail"), [
    (b"<html>CloudFlare: example-key</html>", "Cloudflare 网关"),
    (b"<html>other error: example-key</html>", "网页错误"),
    (b'{"error":{"message":"Cloudflare example-key"}}', "服务商拒绝了请求"),
])
def test_gateway_documents(body, detail):
    error = asyncio.run(provider_error(httpx.Response(400, content=body)))
    assert detail in error.detail
    assert "example-key" not in error.detail


def test_stops_after_eight_kib():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(4):
                yield b"x" * 2048
            raise AssertionError("read beyond preview")

    error = asyncio.run(provider_error(httpx.Response(400, stream=Stream())))
    assert error.status_code == 502
    assert "HTTP 400" in error.detail
