import asyncio
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import personal


@pytest.fixture
def repository(tmp_path, monkeypatch):
    repo = personal.Repository(tmp_path / "test.sqlite3")
    monkeypatch.setattr(personal, "repo", repo)
    monkeypatch.setenv("WEBUI_SECRET_KEY", Fernet.generate_key().decode())
    return repo


def test_account_model_lifecycle_and_isolation(repository):
    owner = SimpleNamespace(id="alice")
    guest = SimpleNamespace(id="bob")
    app = FastAPI()
    app.include_router(personal.personal_router(lambda: owner))
    base = "/api/office/personal-models"
    fields = {"name": "office", "model": "same", "endpoint": "https://api.example.com/v1", "key": "fixture-secret"}
    with TestClient(app) as client:
        created = client.post(base, json=fields)
        assert created.status_code == 200
        model_id = created.json()["id"]
        path = base + "/" + model_id
        assert "fixture-secret" not in client.get(base).text
        fields.pop("key")
        assert client.put(path, json=fields).status_code == 200
        assert client.put(path, json={**fields, "endpoint": "https://other.example/v1"}).status_code == 404
        owner.id = guest.id
        assert client.get(base).json() == []
        assert client.put(path, json=fields).status_code == 404
        assert client.delete(path).status_code == 404
        assert asyncio.run(personal.personal_model(guest, model_id)) is None
        with pytest.raises(HTTPException) as exc:
            asyncio.run(personal.complete(guest, {"model": model_id}))
        assert exc.value.status_code == 404
        owner.id = "alice"
        row, = asyncio.run(repository.records(owner.id))
        assert row["credential"] != "fixture-secret"
        assert personal.cipher().decrypt(row["credential"].encode()) == b"fixture-secret"
        assert client.delete(path).status_code == 200
        assert client.get(base).json() == []


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "224.0.0.1"])
def test_public_target_rejects_internal_answers(ip):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"Answer": [{"type": 1, "data": ip}]})
        )) as client:
            with pytest.raises(HTTPException):
                await personal.public_target(client, "https://my.example/v1")
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
def test_adapter(repository, monkeypatch, stream):
    client_class = httpx.AsyncClient
    def upstream(request):
        if request.url.host == "dns.google":
            return httpx.Response(200, json={"Answer": [{"type": 1, "data": "1.1.1.1"}]})
        assert request.url.host == "1.1.1.1" and request.headers['host'] == "models.example"
        assert request.extensions["sni_hostname"] == "models.example"
        assert request.headers["authorization"] == "Bearer test-key"
        import json
        data = json.loads(request.content)
        assert data['model'] == "example" and 'metadata' not in data
        if stream:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"data: [DONE]\n\n")
        return httpx.Response(200, json={"choices": []})
    monkeypatch.setattr(personal.httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(upstream), **kw))
    async def run():
        settings = personal.Settings(name="mine", model="example", endpoint="https://models.example/v1", key="test-key")
        model_id = await repository.save("alice", None, settings)
        answer = await personal.complete(SimpleNamespace(id="alice"), {
            "model": model_id, "messages": [], "metadata": {"private": True}, "stream": stream})
        if stream:
            assert b"".join([part async for part in answer.body_iterator]) == b"data: [DONE]\n\n"
        else:
            assert answer == {"choices": []}
    asyncio.run(run())
