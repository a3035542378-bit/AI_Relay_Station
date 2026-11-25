import asyncio

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from balance import balance_router, balances


@pytest.mark.parametrize("mode", ["ok", "invalid", "denied", "timeout"])
def test_provider_contract(mode):
    def reply(request):
        assert str(request.url) == "https://api.deepseek.com/user/balance"
        assert request.headers["authorization"] == "Bearer test"
        if mode == "timeout":
            raise httpx.ReadTimeout("fixture")
        if mode == "denied":
            return httpx.Response(401)
        if mode == "invalid":
            return httpx.Response(200, json={"balance_infos": []})
        return httpx.Response(200, json={"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "28.50"}
        ]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            return await balances({
                "OPENAI_API_BASE_URLS": ["https://api.deepseek.com/v1", "https://example.org"],
                "OPENAI_API_KEYS": ["test"],
            }, client)
    first, other = asyncio.run(run())
    assert "message" in other
    if mode == "ok":
        assert first["balance"]["balance_infos"][0]["total_balance"] == "28.50"
    else:
        assert "message" in first


def test_admin_required():
    async def denied():
        raise HTTPException(403)
    app = FastAPI()
    app.include_router(balance_router(denied, lambda: None))
    with TestClient(app) as client:
        assert client.get("/office-balance/data").status_code == 403
        assert "模型账户余额" in client.get("/office-balance").text


def test_kimi_available_balance():
    def reply(request):
        assert str(request.url) == "https://api.moonshot.cn/v1/users/me/balance"
        return httpx.Response(200, json={"code": 0, "status": True, "data": {"available_balance": "20.15"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            return await balances({"OPENAI_API_BASE_URLS": ["https://api.moonshot.cn/v1"],
                                   "OPENAI_API_KEYS": ["fixture"]}, client)
    entry, = asyncio.run(run())
    assert entry["name"] == "Kimi"
    assert entry["balance"]["balance_infos"] == [{"currency": "CNY", "total_balance": "20.15"}]
