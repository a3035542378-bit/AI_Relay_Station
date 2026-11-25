import asyncio

import httpx
import pytest
import yaml

from deploy.egress import refresh as egress


def sample(host="example.test"):
    return yaml.safe_dump({"proxies": [
        {"type": "direct", "name": "local"},
        {"type": "trojan", "name": "anything", "server": host, "port": 443,
         "password": "test", "dialer-proxy": "DIRECT"},
    ], "rules": ["MATCH,DIRECT"], "mixed-port": 9999}).encode()


def test_subscription_only_imports_transport():
    doc = yaml.safe_load(egress.provider_bytes(sample()))
    assert set(doc) == {"proxies"}
    node, = doc["proxies"]
    assert node["name"] == "office-001"
    assert node["type"] == "trojan"
    assert "dialer-proxy" not in node


@pytest.mark.parametrize("data", [b"error", b"proxies: []", b"x" * (egress.MAX_BYTES + 1)])
def test_reject_invalid_subscription(data):
    with pytest.raises(ValueError):
        egress.provider_bytes(data)


@pytest.mark.parametrize("primary_ok,backup_ok", [(True, False), (False, True), (False, False)])
def test_sources_and_retention(tmp_path, monkeypatch, primary_ok, backup_ok):
    sources = tmp_path / "sources"
    sources.write_text("https://primary.invalid/\nhttps://backup.invalid/\n")
    nodes = tmp_path / "nodes.yaml"
    nodes.write_bytes(b"previous")
    monkeypatch.setattr(egress, "NODES", nodes)
    monkeypatch.setattr(egress, "SOURCES", sources)
    monkeypatch.setattr(egress, "STATE", tmp_path)
    monkeypatch.setattr(egress, "replace_nodes", lambda data, path=None: (path or nodes).write_bytes(data))
    calls = []

    def handler(request):
        if request.url.host == "localhost":
            return httpx.Response(200, json={"delay": 42})
        calls.append(request.url.host)
        ok = primary_ok if request.url.host == "primary.invalid" else backup_ok
        return httpx.Response(200, content=sample()) if ok else httpx.Response(403)

    client_class = httpx.AsyncClient
    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return client_class(**kwargs)
    monkeypatch.setattr(egress.httpx, "AsyncClient", mock_client)
    assert asyncio.run(egress.refresh()) == (primary_ok or backup_ok)
    assert calls == (["primary.invalid"] if primary_ok else ["primary.invalid", "backup.invalid"])
    assert nodes.read_bytes() == (egress.provider_bytes(sample()) if primary_ok or backup_ok else b"previous")


def test_health_failure_restores_previous_nodes(tmp_path, monkeypatch):
    nodes = tmp_path / "nodes.yaml"
    old = b"previous"
    monkeypatch.setattr(egress, "STATE", tmp_path)
    monkeypatch.setattr(egress, "replace_nodes", lambda data, path=None: (path or nodes).write_bytes(data))
    loads = []

    def handler(request):
        if request.method == "PUT":
            loads.append(nodes.read_bytes())
            return httpx.Response(204)
        return httpx.Response(503)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost") as client:
            with pytest.raises(httpx.HTTPStatusError):
                await egress.install_candidate(b"candidate", old, client)
    asyncio.run(run())
    assert loads == [b"candidate", old]
    assert nodes.read_bytes() == old


def test_interrupted_update_recovers_verified_file(tmp_path, monkeypatch):
    monkeypatch.setattr(egress, "STATE", tmp_path)
    active = tmp_path / "nodes.yaml"
    active.write_bytes(b"unverified")
    (tmp_path / ".previous-nodes").write_bytes(b"last verified")
    monkeypatch.setattr(egress, "replace_nodes", active.write_bytes)
    assert egress.recover_pending()
    assert active.read_bytes() == b"last verified"
