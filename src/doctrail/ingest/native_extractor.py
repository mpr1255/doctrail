"""Native Rust extraction bridge (`doctrail._ingest_native`).

Rust handles all multicore document extraction, PDF text cleaning, HTML/MHTML
parsing and profile filtering, ZIP expansion, and parallel SHA-1 hashing. This
module validates the FFI contract and maps results to Doctrail's ingest record
format.
"""

from __future__ import annotations

import importlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_native_module = None
_native_loaded = False
_native_lock = threading.Lock()
_VALID_STATUSES = {"extracted", "fallback_required", "failed", "skipped_unsupported"}

ZIP_MAX_ENTRIES = 50_000
ZIP_MAX_TOTAL_ENTRIES = 100_000
ZIP_MAX_MEMBER_BYTES = 512 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
ZIP_MAX_COMPRESSION_RATIO = 200
ZIP_MAX_NESTING_DEPTH = 3

HTML_FILTER_KEYS = ("keep_selectors", "drop_selectors", "drop_line_patterns")
HTML_DIAGNOSTIC_KEYS = ("keep_selectors_matched", "nodes_dropped", "lines_dropped", "low_value_reason")


def _native():
    """Import the compiled PyO3 extension (`doctrail._ingest_native`). Cached on success."""
    global _native_module, _native_loaded
    if _native_loaded:
        return _native_module
    with _native_lock:
        if _native_loaded:
            return _native_module
        loaded = None
        for name in ("doctrail._ingest_native", "_ingest_native"):
            try:
                loaded = importlib.import_module(name)
                break
            except ImportError:
                loaded = None
        if loaded is not None:
            _native_module = loaded
            _native_loaded = True
        return loaded


def _require_native():
    native = _native()
    if native is None:
        raise RuntimeError(
            "doctrail's native extraction module (`doctrail._ingest_native`) is not available. "
            "Reinstall doctrail, or build the extension in a source checkout with `make native`."
        )
    return native


def available() -> bool:
    """True when the compiled native extraction extension is importable."""
    return _native() is not None


def html_settings(html_config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Validate HTML settings through the Rust parser and fill in defaults."""
    if html_config is None:
        return None
    native = _require_native()
    if not hasattr(native, "normalize_html_config"):
        raise RuntimeError(
            "installed doctrail._ingest_native is stale; rebuild it with `make native`"
        )
    return json.loads(native.normalize_html_config(json.dumps(html_config)))


def extract_one(
    path: str | os.PathLike[str],
    html: Optional[Dict[str, Any]] = None,
    *,
    pdf_backend: Optional[str] = None,
    pdftotext: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract a single file through the native engine."""
    native = _require_native()
    html_json = json.dumps(html) if html else None
    if pdf_backend is not None or pdftotext is not None:
        raw = native.extract_path(os.fspath(path), html_json, pdf_backend, pdftotext)
    else:
        raw = native.extract_path(os.fspath(path), html_json)
    return json.loads(raw)


def extract_batch(
    paths: List[str],
    threads: Optional[int] = None,
    html: Optional[Dict[str, Any]] = None,
    *,
    pdf_backend: Optional[str] = None,
    pdftotext: Optional[str] = None,
) -> List[Dict[str, Any]]:
    native = _require_native()
    normalized_paths = [str(path) for path in paths]
    html_json = json.dumps(html) if html else None
    if pdf_backend is not None or pdftotext is not None:
        raw = native.extract_batch(normalized_paths, threads, html_json, pdf_backend, pdftotext)
    else:
        raw = native.extract_batch(normalized_paths, threads, html_json)
    docs = [json.loads(item) for item in raw]
    _validate_batch_contract(normalized_paths, docs)
    return docs


def hash_batch(paths: List[str], threads: Optional[int] = None) -> List[Dict[str, Any]]:
    """Stream SHA-1 for many paths in Rust's bounded Rayon pool."""
    native = _require_native()
    normalized_paths = [str(path) for path in paths]
    results = [json.loads(item) for item in native.hash_batch(normalized_paths, threads)]
    if len(results) != len(normalized_paths):
        raise RuntimeError(
            f"native hasher returned {len(results)} result(s) for "
            f"{len(normalized_paths)} path(s)"
        )
    for index, (expected_path, result) in enumerate(zip(normalized_paths, results)):
        if result.get("path") != expected_path:
            raise RuntimeError(f"native hash result {index} path mismatch")
        sha1 = result.get("sha1")
        if sha1 is not None and (
            not isinstance(sha1, str)
            or len(sha1) != 40
            or any(character not in "0123456789abcdef" for character in sha1)
        ):
            raise RuntimeError(f"native hash result {index} has invalid SHA-1")
    return results


def expand_zip(archive_path: str, destination: str) -> List[Dict[str, Any]]:
    """Safely materialize a ZIP archive through the native extension."""
    native = _require_native()
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
    """Reject a malformed native batch before any rows are written."""
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


def to_result(
    file_path: str,
    sha1: str,
    doc: Dict[str, Any],
    html: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Map a Rust extraction dict to doctrail's ingest result dict."""
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
    content_extraction: Dict[str, Any] = {}
    if isinstance(extraction_metadata, dict):
        content_extraction = extraction_metadata.get("content_extraction") or {}
        structured_sheets = content_extraction.get("structured_sheets")
        if isinstance(structured_sheets, list):
            metadata["_spreadsheet_sheets"] = structured_sheets
            metadata["spreadsheet_sheet_count"] = len(structured_sheets)
    if source_format in ("html", "mhtml"):
        metadata["html_mode"] = content_extraction.get("html_mode") or "article"
        if html and metadata["html_mode"] == "full":
            metadata["html_config"] = json.dumps(html, ensure_ascii=False, sort_keys=True)
        for key in HTML_DIAGNOSTIC_KEYS:
            metadata[f"html_{key}"] = content_extraction.get(key)
    if source_format == "mhtml":
        for key in ("mhtml_subject", "original_url", "save_date"):
            if content_extraction.get(key):
                metadata[key] = content_extraction[key]
    if source_format == "pdf":
        for key in ("page_count", "pdf_text_fallback_from"):
            if content_extraction.get(key) is not None:
                metadata[key] = content_extraction[key]
        pages_without_text = content_extraction.get("pages_without_text")
        if isinstance(pages_without_text, list) and pages_without_text:
            metadata["pages_without_text"] = pages_without_text
    metadata = {k: v for k, v in metadata.items() if v is not None}

    return {
        "success": True,
        "file_path": str(file_path),
        "sha1": sha1,
        "content": doc.get("content") or "",
        "metadata": metadata,
        "elapsed": (doc.get("extraction_ms") or 0) / 1000.0,
    }
