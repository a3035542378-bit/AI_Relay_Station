"""Account-owned OpenAI-compatible chat models."""
import asyncio
import base64
import hashlib
import ipaddress
import json
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

from personal_errors import provider_error
from personal_protocol import prepare_request

log = logging.getLogger("office.personal")
metadata = MetaData()
models = Table(
    "personal_model", metadata,
    Column("id", String, primary_key=True), Column("owner", String, nullable=False, index=True),
    Column("name", String, nullable=False), Column("endpoint", String, nullable=False),
    Column("model", String, nullable=False), Column("credential", String, nullable=False),
)


class ConnectionInput(BaseModel):
    endpoint: str = Field(max_length=1024)
    key: SecretStr | None = Field(default=None, max_length=8192)

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


class DiscoveryInput(ConnectionInput):
    connection_id: str | None = Field(default=None, max_length=100)


class Settings(ConnectionInput):
    name: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)

    @field_validator("name", "model")
    @classmethod
    def clean(cls, value):
        if not value.strip():
            raise ValueError("不能为空")
        return value.strip()


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


def provider_headers(hostname, key, *, listing=False):
    if hostname == "api.xiaomimimo.com":
        return {"api-key": key}
    if listing and hostname == "api.anthropic.com":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": "Bearer " + key}


class ProviderAdapter:
    async def list_models(self, endpoint, key):
        try:
            async with asyncio.timeout(15), httpx.AsyncClient(timeout=10, trust_env=False) as client:
                url, authority, hostname = await public_target(client, endpoint + "/models")
                headers = provider_headers(hostname, key, listing=True)
                headers.update({"Host": authority, "Accept": "application/json", "Accept-Encoding": "identity"})
                async with client.stream("GET", url, headers=headers, extensions={"sni_hostname": hostname}) as response:
                    if not response.is_success:
                        raise await provider_error(response)
                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        content.extend(chunk)
                        if len(content) > 4 * 1024 * 1024:
                            raise HTTPException(502, "服务商模型列表过大")
            payload = json.loads(content)
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise ValueError("model list format")
            ids = {row["id"].strip() for row in rows if isinstance(row, dict)
                   and isinstance(row.get("id"), str) and 0 < len(row["id"].strip()) <= 200}
            return sorted(ids)
        except (httpx.HTTPError, ValueError, TimeoutError, RecursionError) as error:
            log.warning("Model discovery failed (%s)", type(error).__name__)
            raise HTTPException(502, "服务商未返回可读取的模型列表") from error

    async def complete(self, row, body):
        client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15), trust_env=False)
        streaming = False
        try:
            route, payload = prepare_request(row["endpoint"], row["model"], body)
            url, authority, hostname = await public_target(client, row["endpoint"] + route)
            key = cipher().decrypt(row["credential"].encode()).decode()
            req = client.build_request("POST", url, json=payload, headers={
                "Host": authority, **provider_headers(hostname, key),
            }, extensions={"sni_hostname": hostname})
            response = await client.send(req, stream=True)
            if not response.is_success:
                log.warning("Private model returned status %d", response.status_code)
                raise await provider_error(response)
            if body.get("stream"):
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raise HTTPException(502, "模型服务未返回聊天事件流")
                async def events():
                    try:
                        # WebUI 按行识别 SSE，网络分块可能合并或拆开多条事件。
                        async for line in response.aiter_lines():
                            yield (line + "\n").encode("utf-8")
                    finally:
                        await client.aclose()
                event_lines = events()
                if route == "/responses" and not body.get("metadata"):
                    # 直接访问兼容 API 的客户端需要聊天分块。网页聊天由 WebUI 中间件处理。
                    from open_webui.utils.response import convert_responses_stream_to_chat_completions
                    event_lines = convert_responses_stream_to_chat_completions(event_lines)
                streaming = True
                return StreamingResponse(event_lines, media_type="text/event-stream",
                                         background=BackgroundTask(client.aclose))
            await response.aread()
            result = response.json()
            if route == "/responses":
                from open_webui.routers.openai import convert_responses_result
                return convert_responses_result(result)
            return result
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


async def discover_models(user, form):
    key = form.key.get_secret_value() if form.key else ""
    if not key:
        if not form.connection_id:
            raise HTTPException(422, "请先输入此服务商的 API Key")
        rows = await repo.records(user.id, form.connection_id)
        if not rows or rows[0]["endpoint"] != form.endpoint:
            raise HTTPException(404, "未找到此账号下地址匹配的连接")
        key = cipher().decrypt(rows[0]["credential"].encode()).decode()
    return await ProviderAdapter().list_models(form.endpoint, key)


def personal_router(verified_user):
    router = APIRouter()

    @router.get("/personal-models", include_in_schema=False)
    async def page():
        return RedirectResponse("/?settings=connections", status_code=307)

    @router.get("/api/office/personal-models")
    async def list_models(user=Depends(verified_user)):
        return [public_record(row) for row in await repo.records(user.id)]

    @router.post("/api/office/personal-models/discover")
    async def discover(form: DiscoveryInput, user=Depends(verified_user)):
        return {"models": await discover_models(user, form)}

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
