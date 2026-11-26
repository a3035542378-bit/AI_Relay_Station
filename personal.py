"""Account-owned OpenAI-compatible chat models."""
import asyncio
import base64
import hashlib
import ipaddress
import logging
import os
from pathlib import Path
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import Column, MetaData, String, Table, delete, insert, select, update
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.background import BackgroundTask

log = logging.getLogger("office.personal")
metadata = MetaData()
models = Table(
    "personal_model", metadata,
    Column("id", String, primary_key=True), Column("owner", String, nullable=False, index=True),
    Column("name", String, nullable=False), Column("endpoint", String, nullable=False),
    Column("model", String, nullable=False), Column("credential", String, nullable=False),
)


class Settings(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    endpoint: str = Field(max_length=1024)
    model: str = Field(min_length=1, max_length=200)
    key: SecretStr | None = Field(default=None, max_length=8192)

    @field_validator("name", "model")
    @classmethod
    def clean(cls, value):
        if not value.strip():
            raise ValueError("不能为空")
        return value.strip()

    @field_validator("endpoint")
    @classmethod
    def public_https(cls, value):
        try:
            url = httpx.URL(value.strip())
        except httpx.InvalidURL as error:
            raise ValueError("无效的服务地址") from error
        if (url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment
                or url.host.endswith(".") or "@" in url.netloc.decode()):
            raise ValueError("请填写公网 HTTPS Base URL，不含账号、查询参数或片段")
        return str(url).rstrip("/")


class Repository:
    def __init__(self, path):
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}", hide_parameters=True)
        self.lock = asyncio.Lock()
        self.ready = False

    async def initialize(self):
        if not self.ready:
            async with self.lock:
                if not self.ready:
                    async with self.engine.begin() as db:
                        await db.run_sync(metadata.create_all)
                    self.ready = True

    async def records(self, owner, model_id=None):
        await self.initialize()
        query = select(models).where(models.c.owner == owner)
        if model_id is not None:
            query = query.where(models.c.id == model_id)
        async with self.engine.connect() as db:
            return (await db.execute(query)).mappings().all()

    async def save(self, owner, model_id, settings):
        await self.initialize()
        values = settings.model_dump(exclude={"key"})
        password = settings.key.get_secret_value() if settings.key else ""
        if password:
            values["credential"] = cipher().encrypt(password.encode()).decode()
        async with self.engine.begin() as db:
            if model_id is None:
                if not password:
                    raise HTTPException(422, "首次添加需要填写 API Key")
                model_id = "personal:" + uuid4().hex
                await db.execute(insert(models).values(**values, owner=owner, id=model_id))
            else:
                condition = (models.c.owner == owner) & (models.c.id == model_id)
                if not password:
                    # 一条原子更新，避免旧密钥被带到另一个服务地址。
                    condition &= models.c.endpoint == settings.endpoint
                result = await db.execute(update(models).where(condition).values(**values))
                if not result.rowcount:
                    raise HTTPException(404, "未找到模型，或更换地址时需要重新输入密钥")
        return model_id

    async def remove(self, owner, model_id):
        await self.initialize()
        async with self.engine.begin() as db:
            result = await db.execute(delete(models).where(models.c.owner == owner, models.c.id == model_id))
            if not result.rowcount:
                raise HTTPException(404, "未找到模型")


def cipher():
    material = b"office-personal-models/v1:" + os.environ["WEBUI_SECRET_KEY"].encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))


repo = Repository(Path(os.environ.get("DATA_DIR", ".")) / "personal-models.sqlite3")


def public_record(row):
    return {key: row[key] for key in ("id", "name", "endpoint", "model")}


async def catalog(user, model_id=None):
    rows = await repo.records(user.id, model_id)
    return [{"id": row["id"], "name": row["name"], "owner_id": user.id,
             "object": "model", "owned_by": "openai", "tags": [{"name": "个人模型"}]}
            for row in rows]


async def personal_model(user, model_id):
    entries = await catalog(user, model_id)
    return entries[0] if entries else None


async def public_target(client, endpoint):
    # 服务使用 fake-IP DNS；查询真实 A 记录并固定 TLS 连接目标，防止地址重绑定。
    url = httpx.URL(endpoint)
    host = url.raw_host.decode()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        dns = await client.get("https://dns.google/resolve", params={"name": host, "type": "A"})
        dns.raise_for_status()
        answers = [ipaddress.ip_address(a["data"]) for a in dns.json().get("Answer", []) if a.get("type") == 1]
        if not answers or not all(valid_address(ip) for ip in answers):
            raise HTTPException(422, "服务地址需要解析到公网 IPv4")
        address = answers[0]
    if not valid_address(address):
        raise HTTPException(422, "模型服务地址必须是公网地址")
    return url.copy_with(host=str(address)), url.netloc.decode(), host


def valid_address(address):
    return (address.is_global and not address.is_multicast and not address.is_reserved
            and not getattr(address, "ipv4_mapped", None))


class ProviderAdapter:
    async def complete(self, row, body):
        client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15), trust_env=False)
        streaming = False
        try:
            url, authority, hostname = await public_target(client, row["endpoint"] + "/chat/completions")
            allowed = {"messages", "stream", "stream_options", "temperature", "top_p", "max_tokens",
                       "max_completion_tokens", "stop", "tools", "tool_choice", "parallel_tool_calls",
                       "response_format", "seed", "presence_penalty", "frequency_penalty", "reasoning_effort",
                       "modalities", "audio", "logprobs", "top_logprobs", "logit_bias"}
            payload = {k: v for k, v in body.items() if k in allowed}
            payload["model"] = row["model"]
            key = cipher().decrypt(row["credential"].encode()).decode()
            req = client.build_request("POST", url, json=payload, headers={
                "Host": authority, "Authorization": "Bearer " + key,
            }, extensions={"sni_hostname": hostname})
            response = await client.send(req, stream=True)
            if not response.is_success:
                log.warning("Private model returned status %d", response.status_code)
                raise HTTPException(502, f"个人模型服务返回 HTTP {response.status_code}，请检查模型和密钥")
            if body.get("stream"):
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raise HTTPException(502, "模型服务未返回聊天事件流")
                async def events():
                    try:
                        async for chunk in response.aiter_bytes():
                            yield chunk
                    finally:
                        await client.aclose()
                streaming = True
                return StreamingResponse(events(), media_type="text/event-stream",
                                         background=BackgroundTask(client.aclose))
            await response.aread()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Private model request failed (%s)", type(exc).__name__)
            raise HTTPException(502, "无法连接个人模型服务或响应格式有误") from exc
        finally:
            if not streaming:
                await client.aclose()


async def complete(user, body):
    rows = await repo.records(user.id, body["model"])
    if not rows:
        raise HTTPException(404, "未找到个人模型")
    return await ProviderAdapter().complete(rows[0], body)


def personal_router(verified_user):
    router = APIRouter()

    @router.get("/personal-models", include_in_schema=False)
    async def page():
        return RedirectResponse("/?settings=connections", status_code=307)

    @router.get("/api/office/personal-models")
    async def list_models(user=Depends(verified_user)):
        return [public_record(row) for row in await repo.records(user.id)]

    @router.post("/api/office/personal-models")
    async def add(settings: Settings, user=Depends(verified_user)):
        return {"id": await repo.save(user.id, None, settings)}

    @router.put("/api/office/personal-models/{model_id}")
    async def edit(model_id: str, settings: Settings, user=Depends(verified_user)):
        return {"id": await repo.save(user.id, model_id, settings)}

    @router.delete("/api/office/personal-models/{model_id}")
    async def remove(model_id: str, user=Depends(verified_user)):
        await repo.remove(user.id, model_id)
        return {"deleted": True}

    return router
