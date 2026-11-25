import io
from xml.sax.saxutils import escape
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from extractor import router
from document_formats import extract, InvalidDocument


def workbook_fixture():
    book = Workbook()
    for sheet, value in [(book.active, 18), (book.create_sheet("上海"), 27)]:
        sheet.append(["订单", "金额"])
        sheet.append(["A", value])
    book.active.title = "北京"
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def word_fixture(text):
    content = f'<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>'
    xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body>{content}<w:tbl><w:tr><w:tc>{content}</w:tc></w:tr></w:tbl></w:body></w:document>')
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>'
        ))
        archive.writestr("_rels/.rels", (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
        ))
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_multisheet_values():
    text = extract(workbook_fixture(), ".xlsx")
    assert "北京\n订单,金额\nA,18" in text
    assert "上海\n订单,金额\nA,27" in text


def test_word_paragraph_and_table():
    assert extract(word_fixture("周四交付"), ".docx").count("周四交付") == 2


def test_size_limit():
    with pytest.raises(InvalidDocument):
        extract(word_fixture("长" * 45_000), ".docx")


def test_extractor_permissions_and_bad_upload(monkeypatch):
    monkeypatch.setenv("EXTERNAL_DOCUMENT_LOADER_API_KEY", "fixture")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        assert client.put("/office-extractor/process").status_code == 403
        response = client.put("/office-extractor/process", content=b"invalid", headers={
            "Authorization": "Bearer fixture", "X-Filename": "note.docx",
        })
        assert response.status_code == 422


@pytest.mark.parametrize("suffix, encoding", [
    (".csv", "utf-8-sig"), (".tsv", "utf-16"), (".txt", "gb18030"),
    (".md", "utf-8"), (".json", "utf-8"), (".yaml", "utf-8"), (".sql", "utf-8"),
])
def test_text_and_chinese_encodings(suffix, encoding):
    assert "杭州" in extract("城市,金额\n杭州,29".encode(encoding), suffix)


def test_html_and_rtf():
    assert "secret" not in extract(b"<p>Visible</p><script>secret</script>", ".html")
    assert "Visible" in extract(b"{\\rtf1\\ansi Visible}", ".rtf")


def test_rejects_disguised_binary_and_unknown_type():
    with pytest.raises(InvalidDocument):
        extract(b"PK\x00\x01", ".txt")
    with pytest.raises(InvalidDocument):
        extract(b"data", ".exe")


def test_presentation_uses_declared_slide_order():
    out = io.BytesIO()
    with ZipFile(out, "w") as archive:
        archive.writestr("ppt/presentation.xml", (
            '<presentation xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sldIdLst><sldId r:id="second"/><sldId r:id="first"/></sldIdLst></presentation>'
        ))
        archive.writestr("ppt/_rels/presentation.xml.rels",
                         '<rels><r Id="first" Target="slides/slide1.xml"/><r Id="second" Target="slides/slide2.xml"/></rels>')
        for num in (1, 2):
            archive.writestr(f"ppt/slides/slide{num}.xml", (
                '<s xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f'<a:p><a:r><a:t>Slide {num}</a:t></a:r></a:p></s>'))
    assert extract(out.getvalue(), ".pptx").splitlines() == ["幻灯片 1", "Slide 2", "幻灯片 2", "Slide 1"]


def test_odf_preserves_nested_text():
    out = io.BytesIO()
    with ZipFile(out, "w") as archive:
        archive.writestr("content.xml", '<root xmlns:t="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
                         '<t:p>前<t:span>中</t:span>后</t:p></root>')
    assert extract(out.getvalue(), ".odt") == "前中后"


def test_scan_only_pdf_explains_ocr():
    from pypdf import PdfWriter
    out = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=150, height=150)
    writer.write(out)
    with pytest.raises(InvalidDocument, match="OCR"):
        extract(out.getvalue(), ".pdf")


def test_ods_numeric_cached_value_and_empty_columns():
    buf = io.BytesIO()
    with ZipFile(buf, "w") as archive:
        archive.writestr("content.xml",
                         '<o:spreadsheet-root xmlns:o="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                         'xmlns:t="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
                         '<o:spreadsheet><t:table t:name="成本"><t:table-row>'
                         '<t:table-cell t:number-columns-repeated="2"/><t:table-cell o:value="35"/>'
                         '</t:table-row></t:table></o:spreadsheet></o:spreadsheet-root>')
    assert extract(buf.getvalue(), ".ods") == "工作表 成本\n,,35"
