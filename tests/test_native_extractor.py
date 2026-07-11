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

from doctrail.ingest import native_extractor
from doctrail.ingest.core import process_ingest


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


@pytest.mark.parametrize(
    ("filename", "required_tool", "expected_format", "expected_method"),
    [
        ("federalist_fixture.mobi", "ebook-convert", "mobi", "ebook-convert"),
        ("federalist_fixture.djvu", "djvutxt", "djvu", "djvutxt"),
        ("federalist_fixture.rtf", "textutil", "rtf", "textutil"),
        ("federalist_fixture.ppt", "strings", "ppt", "strings"),
        ("federalist_fixture.png", "tesseract", "png", None),
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


def test_native_scanned_pdf_runs_bounded_ocr_lane(tmp_path, native_enabled):
    if shutil.which("ocrmypdf") is None or shutil.which("tesseract") is None:
        pytest.skip("ocrmypdf and tesseract are required")
    from PIL import Image

    image_path = Path(__file__).parent / "assets" / "files" / "federalist_fixture.png"
    pdf_path = tmp_path / "scanned.pdf"
    with Image.open(image_path) as image:
        image.convert("RGB").save(pdf_path, "PDF", resolution=150)

    doc = native_extractor.extract_batch([str(pdf_path)], 1)[0]

    assert doc["status"] == "extracted", doc
    assert doc["source_format"] == "pdf"
    assert doc["ocr_needed"] is False
    assert doc["extraction_method"] == "ocrmypdf"
    assert "Federalist fixture" in doc["content"]


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
