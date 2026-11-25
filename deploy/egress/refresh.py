"""Refresh a Mihomo file provider without importing subscription routing or listeners."""
import asyncio
import fcntl
import logging
import os
import pwd
import tempfile
import sys
from pathlib import Path

import httpx
import yaml

LOG = logging.getLogger("office.egress")
STATE = Path("/var/lib/office-egress")
NODES = STATE / "nodes.yaml"
SOURCES = Path("/etc/office-egress/sources")
MAX_BYTES = 2 * 1024 * 1024
COMMON = {"type", "server", "port", "udp", "skip-cert-verify"}
OPTIONS = {
    "trojan": {"password", "sni", "network", "ws-opts", "alpn"},
    "vmess": {"uuid", "alterId", "cipher", "network", "tls", "servername",
              "ws-headers", "ws-path", "ws-opts", "alpn"},
}


def provider_bytes(data):
    if len(data) > MAX_BYTES:
        raise ValueError("size")
    doc = yaml.safe_load(data)
    if not isinstance(doc, dict) or not isinstance(doc.get("proxies"), list):
        raise ValueError("format")
    nodes = []
    for source in doc["proxies"]:
        if not isinstance(source, dict):
            continue
        kind = source.get("type")
        if kind not in OPTIONS or not source.get("server") or not isinstance(source.get("port"), int):
            continue
        if not source.get("password" if kind == "trojan" else "uuid"):
            continue
        allowed = COMMON | OPTIONS[kind]
        node = {k: v for k, v in source.items() if k in allowed}
        node["name"] = f"office-{len(nodes) + 1:03}"
        nodes.append(node)
    if not nodes or len(nodes) > 500:
        raise ValueError("node count")
    return yaml.safe_dump({"proxies": nodes}, sort_keys=False).encode()


async def download(client, url):
    async with client.stream("GET", url, headers={"User-Agent": "Clash.Meta"}) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise ValueError("size")
    return provider_bytes(bytes(data))


def replace_nodes(content, path=None):
    account = pwd.getpwnam("office-egress")
    with tempfile.NamedTemporaryFile(dir=STATE, delete=False) as staging:
        temp = Path(staging.name)
        try:
            staging.write(content)
            staging.flush()
            os.fsync(staging.fileno())
            os.fchown(staging.fileno(), account.pw_uid, account.pw_gid)
            os.replace(temp, path or NODES)
            directory = os.open(STATE, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temp.unlink(missing_ok=True)


async def reload_nodes(client):
    response = await client.put("/providers/proxies/subscription")
    response.raise_for_status()


def recover_pending():
    pending = STATE / ".previous-nodes"
    if pending.exists():
        replace_nodes(pending.read_bytes())
        return True
    return False


async def install_candidate(candidate, old, control):
    if old == candidate:
        return
    pending = STATE / ".previous-nodes"
    replace_nodes(old, pending)
    try:
        replace_nodes(candidate)
        await reload_nodes(control)
        health = await control.get("/providers/proxies/subscription/healthcheck", timeout=35)
        health.raise_for_status()
        check = await control.get("/proxies/Office/delay", params={
            "url": "https://www.gstatic.com/generate_204", "timeout": 9000,
        })
        check.raise_for_status()
        if not check.json().get("delay"):
            raise ValueError("no working egress")
    except Exception:
        replace_nodes(old)
        await reload_nodes(control)
        pending.unlink()
        raise
    pending.unlink()


async def refresh(incoming=None):
    restored = recover_pending()
    old = NODES.read_bytes()
    transport = httpx.AsyncHTTPTransport(uds=str(STATE / "control.sock"))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost", timeout=25) as control:
        if restored:
            await reload_nodes(control)
            (STATE / ".previous-nodes").unlink()
        if incoming is not None:
            await install_candidate(provider_bytes(incoming), old, control)
            return True
        async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client:
            for index, url in enumerate(SOURCES.read_text().splitlines(), 1):
                try:
                    await install_candidate(await download(client, url), old, control)
                    return True
                except (httpx.HTTPError, ValueError, yaml.YAMLError) as error:
                    status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else 0
                    LOG.warning("Source %d failed (%s, status %d)", index, type(error).__name__, status)
    return False


if __name__ == "__main__":
    with (STATE / ".refresh.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if sys.argv[1:] == ["--recover"]:
                if recover_pending():
                    (STATE / ".previous-nodes").unlink()
                raise SystemExit(0)
            stream = sys.stdin.buffer.read(MAX_BYTES + 1) if sys.argv[1:] == ["--stdin"] else None
            raise SystemExit(0 if asyncio.run(refresh(stream)) else 1)
        except Exception as error:
            LOG.error("Refresh failed (%s)", type(error).__name__)
            raise SystemExit(1) from None
