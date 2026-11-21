import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest

from doctrail.ingest import embedded_media, native_extractor, ocr
from doctrail.ingest.core import process_ingest
from doctrail.ingest.document_processor import (
    SUPPORTED_EXTENSION_FAMILIES,
    format_supported_extensions_for_help,
    process_document,
)
from doctrail.ingest.text_processing import clean_extracted_text

ASSET_DIR = Path(__file__).parent / "assets" / "files"


def _sha1_for(path):
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


def _missing_tools(*tools):
    return [tool for tool in tools if shutil.which(tool) is None]


@pytest.mark.parametrize(
    ("family", "filename", "minimum_chars", "expected_phrase", "required_tools"),
    [
        ("text", "federalist_fixture.txt", 150, "Federalist fixture", ()),
        ("text", "federalist_fixture.md", 150, "Federalist fixture", ()),
        ("delimited", "federalist_fixture.csv", 80, "Federalist No. 1", ()),
        ("delimited", "federalist_fixture.tsv", 80, "Federalist No. 1", ()),
        ("pdf", "federalist_fixture.pdf", 150, "Federalist fixture", ()),
        ("epub", "federalist_fixture.epub", 150, "Federalist fixture", ()),
        ("mobi", "federalist_fixture.mobi", 150, "Federalist fixture", ("ebook-convert",)),
        ("word", "federalist_fixture.doc", 150, "Federalist fixture", ()),
        ("rtf", "federalist_fixture.rtf", 150, "Federalist fixture", ("textutil",)),
        ("word_openxml", "federalist_fixture.docx", 150, "Federalist fixture", ()),
        ("excel_openxml", "federalist_fixture.xlsx", 80, "Federalist No. 1", ()),
        ("excel", "federalist_fixture.xls", 80, "Federalist No. 1", ()),
        ("powerpoint_openxml", "federalist_fixture.pptx", 80, "Federalist fixture", ()),
        ("powerpoint", "federalist_fixture.ppt", 80, "Federalist fixture", ("strings",)),
        ("djvu", "federalist_fixture.djvu", 80, "Federalist fixture", ("djvutxt",)),
        ("mhtml", "federalist_fixture.mhtml", 150, "Federalist fixture", ()),
        ("html", "federalist_fixture.html", 150, "Federalist fixture", ()),
        ("image_ocr", "federalist_fixture.png", 40, "Federalist fixture", ("tesseract",)),
    ],
)
@pytest.mark.asyncio
async def test_process_document_handles_committed_real_file_fixtures(
    family,
    filename,
    minimum_chars,
    expected_phrase,
    required_tools,
):
    missing = _missing_tools(*required_tools)
    if missing:
        pytest.skip(f"missing required extraction tool(s): {', '.join(missing)}")

    path = ASSET_DIR / filename
    assert path.exists(), f"missing committed fixture for {family}: {filename}"
    assert path.suffix.lower() in SUPPORTED_EXTENSION_FAMILIES[family]

    sha1 = _sha1_for(path)
    result_sha1, content, metadata = await process_document(str(path), sha1)

    assert result_sha1 == sha1
    assert isinstance(content, str)
    assert len(content) >= minimum_chars
    assert expected_phrase in content
    assert metadata["original_file_path"] == str(path)
    assert metadata["original_file_type"] == path.suffix.lower().lstrip(".")
    assert metadata["resourceName"] == filename
    if "title" in metadata:
        assert isinstance(metadata["title"], str)


def test_supported_extension_help_uses_dispatch_source_of_truth():
    help_text = format_supported_extensions_for_help()

    for extensions in SUPPORTED_EXTENSION_FAMILIES.values():
        grouped = "/".join(extension.lstrip(".") for extension in extensions)
        assert grouped in help_text


@pytest.mark.asyncio
async def test_ocr_plugin_supports_async_and_sync_functions(monkeypatch, tmp_path):
    async_plugin = tmp_path / "async_ocr.py"
    async_plugin.write_text(
        "async def ocr_async(file_path):\n"
        "    return f'async:{file_path}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCTRAIL_OCR_PLUGIN", str(async_plugin))
    monkeypatch.delenv("DOCTRAIL_MAC_OCR_CLIENT_PATH", raising=False)
    assert await ocr.ocr_with_plugin("sample.pdf") == "async:sample.pdf"

    sync_plugin = tmp_path / "sync_ocr.py"
    sync_plugin.write_text(
        "def ocr(file_path):\n"
        "    return f'sync:{file_path}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCTRAIL_OCR_PLUGIN", str(sync_plugin))
    assert await ocr.ocr_with_plugin("sample.pdf") == "sync:sample.pdf"


def test_ocr_plugin_accepts_legacy_env_var_and_requires_one(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCTRAIL_OCR_PLUGIN", raising=False)
    monkeypatch.delenv("DOCTRAIL_MAC_OCR_CLIENT_PATH", raising=False)
    with pytest.raises(RuntimeError, match="DOCTRAIL_OCR_PLUGIN"):
        ocr.load_ocr_plugin()

    legacy = tmp_path / "legacy_client.py"
    legacy.write_text(
        "async def ocr_async(file_path, node=None):\n"
        "    return 'legacy ocr text'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCTRAIL_MAC_OCR_CLIENT_PATH", str(legacy))
    module = ocr.load_ocr_plugin()
    assert hasattr(module, "ocr_async")


@pytest.mark.asyncio
async def test_process_document_handles_xlsx_real(tmp_path):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Summary"
    sheet.append(["Name", "Count"])
    sheet.append(["Alice", 3])
    sheet.append(["Bob", 4])

    xlsx_path = tmp_path / "sample.xlsx"
    workbook.save(xlsx_path)

    sha1 = _sha1_for(xlsx_path)
    result_sha1, content, metadata = await process_document(str(xlsx_path), sha1)

    assert result_sha1 == sha1
    assert "Summary" in content
    assert "Alice" in content
    assert metadata["original_file_path"] == str(xlsx_path)
    assert metadata["original_file_type"] == "xlsx"
    assert metadata["resourceName"] == "sample.xlsx"


@pytest.mark.asyncio
async def test_process_document_handles_docx_real(tmp_path):
    from docx import Document

    document = Document()
    document.core_properties.title = "Federalist fixture"
    document.add_heading("Federalist fixture", level=1)
    document.add_paragraph("This document has enough real text to catch tuple contract regressions.")

    docx_path = tmp_path / "sample.docx"
    document.save(docx_path)

    sha1 = _sha1_for(docx_path)
    result_sha1, content, metadata = await process_document(str(docx_path), sha1)

    assert result_sha1 == sha1
    assert "Federalist fixture" in content
    assert "tuple contract regressions" in content
    assert metadata["original_file_path"] == str(docx_path)
    assert metadata["original_file_type"] == "docx"
    assert metadata["resourceName"] == "sample.docx"


@pytest.mark.asyncio
async def test_process_document_handles_pptx_real(tmp_path):
    from pptx import Presentation

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Quarterly update"
    slide.placeholders[1].text = "Revenue up\nNew hires"

    pptx_path = tmp_path / "sample.pptx"
    presentation.save(pptx_path)

    sha1 = _sha1_for(pptx_path)
    result_sha1, content, metadata = await process_document(str(pptx_path), sha1)

    assert result_sha1 == sha1
    assert "Quarterly update" in content
    assert "Revenue up" in content
    assert metadata["original_file_type"] == "pptx"


@pytest.mark.asyncio
async def test_process_document_handles_csv_real(tmp_path):
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("Name,Count\nAlice,5\nBob,7\n", encoding="utf-8")
    sha1 = _sha1_for(csv_path)

    result_sha1, content, metadata = await process_document(str(csv_path), sha1)

    assert result_sha1 == sha1
    assert "Alice" in content
    assert metadata["original_file_type"] == "csv"


@pytest.mark.asyncio
async def test_process_document_handles_html_and_mhtml(tmp_path):
    html_path = tmp_path / "sample.html"
    html_path.write_text(
        "<!DOCTYPE html><html><head><title>  Example   page  </title></head>"
        "<body><article><h1>Visible heading</h1><p>Hello world from a saved page.</p></article></body></html>",
        encoding="utf-8",
    )
    _, content, metadata = await process_document(str(html_path), _sha1_for(html_path))
    assert "Hello world" in content
    assert isinstance(metadata.get("title"), str)

    mhtml_as_html = tmp_path / "saved-page.html"
    shutil.copyfile(ASSET_DIR / "federalist_fixture.mhtml", mhtml_as_html)
    _, mcontent, _ = await process_document(str(mhtml_as_html), _sha1_for(mhtml_as_html))
    assert "Federalist fixture" in mcontent


def test_clean_extracted_text_rejects_non_string_input():
    with pytest.raises(TypeError):
        clean_extracted_text(("content", "title"))


def test_embedded_image_rows_preserve_each_parent_relationship(monkeypatch, tmp_path):
    first = tmp_path / "first.docx"
    second = tmp_path / "second.docx"
    first.write_bytes(b"first parent")
    second.write_bytes(b"second parent")
    shared_image = b"same screenshot bytes"

    monkeypatch.setattr(
        embedded_media,
        "office_images",
        lambda path, soffice="soffice": [("word/media/image1.png", shared_image)],
    )

    first_row = embedded_media._extract_rows_for_file(
        first, soffice="soffice", ocr_fn=lambda path: "", ocr_engine="none"
    )[0]
    second_row = embedded_media._extract_rows_for_file(
        second, soffice="soffice", ocr_fn=lambda path: "", ocr_engine="none"
    )[0]

    assert first_row["sha1"] != second_row["sha1"]
    assert first_row["extra_fields"]["image_sha1"] == second_row["extra_fields"]["image_sha1"]
    assert first_row["extra_fields"]["parent_sha1"] == _sha1_for(first)
    assert second_row["extra_fields"]["parent_sha1"] == _sha1_for(second)


def test_ooxml_embedded_media_rejects_zip_bomb_ratio():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/media/image1.png", b"0" * (2 * 1024 * 1024))

    with pytest.raises(ValueError, match="compression ratio"):
        embedded_media._zip_media(buffer.getvalue())


def test_legacy_media_conversion_kills_process_group_on_timeout(monkeypatch, tmp_path):
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"legacy")
    calls = {"communicate": 0}
    kill_calls = []

    class FakeProcess:
        pid = 12345
        returncode = None

        def communicate(self, timeout=None):
            calls["communicate"] += 1
            if calls["communicate"] == 1:
                raise subprocess.TimeoutExpired("soffice", timeout)
            return b"", b""

    def fake_popen(command, **kwargs):
        assert kwargs["start_new_session"] is True
        return FakeProcess()

    monkeypatch.setattr(embedded_media.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        embedded_media.os,
        "killpg",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )

    result = embedded_media._libreoffice_to_ooxml(
        source,
        "docx",
        tmp_path / "work",
        timeout=1,
    )

    assert result is None
    assert kill_calls == [(12345, embedded_media.signal.SIGKILL)]
    assert calls["communicate"] == 2


def test_embedded_media_ocr_creates_parent_for_image_only_office_file(monkeypatch, tmp_path):
    source = tmp_path / "image-only.doc"
    source.write_bytes(b"legacy parent bytes")
    image_bytes = (ASSET_DIR / "federalist_fixture.png").read_bytes()
    monkeypatch.setattr(
        embedded_media,
        "office_images",
        lambda path, soffice="soffice": [("word/media/image1.jpg", image_bytes)],
    )
    ocr_paths = []

    def fake_ocr(path):
        ocr_paths.append(path)
        return "Recovered embedded image text"

    monkeypatch.setattr(
        embedded_media,
        "_ocr_callable",
        lambda engine: (fake_ocr, "test-ocr"),
    )
    db_path = tmp_path / "image-only.sqlite"

    result = embedded_media.run_media_ingest(
        [str(source)], str(db_path), ocr_engine="plugin", fulltext=False
    )

    assert result["inserted"] == 1
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT sha1, filename, raw_content, json_metadata FROM documents"
        ).fetchone()
    assert row[0] == _sha1_for(source)
    assert row[1] == "image-only.doc"
    assert "Recovered embedded image text" in row[2]
    assert len(json.loads(row[3])["embedded_media"]) == 1
    assert ocr_paths[0].suffix == ".png"


@pytest.mark.asyncio
async def test_native_ingest_merges_embedded_docx_image_ocr_into_parent(monkeypatch, tmp_path):
    from docx import Document

    source = tmp_path / "source"
    source.mkdir()
    image_path = ASSET_DIR / "federalist_fixture.png"
    docx_path = source / "with-image.docx"
    document = Document()
    document.add_paragraph("Parent document text for native extraction.")
    document.add_picture(str(image_path))
    document.save(docx_path)

    ocr_calls = []

    def fake_ocr(path):
        ocr_calls.append(path)
        return "Embedded image OCR fixture text"

    monkeypatch.setattr(
        embedded_media,
        "_ocr_callable",
        lambda engine: (fake_ocr, "test-ocr"),
    )
    db_path = tmp_path / "embedded.sqlite"
    result = await process_ingest(
        db_path=str(db_path),
        input_dir=str(source),
        table="documents",
        ocr_engine="auto",
        workers=1,
        yes=True,
    )

    assert result["successful"] == 1
    assert result["failed"] == 0
    assert result["embedded_media"]["inserted"] == 1
    with sqlite3.connect(db_path) as connection:
        parent = connection.execute(
            """SELECT sha1, raw_content, json_metadata, metadata, added_at
               FROM documents WHERE filename = 'with-image.docx'"""
        ).fetchone()
        row_count = connection.execute("SELECT count(*) FROM documents").fetchone()[0]

    assert parent is not None
    assert "Parent document text for native extraction." in parent[1]
    assert "--- EMBEDDED IMAGE OCR ---" in parent[1]
    assert "Embedded image OCR fixture text" in parent[1]
    media = json.loads(parent[2])["embedded_media"]
    assert len(media) == 1
    assert media[0]["ocr_status"] == "success"
    assert media[0]["ocr_chars"] == len("Embedded image OCR fixture text")
    assert json.loads(parent[3])["embedded_media_ocr_count"] == 1
    assert parent[4]
    assert row_count == 1
    assert len(ocr_calls) == 1
    assert ocr_calls[0].suffix == ".png"

    repeated = await process_ingest(
        db_path=str(db_path),
        input_dir=str(source),
        table="documents",
        ocr_engine="auto",
        workers=1,
        yes=True,
    )
    assert repeated["successful"] == 0
    assert repeated["embedded_media"]["inserted"] == 0
    assert len(ocr_calls) == 1


@pytest.mark.asyncio
async def test_ingest_can_skip_embedded_office_media(monkeypatch, tmp_path):
    from docx import Document

    source = tmp_path / "source"
    source.mkdir()
    docx_path = source / "text-only.docx"
    document = Document()
    document.add_paragraph("Document text remains available.")
    document.save(docx_path)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("embedded media extraction should be skipped")

    monkeypatch.setattr(embedded_media, "run_media_ingest", fail_if_called)
    result = await process_ingest(
        db_path=str(tmp_path / "skip-media.sqlite"),
        input_dir=str(source),
        table="documents",
        workers=1,
        skip_embedded_media=True,
        yes=True,
    )

    assert result["successful"] == 1
    assert result["embedded_media"] == {}


@pytest.mark.asyncio
async def test_process_document_plugin_ocr_for_pdf_and_image(monkeypatch, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\nfake scanned pdf\n")
    sha1 = _sha1_for(pdf_path)

    monkeypatch.setattr(ocr, "ocr_with_plugin", _async_return("==== Page 1 ====\nPlugin OCR text"))
    monkeypatch.setattr(
        ocr,
        "_try_ocr_pdf_with_textra",
        lambda path, file_sha1: pytest.fail("plugin mode must not invoke Textra"),
    )
    monkeypatch.setattr(
        ocr,
        "_try_ocr_pdf_with_ocrmypdf",
        lambda path, file_sha1: pytest.fail("plugin mode must not invoke ocrmypdf"),
    )

    result_sha1, content, metadata = await process_document(
        str(pdf_path),
        sha1,
        pdf_engine="ocr",
        ocr_engine="plugin",
    )
    assert result_sha1 == sha1
    assert "Plugin OCR text" in content
    assert metadata["extraction_method"] == "ocr_plugin"
    assert metadata["ocr_engine"] == "plugin"

    # Legacy alias mac-ocr-only also routes strictly through the plugin.
    image_path = ASSET_DIR / "federalist_fixture.png"
    monkeypatch.setattr(
        ocr,
        "_try_ocr_image_with_textra",
        lambda path, file_sha1: pytest.fail("plugin mode must not invoke Textra"),
    )
    monkeypatch.setattr(
        ocr,
        "_try_ocr_image_with_tesseract",
        lambda path: pytest.fail("plugin mode must not invoke Tesseract"),
    )
    _, img_content, img_meta = await process_document(
        str(image_path), _sha1_for(image_path), ocr_engine="mac-ocr-only"
    )
    assert "Plugin OCR text" in img_content
    assert img_meta["ocr_engine"] == "plugin"


def test_missing_pdftotext_names_install_and_env_var(monkeypatch):
    monkeypatch.setenv("DOCTRAIL_PDFTOTEXT", "/nonexistent/pdftotext")
    assert ocr.find_pdftotext() is None
    with pytest.raises(RuntimeError, match="brew install poppler.*DOCTRAIL_PDFTOTEXT"):
        ocr.require_pdftotext()
