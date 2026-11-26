import asyncio
import logging
import os
import secrets
import sys
from pathlib import Path, PurePath
from urllib.parse import unquote
from fastapi import APIRouter, HTTPException, Request

from document_formats import InvalidDocument, MAX_BYTES

router = APIRouter()
log = logging.getLogger("office.extractor")
parse_slot = asyncio.Semaphore(1)


async def run_worker(data, suffix):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).with_name("document_formats.py")), suffix,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TMPDIR", "OPENBLAS_NUM_THREADS"}},
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(data), timeout=18)
    except TimeoutError as error:
        raise InvalidDocument("解析超时，请拆分文件或另存为文本。") from error
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode == 2:
        raise InvalidDocument(output.decode("utf-8"))
    if proc.returncode:
        raise InvalidDocument("文件损坏、已加密或超过解析资源上限。")
    return output.decode("utf-8")


@router.put("/office-extractor/process", include_in_schema=False)
async def process_file(request: Request):
    key = os.environ.get("EXTERNAL_DOCUMENT_LOADER_API_KEY", "")
    if not key or not secrets.compare_digest(
        request.headers.get("authorization", "").encode(), f"Bearer {key}".encode()
    ):
        raise HTTPException(403)
    suffix = PurePath(unquote(request.headers.get("x-filename", ""))).suffix.lower()
    if parse_slot.locked():
        raise HTTPException(503, "解析器繁忙，请稍后重试。", headers={"Retry-After": "2"})
    try:
        async with parse_slot:
            data = bytearray()
            async for chunk in request.stream():
                if len(chunk) + len(data) > MAX_BYTES:
                    raise HTTPException(413, "单文件上限 8 MiB。")
                data.extend(chunk)
            text = await run_worker(data, suffix)
    except HTTPException:
        raise
    except InvalidDocument as error:
        log.warning("Document rejected by format or size limit")
        raise HTTPException(422, str(error)) from error
    except (OSError, ValueError, KeyError, UnicodeError) as error:
        log.warning("Invalid Office document (%s)", type(error).__name__)
        raise HTTPException(422, "无法读取文件，请检查文件格式和加密状态。") from error
    except Exception as error:
        log.warning("Document parse failure (%s)", type(error).__name__)
        raise HTTPException(422, "文件损坏或格式不受支持。") from error
    return {"page_content": text, "metadata": {}}
