import asyncio
import hashlib
from pathlib import Path

import pytest

from doctrail.ingest.document_processor import process_document


@pytest.mark.asyncio
async def test_process_document_handles_doc(monkeypatch, tmp_path):
    doc_path = tmp_path / "sample.doc"
    doc_path.write_bytes(b"fake doc content")
    sha1 = hashlib.sha1(doc_path.read_bytes()).hexdigest()

    monkeypatch.setattr(
        "doctrail.ingest.document_processor.extract_text_from_doc",
        lambda path: "Line one\nLine two\n"
    )

    result_sha1, content, metadata = await process_document(str(doc_path), sha1)

    assert result_sha1 == sha1
    assert "Line one" in content
    assert metadata["extraction_method"] == "antiword"
    assert metadata["original_file_type"] == "doc"


@pytest.mark.asyncio
async def test_process_document_doc_missing_antiword(monkeypatch, tmp_path):
    doc_path = tmp_path / "sample.doc"
    doc_path.write_bytes(b"fake doc content")
    sha1 = hashlib.sha1(doc_path.read_bytes()).hexdigest()

    def _raise(_):
        raise RuntimeError("antiword is not installed. Install antiword to ingest .doc files.")

    monkeypatch.setattr(
        "doctrail.ingest.document_processor.extract_text_from_doc",
        _raise
    )

    with pytest.raises(ValueError) as excinfo:
        await process_document(str(doc_path), sha1)

    assert "antiword" in str(excinfo.value)
