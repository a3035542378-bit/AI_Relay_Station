"""Excel workbook attachment tool for Open WebUI."""
import asyncio
import logging
import math
import re
from io import BytesIO

from fastapi import HTTPException, Request, UploadFile
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from starlette.datastructures import Headers

log = logging.getLogger("office.workbook")
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def build_workbook(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError("请提供 1 至 5000 行数据。")
    book = Workbook()
    sheet = book.active
    sheet.title = "Sheet1"
    cells = chars = 0
    for row_idx, row in enumerate(rows, 1):
        if not isinstance(row, list) or len(row) > 256:
            raise ValueError("每行最多支持 256 列。")
        cells += len(row)
        if cells > 50000:
            raise ValueError("单个工作簿最多支持 50000 个单元格。")
        for col_idx, value in enumerate(row, 1):
            if value is None:
                continue
            if isinstance(value, str):
                chars += len(value)
                if len(value) > 32767 or chars > 2_000_000 or ILLEGAL_CHARACTERS_RE.search(value):
                    raise ValueError("单元格文本过长或含无效字符。")
            elif not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("单元格仅支持文本、有限数值、布尔值或空值。")
            cell = sheet.cell(row_idx, col_idx, value)
            if isinstance(value, str):
                cell.data_type = "s"
    result = BytesIO()
    book.save(result)
    result.seek(0)
    return result


async def create_excel_file(filename: str, rows: list[list[str | int | float | bool | None]],
                            __request__: Request = None, __user__: dict = None, __event_emitter__=None) -> dict:
    """Create and attach a real downloadable .xlsx workbook. Call this when the user asks for an Excel file, including a test or processed table. Return the supplied download link in your answer.

    :param filename: Requested workbook filename, for example 测试.xlsx.
    :param rows: Table rows in order, including any header row. Provide actual cell values.
    """
    from open_webui.models.users import UserModel
    from open_webui.routers.files import upload_file_handler

    user = UserModel(**__user__)
    name = filename.strip()
    if not name or len(name) > 120 or re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
        return {"error": "请使用不含路径的有效文件名，最多 120 个字符。"}
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    try:
        stream = await asyncio.to_thread(build_workbook, rows)
    except (ValueError, OverflowError, UnicodeError) as exc:
        log.info("Workbook data validation failed (%s)", type(exc).__name__)
        return {"error": "无法生成工作簿，请检查行列数、单元格长度和内容。"}
    upload = UploadFile(stream, filename=name, headers=Headers({"content-type": MIME}))
    try:
        file = await upload_file_handler(__request__, upload, metadata={}, process=False,
                                         process_in_background=False, user=user)
    except HTTPException as exc:
        log.warning("Workbook storage returned HTTP %s", exc.status_code)
        return {"error": "工作簿保存失败，请稍后重试。"}
    finally:
        await upload.close()
    url = f"/api/v1/files/{file.id}/content?attachment=true"
    attachment = {"type": "file", "id": file.id, "name": name, "url": url, "content_type": MIME}
    if __event_emitter__:
        await __event_emitter__({"type": "files", "data": {"files": [attachment.copy()]}})
    return {"filename": name, "download_url": url}
