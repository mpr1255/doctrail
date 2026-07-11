"""Native (Rust) extraction path.

Thin Python glue around the vendored `doctrail._ingest_native` PyO3 extension
(the vendored extraction crate). Rust does all multicore document extraction
under the hood; this module validates and adapts its output to doctrail's ingest
result-dict contract. The legacy Python extractor is reached only through the
explicit backup switch or the ``DOCTRAIL_DISABLE_NATIVE`` kill-switch.

Interface contract with the Rust binding
----------------------------------------
`_ingest_native.extract_batch(paths: list[str], threads: int | None)` returns a
list (order-preserving, one per input path) of dicts with these keys:

    path: str                     # echoed input path
    status: str                   # "extracted" | "fallback_required" | "failed" | "skipped_unsupported"
    source_format: str | None     # "pdf" | "html" | "docx" | ...
    title: str | None
    content: str                  # extracted text ("" on failure)
    content_chars: int
    language: str | None          # detected language (e.g. "zh", "en")
    language_confidence: float | None
    mime_type: str | None         # detected MIME (e.g. "application/pdf")
    extraction_method: str | None # e.g. "mupdf_smart_paragraphs"
    extraction_metadata: dict | None
    ocr_needed: bool              # Rust flagged this as needing OCR (scanned / mojibake)
    ocr_reason: str | None
    error: str | None

`extract_path(path)` returns a single such dict (used for tests / single files).
The binding never raises per-file: a bad file comes back status="failed".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

_native_module = None
_native_loaded = False
_VALID_STATUSES = {"extracted", "fallback_required", "failed", "skipped_unsupported"}
ZIP_MAX_ENTRIES = 10_000
ZIP_MAX_MEMBER_BYTES = 512 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
ZIP_MAX_COMPRESSION_RATIO = 200
ZIP_MAX_NESTING_DEPTH = 3


def _native():
    """Import the PyO3 extension, whether installed as the doctrail submodule
    (final layout) or as a standalone top-level module (M1 build). Cached."""
    global _native_module, _native_loaded
    if not _native_loaded:
        _native_loaded = True
        for name in ("doctrail._ingest_native", "_ingest_native"):
            try:
                import importlib
                _native_module = importlib.import_module(name)
                break
            except Exception:
                _native_module = None
    return _native_module


def available() -> bool:
    """True if the native extension is importable and not disabled.

    Setting ``DOCTRAIL_DISABLE_NATIVE=1`` forces the Python extraction path even
    when the compiled extension is present. This is both a user kill-switch and
    how the test suite stays deterministic and matches CI (which has no built
    ``.so``) regardless of whether a local dev build dropped one into the
    package.
    """
    if os.environ.get("DOCTRAIL_DISABLE_NATIVE"):
        return False
    return _native() is not None


def extract_batch(paths: List[str], threads: Optional[int] = None) -> List[Dict[str, Any]]:
    native = _native()
    if native is None:
        raise RuntimeError("native extension doctrail._ingest_native is not available")
    normalized_paths = [str(path) for path in paths]
    raw = native.extract_batch(normalized_paths, threads)
    docs = [json.loads(item) for item in raw]
    _validate_batch_contract(normalized_paths, docs)
    return docs


def expand_zip(archive_path: str, destination: str) -> List[Dict[str, Any]]:
    """Safely materialize a ZIP archive through the native extension."""
    native = _native()
    if native is None:
        raise RuntimeError("ZIP ingestion requires doctrail._ingest_native")
    if not hasattr(native, "expand_zip"):
        raise RuntimeError(
            "installed doctrail._ingest_native is stale; rebuild it with `make native`"
        )
    raw = native.expand_zip(
        str(archive_path),
        str(destination),
        ZIP_MAX_ENTRIES,
        ZIP_MAX_MEMBER_BYTES,
        ZIP_MAX_TOTAL_BYTES,
        ZIP_MAX_COMPRESSION_RATIO,
    )
    members = [json.loads(item) for item in raw]
    for index, member in enumerate(members):
        if not isinstance(member, dict):
            raise RuntimeError(f"native ZIP member {index} is not an object")
        if not isinstance(member.get("path"), str) or not isinstance(
            member.get("member_path"), str
        ):
            raise RuntimeError(f"native ZIP member {index} has an invalid path contract")
        if not isinstance(member.get("uncompressed_bytes"), int):
            raise RuntimeError(f"native ZIP member {index} has an invalid size contract")
    return members


def _validate_batch_contract(paths: List[str], docs: List[Dict[str, Any]]) -> None:
    """Reject a malformed native batch before any rows are written.

    The ingest loop relies on one order-preserving result per input path. A
    truncated or reordered FFI response must fail the whole chunk instead of
    silently dropping or misattributing documents.
    """
    if len(docs) != len(paths):
        raise RuntimeError(
            f"native extractor returned {len(docs)} result(s) for {len(paths)} path(s)"
        )

    for index, (expected_path, doc) in enumerate(zip(paths, docs)):
        if not isinstance(doc, dict):
            raise RuntimeError(f"native extractor result {index} is not an object")
        if doc.get("path") != expected_path:
            raise RuntimeError(
                f"native extractor result {index} path mismatch: "
                f"expected {expected_path!r}, got {doc.get('path')!r}"
            )
        if doc.get("status") not in _VALID_STATUSES:
            raise RuntimeError(
                f"native extractor result {index} has invalid status {doc.get('status')!r}"
            )
        if not isinstance(doc.get("content"), str):
            raise RuntimeError(f"native extractor result {index} has non-string content")


def is_complete_extraction(doc: Dict[str, Any], file_path: str) -> bool:
    """Return whether a native result is safe to write without intervention."""
    if doc.get("ocr_needed"):
        return False
    if doc.get("status") != "extracted":
        return False
    content = doc.get("content") or ""
    if not content.strip():
        try:
            if Path(file_path).stat().st_size > 0:
                return False
        except OSError:
            return False
    return True


def to_result(file_path: str, sha1: str, doc: Dict[str, Any]) -> Dict[str, Any]:
    """Map a Rust extraction dict to doctrail's ingest result dict.

    Matches the shape produced by ``core._build_success_result`` so it can be
    handed straight to ``core.handle_result`` (metadata cleaning, sidecar merge,
    ``insert_document``). Only non-null metadata keys are emitted; the important
    fields (title, language, extraction_method) are promoted to top-level
    columns by ``insert_document``.
    """
    source_format = doc.get("source_format")
    metadata: Dict[str, Any] = {
        "extraction_method": doc.get("extraction_method") or (f"rust:{source_format}" if source_format else "rust"),
        "processing_method": "rust-ingestor",
        "Content-Type": doc.get("mime_type"),
        "source_format": source_format,
        "title": doc.get("title") or None,
        "language": doc.get("language"),
        "language_confidence": doc.get("language_confidence"),
    }
    extraction_metadata = doc.get("extraction_metadata")
    if isinstance(extraction_metadata, dict):
        structured_sheets = extraction_metadata.get("content_extraction", {}).get(
            "structured_sheets"
        )
        if isinstance(structured_sheets, list):
            metadata["_spreadsheet_sheets"] = structured_sheets
    metadata = {k: v for k, v in metadata.items() if v is not None}

    return {
        "success": True,
        "file_path": str(file_path),
        "sha1": sha1,
        "content": doc.get("content") or "",
        "metadata": metadata,
        "elapsed": (doc.get("extraction_ms") or 0) / 1000.0,
    }
