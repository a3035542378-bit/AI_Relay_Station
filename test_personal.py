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


def test_old_bookmark_opens_native_connection_settings():
    app = FastAPI()
    app.include_router(personal.personal_router(lambda: None))
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/personal-models")
        assert response.status_code == 307
        assert response.headers["location"] == "/?settings=connections"


def test_stream_emits_complete_lines_across_transport_chunks(repository, monkeypatch):
    class SplitEvents(httpx.AsyncByteStream):
        async def __aiter__(self):
            for block in (b"data: one\r", b"\n\r\ndata: ", b"two\n\ndata: [DONE]\n\n"):
                yield block

    def upstream(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SplitEvents())

    client_class = httpx.AsyncClient
    monkeypatch.setattr(personal.httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(upstream), **kw))

    async def run():
        row = {"endpoint": "https://1.1.1.1/v1", "model": "example",
               "credential": personal.cipher().encrypt(b"example").decode()}
        result = await personal.ProviderAdapter().complete(row, {"messages": [], "stream": True})
        assert [line async for line in result.body_iterator] == [
            b"data: one\n", b"\n", b"data: two\n", b"\n", b"data: [DONE]\n", b"\n",
        ]
    asyncio.run(run())


def test_discovery_saved_key_bound_to_owner_and_url(repository, monkeypatch):
    owner = SimpleNamespace(id="alice")
    requests = []

    async def listing(self, endpoint, key):
        requests.append((endpoint, key))
        return ["one", "two"]

    monkeypatch.setattr(personal.ProviderAdapter, "list_models", listing)
    app = FastAPI()
    app.include_router(personal.personal_router(lambda: owner))
    base = "/api/office/personal-models"
    url = "https://example.com/v1"
    with TestClient(app) as client:
        connection = client.post(base, json=dict(name="one", model="one", endpoint=url, key="saved")).json()["id"]
        form = {"endpoint": url, "connection_id": connection}
        assert client.post(base + "/discover", json=form).json() == {"models": ["one", "two"]}
        assert client.post(base + "/discover", json={**form, "endpoint": "https://other.example/v1"}).status_code == 404
        owner.id = "bob"
        assert client.post(base + "/discover", json=form).status_code == 404
        assert client.post(base + "/discover", json={"endpoint": url}).status_code == 422
        assert client.post(base + "/discover", json={"endpoint": url, "key": "draft"}).status_code == 200
        assert client.get(base).json() == []
        assert requests == [(url, "saved"), (url, "draft")]


@pytest.mark.parametrize(("host", "header", "expected"), [
    ("api.xiaomimimo.com", "api-key", "fixture"),
    ("api.anthropic.com", "x-api-key", "fixture"),
    ("models.example", "authorization", "Bearer fixture"),
])
def test_discovery_auth_and_normalization(monkeypatch, host, header, expected):
    client_class = httpx.AsyncClient
    def upstream(request):
        if request.url.host == "dns.google":
            return httpx.Response(200, json={"Answer": [{"type": 1, "data": "1.1.1.1"}]})
        assert request.url.path == "/v1/models" and request.method == "GET"
        assert request.url.host == "1.1.1.1" and request.headers["host"] == host
        assert request.extensions["sni_hostname"] == host
        assert request.headers[header] == expected
        return httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}, {"id": None}, {}]})
    monkeypatch.setattr(personal.httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(upstream), **kw))
    assert asyncio.run(personal.ProviderAdapter().list_models("https://" + host + "/v1", "fixture")) == ["a", "b"]


@pytest.mark.parametrize("status,body", [(200, b"not JSON"), (200, b"{}"), (401, b"private upstream message"),
                                         (302, b"redirect"), (200, b"x" * 4194305)])
def test_discovery_unavailable(monkeypatch, status, body):
    client_class = httpx.AsyncClient
    def upstream(req):
        assert req.url.host == "1.1.1.1"
        return httpx.Response(status, content=body, headers={"location": "https://other.example/"})
    monkeypatch.setattr(personal.httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(upstream), **kw))
    with pytest.raises(HTTPException) as error:
        asyncio.run(personal.ProviderAdapter().list_models("https://1.1.1.1/v1", "fixture"))
    assert error.value.status_code == 502 and "private upstream message" not in error.value.detail
