"""Tests for the optional Rust extraction accelerator (``doctrail._ingest_native``).

Skipped entirely when the compiled extension is not built (e.g. CI without a
native build). The rest of the suite runs the Python path via the
``DOCTRAIL_DISABLE_NATIVE`` guard in ``conftest.py``; here we clear it so the
Rust path is active.
"""

import pytest

from doctrail.ingest import native_extractor


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
    assert not native_extractor.needs_python_fallback(doc, str(f))


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


def test_spreadsheet_routes_to_python_fallback(tmp_path, native_enabled):
    """Rust extracts CSV cleanly, but spreadsheets must fall back to Python so
    doctrail's structured json_metadata payload is preserved."""
    f = tmp_path / "data.csv"
    f.write_text("Name,Count\nAlice,3\nBob,4\n", encoding="utf-8")

    doc = native_extractor.extract_batch([str(f)])[0]

    assert doc["status"] == "extracted"
    assert doc["source_format"] == "csv"
    assert native_extractor.needs_python_fallback(doc, str(f))


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
