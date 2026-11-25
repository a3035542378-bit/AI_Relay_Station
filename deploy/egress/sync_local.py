"""Run on the subscription-authorized workstation. SSH carries the configuration."""
import asyncio
import logging
import os
from pathlib import Path

import httpx

TARGET_FILE = Path(__file__).resolve().parents[2] / ".private" / "ssh-target"
MAX_BYTES = 2 * 1024 * 1024


async def sync():
    target = os.environ.get("OFFICE_SSH_TARGET") or (
        TARGET_FILE.read_text().strip() if TARGET_FILE.is_file() else ""
    )
    if not target or target.startswith("-") or any(c.isspace() for c in target):
        raise SystemExit("请设置 OFFICE_SSH_TARGET，如 deploy@relay.example.com")
    reader = await asyncio.create_subprocess_exec(
        "ssh", "-oBatchMode=yes", target, "cat /etc/office-egress/sources",
        stdout=asyncio.subprocess.PIPE,
    )
    sources, _ = await reader.communicate()
    if reader.returncode:
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        for index, url in enumerate(sources.decode().splitlines(), 1):
            try:
                async with client.stream("GET", url, headers={"User-Agent": "Clash.Meta"}) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise ValueError("size")
                installer = await asyncio.create_subprocess_exec(
                    "ssh", "-oBatchMode=yes", target,
                    "/opt/office-webui/venv/bin/python /opt/office-egress/refresh.py --stdin",
                    stdin=asyncio.subprocess.PIPE,
                )
                await installer.communicate(data)
                if installer.returncode == 0:
                    print(f"订阅源 {index} 已同步。")
                    return True
            except (httpx.HTTPError, ValueError) as error:
                logging.warning("Source %d failed (%s)", index, type(error).__name__)
    return False


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(sync()) else 1)
