import logging
from decimal import Decimal
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger("office.balance")
ENDPOINTS = {
    "https://api.deepseek.com": ("DeepSeek", "https://api.deepseek.com/user/balance"),
    "https://api.deepseek.com/v1": ("DeepSeek", "https://api.deepseek.com/user/balance"),
    "https://api.moonshot.cn/v1": ("Kimi", "https://api.moonshot.cn/v1/users/me/balance"),
}


class Amount(BaseModel):
    currency: str = Field(min_length=1, max_length=12)
    total_balance: Decimal = Field(allow_inf_nan=False)


class Balance(BaseModel):
    is_available: bool
    balance_infos: list[Amount] = Field(min_length=1)


class KimiAccount(BaseModel):
    available_balance: Decimal = Field(allow_inf_nan=False)


class KimiResponse(BaseModel):
    code: Literal[0]
    status: Literal[True]
    data: KimiAccount


class ProviderBalanceAdapter:
    async def query(self, client: httpx.AsyncClient, key: str, provider: str, endpoint: str) -> Balance:
        try:
            response = await client.get(
                endpoint,
                headers={"Authorization": f"Bearer {key}"},
            )
            response.raise_for_status()
            if provider == "Kimi":
                amount = KimiResponse.model_validate_json(response.content).data.available_balance
                return Balance(is_available=amount > 0, balance_infos=[Amount(currency="CNY", total_balance=amount)])
            return Balance.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError) as error:
            log.warning("Balance provider failure (%s)", type(error).__name__)
            raise HTTPException(502, "余额查询失败，请检查服务商状态和密钥权限。") from error


async def balances(config: dict, client: httpx.AsyncClient) -> list[dict]:
    result = []
    keys = config.get("OPENAI_API_KEYS") or []
    settings = config.get("OPENAI_API_CONFIGS") or {}
    for index, url in enumerate(config.get("OPENAI_API_BASE_URLS") or []):
        options = settings.get(str(index)) or {}
        if not options.get("enable", True):
            continue
        item = {"name": options.get("name") or f"模型连接 {index + 1}"}
        provider = ENDPOINTS.get(url.rstrip("/"))
        if provider and not options.get("name"):
            item["name"] = provider[0]
        if not provider:
            item["message"] = "此服务商尚未接入余额查询"
        elif index >= len(keys) or not keys[index]:
            item["message"] = "请先在模型连接中填写 API Key"
        else:
            try:
                data = await ProviderBalanceAdapter().query(client, keys[index], *provider)
                item["balance"] = data.model_dump(mode="json")
            except HTTPException as error:
                item["message"] = error.detail
        result.append(item)
    return result


def balance_router(admin_dependency, config_dependency):
    router = APIRouter()

    @router.get("/office-balance", include_in_schema=False)
    async def page():
        return FileResponse(Path(__file__).with_name("balance.html"))

    @router.get("/office-balance/data", dependencies=[Depends(admin_dependency)])
    async def read_balance():
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            return await balances(await config_dependency(), client)

    return router
