"""Tests for the optional Rust extraction accelerator (``doctrail._ingest_native``).

Skipped entirely when the compiled extension is not built (e.g. CI without a
native build). The rest of the suite runs the Python path via the
``DOCTRAIL_DISABLE_NATIVE`` guard in ``conftest.py``; here we clear it so the
Rust path is active.
"""

import json
import io
import sqlite3
import shutil
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont

from doctrail.ingest import native_extractor
from doctrail.ingest.core import _expand_zip_inputs, process_ingest


pytestmark = pytest.mark.skipif(
    native_extractor._native() is None,
    reason="native extraction extension (doctrail._ingest_native) is not built",
)


@pytest.fixture
def native_enabled(monkeypatch):
    """Clear the suite-wide kill-switch so the Rust extraction path is active."""
    monkeypatch.delenv("DOCTRAIL_DISABLE_NATIVE", raising=False)
    assert native_extractor.available()


def test_extract_batch_plain_text(tmp_path, native_enabled):
    f = tmp_path / "hello.txt"
    f.write_text("Hello doctrail from Rust.\n", encoding="utf-8")

    docs = native_extractor.extract_batch([str(f)])

    assert len(docs) == 1
    doc = docs[0]
    assert doc["status"] == "extracted"
    assert doc["source_format"] == "text"
    assert "Hello doctrail" in doc["content"]
    assert native_extractor.is_complete_extraction(doc, str(f))


def test_extract_batch_html_uses_readability(tmp_path, native_enabled):
    f = tmp_path / "page.html"
    f.write_text(
        "<html><body><h1>Title</h1><p>Some paragraph text here.</p></body></html>",
        encoding="utf-8",
    )

    doc = native_extractor.extract_batch([str(f)])[0]

    assert doc["status"] == "extracted"
    assert doc["source_format"] == "html"
    assert "Some paragraph text here." in doc["content"]


def test_extract_batch_rejects_login_placeholder(tmp_path, native_enabled):
    page = tmp_path / "login.html"
    page.write_text(
        "<html><body>"
        + ("会员登录 立即注册 用户名 密码 服务热线：400-810-9888 " * 20)
        + "</body></html>",
        encoding="utf-8",
    )

    doc = native_extractor.extract_batch([str(page)])[0]

    assert doc["status"] == "extracted"
    assert doc["content"] == ""
    assert not native_extractor.is_complete_extraction(doc, str(page))


def test_extract_batch_preserves_order_and_count(tmp_path, native_enabled):
    paths = []
    for i in range(6):
        f = tmp_path / f"doc{i}.txt"
        f.write_text(f"content number {i}\n", encoding="utf-8")
        paths.append(str(f))

    docs = native_extractor.extract_batch(paths)

    assert len(docs) == len(paths)
    for i, (path, doc) in enumerate(zip(paths, docs)):
        assert doc["path"] == path
        assert f"number {i}" in doc["content"]


@pytest.mark.parametrize(
    "payload, error",
    [
        ([], "returned 0 result"),
        ([{"path": "wrong", "status": "extracted", "content": "ok"}], "path mismatch"),
        ([{"path": "PATH", "status": "mystery", "content": "ok"}], "invalid status"),
        ([{"path": "PATH", "status": "extracted", "content": None}], "non-string content"),
    ],
)
def test_extract_batch_rejects_malformed_native_contract(tmp_path, monkeypatch, payload, error):
    path = str(tmp_path / "input.txt")
    adjusted = [
        {key: path if value == "PATH" else value for key, value in item.items()}
        for item in payload
    ]

    class FakeNative:
        @staticmethod
        def extract_batch(paths, threads):
            return [json.dumps(item) for item in adjusted]

    monkeypatch.setattr(native_extractor, "_native_module", FakeNative())
    monkeypatch.setattr(native_extractor, "_native_loaded", True)

    with pytest.raises(RuntimeError, match=error):
        native_extractor.extract_batch([path])


def test_spreadsheet_is_a_complete_native_extraction(tmp_path, native_enabled):
    f = tmp_path / "data.csv"
    f.write_text("Name,Count\nAlice,3\nBob,4\n", encoding="utf-8")

    doc = native_extractor.extract_batch([str(f)])[0]

    assert doc["status"] == "extracted"
    assert doc["source_format"] == "csv"
    assert native_extractor.is_complete_extraction(doc, str(f))
    result = native_extractor.to_result(str(f), "spreadsheet-sha1", doc)
    sheets = result["metadata"]["_spreadsheet_sheets"]
    assert sheets[0]["rows"] == [["Name", "Count"], ["Alice", "3"], ["Bob", "4"]]
    assert sheets[0]["delimiter"] == ","


def test_sparse_xlsx_with_bounded_dimension_stays_native(tmp_path, native_enabled):
    ordinary = tmp_path / "ordinary.xlsx"
    workbook = Workbook()
    workbook.active["A1"] = "bounded spreadsheet fallback"
    workbook.save(ordinary)

    sparse = tmp_path / "sparse.xlsx"
    with zipfile.ZipFile(ordinary) as source, zipfile.ZipFile(sparse, "w") as target:
        for member in source.infolist():
            content = source.read(member.filename)
            if member.filename == "xl/worksheets/sheet1.xml":
                updated = content.replace(
                    b'<dimension ref="A1:A1"/>',
                    b'<dimension ref="A1:XFD332"/>',
                )
                assert updated != content
                content = updated
            target.writestr(member, content)

    doc = native_extractor.extract_batch([str(sparse)], 1)[0]

    assert doc["status"] == "extracted"
    assert doc["source_format"] == "xlsx"
    assert "bounded spreadsheet fallback" in doc["content"]


def test_scanned_pdf_returns_ocr_signal_without_running_local_ocr(tmp_path, native_enabled):
    image = Image.new("RGB", (1600, 500), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 110)
    draw.text((80, 160), "DOCTRAIL TEXTRA PDF", fill="black", font=font)
    scanned_pdf = tmp_path / "scanned.pdf"
    image.save(scanned_pdf, "PDF", resolution=150)

    doc = native_extractor.extract_batch([str(scanned_pdf)], 1)[0]

    assert doc["status"] == "extracted"
    assert doc["source_format"] == "pdf"
    assert doc["extraction_method"] == "mupdf_smart_paragraphs"
    assert doc["ocr_needed"] is True


@pytest.mark.parametrize(
    ("filename", "required_tool", "expected_format", "expected_method"),
    [
        ("federalist_fixture.mobi", "ebook-convert", "mobi", "ebook-convert"),
        ("federalist_fixture.djvu", "djvutxt", "djvu", "djvutxt"),
        ("federalist_fixture.rtf", "textutil", "rtf", "textutil"),
        ("federalist_fixture.ppt", "strings", "ppt", "strings"),
    ],
)
def test_native_external_lanes_cover_python_only_fixtures(
    native_enabled, filename, required_tool, expected_format, expected_method
):
    if shutil.which(required_tool) is None:
        pytest.skip(f"{required_tool} is not installed")
    path = Path(__file__).parent / "assets" / "files" / filename

    doc = native_extractor.extract_batch([str(path)], 1)[0]

    assert doc["status"] == "extracted", doc
    assert doc["source_format"] == expected_format
    assert "Federalist fixture" in doc["content"]
    assert doc["ocr_needed"] is False
    if expected_method:
        assert doc["extraction_method"] == expected_method


def test_native_scanned_pdf_defers_to_configured_ocr_backend(tmp_path, native_enabled):
    from PIL import Image

    image_path = Path(__file__).parent / "assets" / "files" / "federalist_fixture.png"
    pdf_path = tmp_path / "scanned.pdf"
    with Image.open(image_path) as image:
        image.convert("RGB").save(pdf_path, "PDF", resolution=150)

    doc = native_extractor.extract_batch([str(pdf_path)], 1)[0]

    assert doc["status"] == "extracted", doc
    assert doc["source_format"] == "pdf"
    assert doc["ocr_needed"] is True
    assert doc["extraction_method"] == "mupdf_smart_paragraphs"


def test_native_image_defers_to_configured_ocr_backend(native_enabled):
    path = Path(__file__).parent / "assets" / "files" / "federalist_fixture.png"

    doc = native_extractor.extract_batch([str(path)], 1)[0]

    assert doc["status"] == "fallback_required"
    assert doc["source_format"] == "png"
    assert doc["ocr_needed"] is True
    assert doc["ocr_reason"] == "image_requires_ocr"


def test_to_result_maps_to_ingest_contract(tmp_path, native_enabled):
    f = tmp_path / "hello.txt"
    f.write_text("Hello doctrail.\n", encoding="utf-8")
    doc = native_extractor.extract_batch([str(f)])[0]

    result = native_extractor.to_result(str(f), "deadbeef", doc)

    assert result["success"] is True
    assert result["sha1"] == "deadbeef"
    assert result["file_path"] == str(f)
    assert "Hello doctrail" in result["content"]
    assert result["metadata"]["processing_method"] == "rust-ingestor"


def test_disable_native_env_forces_python_path(tmp_path, monkeypatch):
    """The kill-switch makes available() report False even when the .so is built."""
    monkeypatch.setenv("DOCTRAIL_DISABLE_NATIVE", "1")
    assert native_extractor.available() is False
    monkeypatch.delenv("DOCTRAIL_DISABLE_NATIVE", raising=False)
    assert native_extractor.available() is True


@pytest.mark.asyncio
async def test_process_ingest_writes_native_rows_to_sqlite(tmp_path, native_enabled):
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.txt").write_text("First document from the native extractor.", encoding="utf-8")
    (source / "two.html").write_text(
        "<html><body><article><h1>Second</h1><p>Native HTML body.</p></article></body></html>",
        encoding="utf-8",
    )
    db_path = tmp_path / "native.sqlite"

    result = await process_ingest(
        db_path=str(db_path),
        input_dir=str(source),
        table="documents",
        extractor="rust",
        workers=2,
        yes=True,
    )

    assert result["successful"] == 2
    assert result["failed"] == 0
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT filename, processing_method, raw_content FROM documents ORDER BY filename"
        ).fetchall()
    assert [row[0] for row in rows] == ["one.txt", "two.html"]
    assert all(row[1] == "rust-ingestor" for row in rows)
    assert "First document" in rows[0][2]
    assert "Native HTML body" in rows[1][2]


@pytest.mark.asyncio
async def test_process_ingest_records_whole_chunk_failure_without_python_fallback(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.txt").write_text("First fallback document.", encoding="utf-8")
    (source / "two.txt").write_text("Second fallback document.", encoding="utf-8")
    db_path = tmp_path / "fallback.sqlite"

    monkeypatch.setattr(native_extractor, "available", lambda: True)
    monkeypatch.setattr(
        native_extractor,
        "extract_batch",
        lambda paths, threads: (_ for _ in ()).throw(RuntimeError("bad batch contract")),
    )

    result = await process_ingest(
        db_path=str(db_path),
        input_dir=str(source),
        table="documents",
        extractor="rust",
        workers=2,
        yes=True,
    )

    assert result["successful"] == 0
    assert result["failed"] == 2
    with sqlite3.connect(db_path) as conn:
        table_exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()[0]
    assert table_exists == 0


@pytest.mark.asyncio
async def test_auto_mode_fails_closed_when_native_extension_is_unavailable(
    tmp_path, native_enabled, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "document.txt").write_text("must not use Python implicitly", encoding="utf-8")
    monkeypatch.setattr(native_extractor, "available", lambda: False)

    with pytest.raises(RuntimeError, match="Native extraction is required"):
        await process_ingest(
            db_path=str(tmp_path / "closed.sqlite"),
            input_dir=str(source),
            table="documents",
            extractor="auto",
            workers=1,
            yes=True,
        )


def test_expand_zip_materializes_safe_members(tmp_path, native_enabled):
    archive = tmp_path / "sample.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("docs/one.txt", "first member")
        zf.writestr("two.html", "<p>second member</p>")

    members = native_extractor.expand_zip(str(archive), str(tmp_path / "expanded"))

    assert [member["member_path"] for member in members] == ["docs/one.txt", "two.html"]
    assert Path(members[0]["path"]).read_text(encoding="utf-8") == "first member"
    assert members[1]["uncompressed_bytes"] == len("<p>second member</p>")


def test_expand_zip_skips_oversized_archive_without_aborting_corpus(
    tmp_path, native_enabled, monkeypatch
):
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_text("ordinary document", encoding="utf-8")
    first = tmp_path / "first.zip"
    oversized = tmp_path / "oversized.zip"
    second = tmp_path / "second.zip"
    for archive in (first, oversized, second):
        archive.write_bytes(b"test archive placeholder")

    def fake_expand_zip(archive_path, destination):
        archive_name = Path(archive_path).name
        destination_path = Path(destination)
        destination_path.mkdir(parents=True)
        member_path = destination_path / f"{archive_name}.txt"
        member_path.write_text(archive_name, encoding="utf-8")
        return [{
            "member_path": member_path.name,
            "path": str(member_path),
            "uncompressed_bytes": 11 if archive_name == "oversized.zip" else 7,
        }]

    monkeypatch.setattr(native_extractor, "expand_zip", fake_expand_zip)
    monkeypatch.setattr(native_extractor, "ZIP_MAX_TOTAL_BYTES", 10)
    monkeypatch.setattr(native_extractor, "ZIP_MAX_TOTAL_ENTRIES", 10)

    leaves, logical_paths, _, staging, failures = _expand_zip_inputs(
        [ordinary, first, oversized, second]
    )
    try:
        assert ordinary in leaves
        assert sorted(Path(path).read_text(encoding="utf-8") for path in logical_paths) == [
            "first.zip",
            "second.zip",
        ]
        assert failures == [(
            str(oversized),
            "expansion tree would reach 11 bytes, above safety limit 10",
        )]
    finally:
        assert staging is not None
        staging.cleanup()


@pytest.mark.asyncio
async def test_process_ingest_expands_nested_zip_with_provenance(tmp_path, native_enabled):
    inner_bytes = io.BytesIO()
    with zipfile.ZipFile(inner_bytes, "w", compression=zipfile.ZIP_DEFLATED) as inner:
        inner.writestr("nested/two.html", "<html><body><p>Nested Rust member.</p></body></html>")

    source = tmp_path / "source"
    source.mkdir()
    archive = source / "bundle.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as outer:
        outer.writestr("docs/one.txt", "Top-level Rust member.")
        outer.writestr("inner.zip", inner_bytes.getvalue())
        outer.writestr("__MACOSX/._junk.txt", "metadata detritus")

    db_path = tmp_path / "archive.sqlite"
    result = await process_ingest(
        db_path=str(db_path),
        input_dir=str(source),
        table="documents",
        extractor="rust",
        workers=2,
        yes=True,
    )

    assert result["successful"] == 2
    assert result["failed"] == 0
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT filepath, raw_content, metadata FROM documents ORDER BY filename"
        ).fetchall()
    assert len(rows) == 2
    assert all("bundle.zip!/" in row[0] for row in rows)
    assert any("Top-level Rust member" in row[1] for row in rows)
    assert any("Nested Rust member" in row[1] for row in rows)
    metadata = [json.loads(row[2]) for row in rows]
    assert {item["archive_depth"] for item in metadata} == {"1", "2"}
    assert any("inner.zip!/nested/two.html" in item["archive_member_path"] for item in metadata)


@pytest.mark.asyncio
async def test_process_ingest_contains_corrupt_zip_failure(tmp_path, native_enabled):
    source = tmp_path / "source"
    source.mkdir()
    (source / "good.txt").write_text("The valid document survives.", encoding="utf-8")
    (source / "broken.zip").write_bytes(b"not a zip archive")

    db_path = tmp_path / "archive.sqlite"
    result = await process_ingest(
        db_path=str(db_path),
        input_dir=str(source),
        table="documents",
        extractor="rust",
        workers=1,
        yes=True,
    )

    assert result["successful"] == 1
    assert result["failed"] == 1
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT filename, raw_content FROM documents").fetchall()
    assert rows == [("good.txt", "The valid document survives.")]


@pytest.mark.asyncio
async def test_native_zero_page_pdf_still_tries_mac_ocr_and_preserves_failure(
    tmp_path, native_enabled, monkeypatch, capsys
):
    source = tmp_path / "source"
    source.mkdir()
    broken_pdf = source / "truncated.pdf"
    broken_pdf.write_bytes(b"%PDF-1.6\ntruncated")

    monkeypatch.setattr(
        native_extractor,
        "extract_batch",
        lambda paths, threads: [{
            "path": paths[0],
            "status": "extracted",
            "content": "",
            "source_format": "pdf",
            "extraction_method": "mupdf_smart_paragraphs",
            "extraction_ms": 1,
            "ocr_needed": True,
            "ocr_reason": "zero_pages",
        }],
    )

    attempted = []

    async def failing_ocr(path):
        attempted.append(path)
        raise RuntimeError("renderer returned HTTP 500")

    monkeypatch.setattr("doctrail.ingest.core.ocr_with_mac_ocr", failing_ocr)
    result = await process_ingest(
        db_path=str(tmp_path / "documents.db"),
        input_dir=str(source),
        table="documents",
        extractor="rust",
        ocr_engine="mac-ocr",
        workers=1,
        yes=True,
    )

    assert result["failed"] == 1
    assert attempted == [str(broken_pdf)]
    output = capsys.readouterr().out
    assert "Mac OCR failed after zero_pages" in output
    assert "renderer returned HTTP 500" in output


@pytest.mark.asyncio
async def test_native_zero_page_pdf_classifies_confirmed_renderer_failure_as_damage(
    tmp_path, native_enabled, monkeypatch, capsys
):
    source = tmp_path / "source"
    source.mkdir()
    broken_pdf = source / "truncated.pdf"
    broken_pdf.write_bytes(b"%PDF-1.6\ntruncated")

    monkeypatch.setattr(
        native_extractor,
        "extract_batch",
        lambda paths, threads: [{
            "path": paths[0],
            "status": "extracted",
            "content": "",
            "source_format": "pdf",
            "extraction_method": "mupdf_smart_paragraphs",
            "extraction_ms": 1,
            "ocr_needed": True,
            "ocr_reason": "zero_pages",
        }],
    )

    async def unrenderable_ocr(path):
        raise RuntimeError(
            "HTTP 500 No PNG files generated from PDF after trying all resolutions"
        )

    monkeypatch.setattr("doctrail.ingest.core.ocr_with_mac_ocr", unrenderable_ocr)
    result = await process_ingest(
        db_path=str(tmp_path / "documents.db"),
        input_dir=str(source),
        table="documents",
        extractor="rust",
        ocr_engine="mac-ocr",
        workers=1,
        yes=True,
    )

    assert result["failed"] == 1
    output = capsys.readouterr().out
    assert "Damaged/unrenderable PDF" in output
    assert "MuPDF found zero pages" in output


@pytest.mark.asyncio
async def test_process_ingest_skips_css_before_native_submission(
    tmp_path, native_enabled, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "document.txt").write_text("Useful document", encoding="utf-8")
    (source / "theme.css").write_text("body { color: red; }", encoding="utf-8")

    submitted = []
    real_extract_batch = native_extractor.extract_batch

    def recording_extract_batch(paths, threads=None):
        submitted.extend(paths)
        return real_extract_batch(paths, threads)

    monkeypatch.setattr(native_extractor, "extract_batch", recording_extract_batch)
    result = await process_ingest(
        db_path=str(tmp_path / "documents.db"),
        input_dir=str(source),
        table="documents",
        extractor="rust",
        workers=1,
        yes=True,
    )

    assert result["successful"] == 1
    assert result["failed"] == 0
    assert submitted == [str(source / "document.txt")]


@pytest.mark.asyncio
async def test_native_ingest_defers_ocr_until_all_rust_batches_finish(
    tmp_path, native_enabled, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    paths = []
    for index in range(33):
        path = source / f"document-{index:02}.txt"
        path.write_text(f"document {index}", encoding="utf-8")
        paths.append(path)

    events = []

    def fake_extract_batch(batch_paths, threads=None):
        events.append("rust")
        docs = []
        for batch_path in batch_paths:
            needs_ocr = batch_path == str(paths[0])
            docs.append({
                "path": batch_path,
                "status": "fallback_required" if needs_ocr else "extracted",
                "content": "" if needs_ocr else f"extracted {Path(batch_path).name}",
                "source_format": "text",
                "extraction_method": "text",
                "extraction_ms": 1,
                "ocr_needed": needs_ocr,
                "ocr_reason": "test_ocr" if needs_ocr else None,
            })
        return docs

    async def fake_ocr(path):
        events.append("ocr")
        return "queued OCR text"

    monkeypatch.setattr(native_extractor, "extract_batch", fake_extract_batch)
    monkeypatch.setattr("doctrail.ingest.core.ocr_with_mac_ocr", fake_ocr)
    result = await process_ingest(
        db_path=str(tmp_path / "documents.db"),
        input_dir=str(source),
        table="documents",
        extractor="rust",
        ocr_engine="mac-ocr",
        workers=1,
        yes=True,
    )

    assert result["successful"] == 33
    assert events == ["rust", "rust", "ocr"]
