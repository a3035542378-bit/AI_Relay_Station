"""Bounded text extraction in a separate worker process."""
import codecs
import csv
import io
import posixpath
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

from defusedxml.ElementTree import iterparse, fromstring

MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT = 80_000


class InvalidDocument(ValueError):
    pass


def read_excel(raw):
    from openpyxl import load_workbook
    workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
    try:
        inspected = 0
        for sheet in workbook.worksheets:
            yield f"工作表 {sheet.title}\n"
            sheet.reset_dimensions()
            for row in sheet.rows:
                inspected += len(row)
                if inspected > 150_000:
                    raise InvalidDocument("工作表过大，请拆分后上传。")
                values = ["" if cell.value is None else str(cell.value) for cell in row]
                if any(values):
                    buf = io.StringIO()
                    csv.writer(buf, lineterminator="\n").writerow(values)
                    yield buf.getvalue()
    finally:
        workbook.close()


def read_word(archive):
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with archive.open("word/document.xml") as xml:
        for _, element in iterparse(xml, events=("end",)):
            if element.tag == namespace + "t":
                yield element.text or ""
            elif element.tag in {namespace + "p", namespace + "br", namespace + "tr"}:
                yield "\n"
            elif element.tag in {namespace + "tab", namespace + "tc"}:
                yield "\t"
            element.clear()



TEXT_SUFFIXES = set("""
.txt .md .markdown .rst .csv .tsv .json .jsonl .ndjson .xml .yaml .yml .toml .ini .conf .cfg
.log .sql .py .js .jsx .ts .tsx .css .java .c .h .cpp .hpp .cs .go .rs .rb .php .sh .kt
.swift .vue .properties .tex .srt .vtt .diff .patch
""".split())
WORD = {".docx", ".docm", ".dotx", ".dotm"}
EXCEL = {".xlsx", ".xlsm", ".xltx", ".xltm"}
SLIDES = {".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"}
ODF = {".odt", ".ods", ".odp", ".ott", ".ots", ".otp"}


def decode_text(data):
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encodings = ["utf-32"]
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encodings = ["utf-16"]
    else:
        encodings = ["utf-8-sig", "gb18030", "cp1252"]
    for encoding in encodings:
        try:
            value = data.decode(encoding)
        except UnicodeError:
            continue
        if not re.search(r"[\x00-\x08\x0b\x0e-\x1f]", value):
            return value
    raise InvalidDocument("无法识别文本编码，请另存为 UTF-8 后上传。")


def read_html(raw):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(decode_text(raw), "html.parser")
    for element in soup(["style", "script", "template", "noscript"]):
        element.decompose()
    yield soup.get_text("\n", strip=True)


def read_slides(archive):
    presentation = fromstring(archive.read("ppt/presentation.xml"))
    rels = fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
    targets = {r.get("Id"): r.get("Target") for r in rels if r.get("TargetMode") != "External"}
    relationship = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    for number, slide in enumerate(presentation.findall(".//{*}sldIdLst/{*}sldId"), 1):
        target = targets[slide.attrib[relationship]]
        path = posixpath.normpath(posixpath.join("ppt", target)).lstrip("/")
        yield f"幻灯片 {number}\n"
        root = fromstring(archive.read(path))
        for paragraph in root.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/main}p"):
            yield "".join(node.text or "" for node in paragraph.findall(".//{*}t")) + "\n"


def read_odf(archive):
    root = fromstring(archive.read("content.xml"))
    namespace = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
    office = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
    spreadsheet = root.find(".//" + office + "spreadsheet")
    if spreadsheet is not None:
        for table in spreadsheet.findall(namespace + "table"):
            yield "工作表 " + table.get(namespace + "name", "") + "\n"
            for row in table.iter(namespace + "table-row"):
                values = []
                gaps = 0
                for cell in row:
                    if cell.tag not in {namespace + "table-cell", namespace + "covered-table-cell"}:
                        continue
                    count = max(1, int(cell.get(namespace + "number-columns-repeated", 1)))
                    value = "\n".join("".join(p.itertext()) for p in cell.findall("{*}p"))
                    if not value:
                        value = next((cell.attrib[office + k] for k in
                                      ("value", "date-value", "time-value", "boolean-value", "string-value")
                                      if office + k in cell.attrib), "")
                    if not value:
                        gaps += count
                        continue
                    if len(values) + gaps + count > 16384:
                        raise InvalidDocument("工作表列数过多，请拆分后上传。")
                    values.extend([""] * gaps)
                    values.extend([value] * count)
                    gaps = 0
                if values:
                    buf = io.StringIO()
                    csv.writer(buf, lineterminator="\n").writerow(values)
                    line = buf.getvalue()
                    repeats = max(1, int(row.get(namespace + "number-rows-repeated", 1)))
                    if repeats * len(line) > MAX_TEXT:
                        raise InvalidDocument("工作表内容过多，请拆分后上传。")
                    yield line * repeats
        return
    for element in root.iter():
        if element.tag == namespace + "table":
            yield "表格 " + element.get(namespace + "name", "") + "\n"
        elif element.tag in {"{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p",
                              "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}h"}:
            yield "".join(element.itertext()) + "\n"


def read_pdf(raw):
    from pypdf import PdfReader
    pdf = PdfReader(io.BytesIO(raw))
    if pdf.is_encrypted and not pdf.decrypt(""):
        raise InvalidDocument("PDF 已加密，请上传已解密版本。")
    found = False
    for index, page in enumerate(pdf.pages, 1):
        value = (page.extract_text() or "").strip()
        if value:
            found = True
            yield f"第 {index} 页\n{value}\n"
    if not found:
        raise InvalidDocument("PDF 中没有可提取的文本。扫描件请先做 OCR，或将页面作为图片上传。")


def read_xls(raw):
    import xlrd
    book = xlrd.open_workbook(file_contents=raw, on_demand=True, ragged_rows=True)
    inspected = 0
    try:
        for i, name in enumerate(book.sheet_names()):
            sheet = book.sheet_by_index(i)
            yield f"工作表 {name}\n"
            for n in range(sheet.nrows):
                inspected += sheet.row_len(n)
                if inspected > 150_000:
                    raise InvalidDocument("工作表过大，请拆分后上传。")
                values = []
                for cell in sheet.row(n):
                    value = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        value = xlrd.xldate_as_datetime(value, book.datemode).isoformat(sep=" ")
                    values.append(value)
                buf = io.StringIO()
                csv.writer(buf, lineterminator="\n").writerow(values)
                yield buf.getvalue()
            book.unload_sheet(i)
    finally:
        book.release_resources()


def read_ppt_textboxes(raw):
    import olefile
    with olefile.OleFileIO(io.BytesIO(raw)) as presentation:
        data = presentation.openstream("PowerPoint Document").read()

    def records(start, end):
        while start + 8 <= end:
            flags, kind, size = struct.unpack_from("<HHI", data, start)
            begin, finish = start + 8, start + 8 + size
            if finish > end:
                raise InvalidDocument("PPT 记录不完整，请另存为 .pptx。")
            yield flags, kind, begin, finish
            start = finish

    def textbox(start, end, depth=0):
        if depth > 40:
            raise InvalidDocument("PPT 形状嵌套过深，请另存为 .pptx。")
        for flags, kind, begin, finish in records(start, end):
            if kind in {4000, 4008}:
                encoding = "utf-16le" if kind == 4000 else "latin1"
                yield data[begin:finish].decode(encoding, "replace") + "\n"
            elif flags & 15 == 15 or kind == 0xF00D:
                yield from textbox(begin, finish, depth + 1)

    # 仅在单次完整保存的文件中读取幻灯片容器，避免导入旧修订或母版。
    if sum(kind == 4085 for _, kind, _, _ in records(0, len(data))) != 1:
        raise InvalidDocument("此 PPT 含增量修订，请另存为 .pptx。")
    for _, kind, begin, finish in records(0, len(data)):
        if kind == 1006:
            yield from textbox(begin, finish)


def read_legacy(raw, suffix):
    if not raw.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        raise InvalidDocument("未识别为旧版 Office 文件，请使用另存为功能转换为新版格式。")
    command = ["catdoc", "-d", "utf-8"] if suffix == ".doc" else ["catppt", "-d", "utf-8"]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / ("input" + suffix)
        path.write_bytes(raw)
        with tempfile.TemporaryFile() as output:
            done = subprocess.run([*command, str(path)], stdout=output, stderr=subprocess.DEVNULL,
                                  stdin=subprocess.DEVNULL, timeout=9)
            if done.returncode:
                raise InvalidDocument("旧版 Office 文件无法读取，请另存为 .docx 或 .pptx。")
            output.seek(0)
            data = output.read(MAX_TEXT * 4 + 1)
            if len(data) > MAX_TEXT * 4:
                raise InvalidDocument("文件文字超过 8 万字符，请拆分后上传。")
            if suffix == ".ppt" and not data.strip():
                yield from read_ppt_textboxes(raw)
            else:
                yield data.decode("utf-8", errors="replace")


def parts(raw, suffix):
    if suffix in TEXT_SUFFIXES:
        yield decode_text(raw)
    elif suffix in {".html", ".htm", ".xhtml"} or (
        suffix in {".doc", ".xls"} and re.match(br"\s*<(?:!doctype\s+html|html|table)\b", raw[:256], re.I)
    ):
        yield from read_html(raw)
    elif suffix == ".rtf" or (suffix == ".doc" and raw.startswith(b"{\\rtf")):
        from striprtf.striprtf import rtf_to_text
        yield rtf_to_text(raw.decode("latin1"))
    elif suffix == ".pdf":
        yield from read_pdf(raw)
    elif suffix == ".xls":
        yield from read_xls(raw)
    elif suffix in {".doc", ".ppt"}:
        yield from read_legacy(raw, suffix)
    elif suffix in WORD | EXCEL | SLIDES | ODF:
        with ZipFile(io.BytesIO(raw)) as archive:
            files = archive.infolist()
            if len(files) > 3000 or sum(f.file_size for f in files) > 32 * 1024 * 1024:
                raise InvalidDocument("文件解压后过大，请拆分后上传。")
            if suffix in WORD:
                yield from read_word(archive)
            elif suffix in EXCEL:
                yield from read_excel(raw)
            elif suffix in SLIDES:
                yield from read_slides(archive)
            else:
                yield from read_odf(archive)
    else:
        raise InvalidDocument("暂不支持这种文件。可转换为 PDF、Word、Excel、PowerPoint、CSV 或 TXT 后上传。")


def extract(raw, suffix):
    if len(raw) > MAX_BYTES:
        raise InvalidDocument("单文件上限 8 MiB。")
    length = 0
    with io.StringIO() as output:
        for part in parts(bytes(raw), suffix.lower()):
            length += len(part)
            if length > MAX_TEXT:
                raise InvalidDocument("文件文字超过 8 万字符，请拆分后上传。")
            output.write(part)
        text = output.getvalue().strip()
    if not text:
        raise InvalidDocument("文件中未找到可读取的正文。")
    return text


if __name__ == "__main__":
    if sys.platform.startswith("linux"):
        import resource
        for kind, limit in [(resource.RLIMIT_CPU, 12), (resource.RLIMIT_AS, 384 * 1024 ** 2),
                            (resource.RLIMIT_FSIZE, 10 * 1024 ** 2), (resource.RLIMIT_CORE, 0)]:
            resource.setrlimit(kind, (limit, limit))
    try:
        sys.stdout.buffer.write(extract(sys.stdin.buffer.read(MAX_BYTES + 1), sys.argv[1]).encode())
    except InvalidDocument as error:
        sys.stdout.buffer.write(str(error).encode())
        sys.exit(2)
