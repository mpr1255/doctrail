"""Single-document extraction helper backed by the native Rust engine and OCR."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..file_filters import check_for_manual_override, should_skip_file
from . import native_extractor
from .ocr import (
    LEGACY_OCR_CLIENT_ENV,
    OCR_PLUGIN_ENV,
    PLUGIN_OCR_ENGINES,
    _try_ocr_image_with_tesseract,
    _try_ocr_image_with_textra,
    _try_ocr_pdf_with_textra,
    load_ocr_plugin,
    normalize_ocr_engine,
    ocr_with_plugin,
    require_pdftotext,
    run_ocr,
    try_ocr_with_plugin,
)

# Backwards-compatible aliases kept for callers/tests referencing the older names.
MAC_OCR_CLIENT_ENV = LEGACY_OCR_CLIENT_ENV
MAC_OCR_ENGINES = PLUGIN_OCR_ENGINES
_load_mac_ocr_module = load_ocr_plugin
ocr_with_mac_ocr = ocr_with_plugin
_try_ocr_with_mac_ocr = try_ocr_with_plugin
_try_ocr_with_textra = _try_ocr_pdf_with_textra

SUPPORTED_EXTENSION_FAMILIES: Dict[str, Tuple[str, ...]] = {
    "text": (".txt", ".md"),
    "delimited": (".csv", ".tsv"),
    "pdf": (".pdf",),
    "epub": (".epub",),
    "mobi": (".mobi",),
    "word": (".doc",),
    "rtf": (".rtf",),
    "word_openxml": (".docx",),
    "excel_openxml": (".xlsx",),
    "excel": (".xls",),
    "powerpoint_openxml": (".pptx",),
    "powerpoint": (".ppt",),
    "djvu": (".djvu",),
    "mhtml": (".mhtml", ".mht"),
    "html": (".html", ".htm"),
    "image_ocr": (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif"),
    "archive": (".zip",),
}

SUPPORTED_EXTENSIONS: Tuple[str, ...] = tuple(
    extension
    for extensions in SUPPORTED_EXTENSION_FAMILIES.values()
    for extension in extensions
)


def format_supported_extensions_for_help() -> str:
    """Return the local ingest formats in the same grouping used by dispatch."""
    return "; ".join(
        "/".join(extension.lstrip(".") for extension in extensions)
        for extensions in SUPPORTED_EXTENSION_FAMILIES.values()
    )


class SkippedFileException(Exception):
    """Exception raised when a file is intentionally skipped."""


async def process_document(
    file_path: str,
    file_sha1: str,
    use_readability: bool = False,
    html_extractor: str = "default",
    skip_garbage_check: bool = False,
    pdf_engine: str = "auto",
    ocr_engine: str = "auto",
    html_config: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, Dict[str, Any]]:
    """Extract one document using Doctrail's native engine and configured OCR backend."""
    path = Path(file_path)
    override_file = check_for_manual_override(file_path)
    if override_file:
        override_content = Path(override_file).read_text(encoding="utf-8").strip()
        if len(override_content) > 50:
            return file_sha1, override_content, {
                "title": path.stem,
                "original_file_path": file_path,
                "override_file_path": override_file,
                "original_file_type": path.suffix.lower().lstrip("."),
                "Content-Type": "text/plain",
                "resourceName": path.name,
                "extraction_method": "manual_override",
                "processing_method": f"manual_override_{Path(override_file).stem.split('--')[-1]}",
            }

    if should_skip_file(file_path):
        raise SkippedFileException("File should be skipped")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {suffix or '(none)'} ({path.name})")

    effective_ocr = ocr_engine
    if pdf_engine in {"ocr", "mac-ocr"} and effective_ocr == "auto":
        effective_ocr = "plugin" if pdf_engine == "mac-ocr" else "auto"

    if suffix == ".pdf" and pdf_engine in {"ocr", "mac-ocr"}:
        content, method, engine_used = await run_ocr(
            file_path, file_sha1, ocr_engine=effective_ocr, is_pdf=True
        )
        return file_sha1, content, {
            "title": path.stem,
            "original_file_path": file_path,
            "original_file_type": "pdf",
            "Content-Type": "application/pdf",
            "resourceName": path.name,
            "extraction_method": method,
            "processing_method": "rust-ingestor",
            "ocr_applied": True,
            "ocr_engine": engine_used,
        }

    effective_html = html_config
    if effective_html is None and use_readability:
        effective_html = {"mode": "article"}

    prev_backend = os.environ.get("DOCTRAIL_PDF_BACKEND")
    prev_pdftotext = os.environ.get("DOCTRAIL_PDFTOTEXT")
    try:
        if suffix == ".pdf":
            pdf_backend = "pdftotext" if pdf_engine in ("auto", "pdftotext") else "mupdf"
            if pdf_backend == "pdftotext":
                os.environ["DOCTRAIL_PDFTOTEXT"] = require_pdftotext()
            os.environ["DOCTRAIL_PDF_BACKEND"] = pdf_backend

        doc = native_extractor.extract_one(file_path, html=effective_html)
    finally:
        if prev_backend is None:
            os.environ.pop("DOCTRAIL_PDF_BACKEND", None)
        else:
            os.environ["DOCTRAIL_PDF_BACKEND"] = prev_backend
        if prev_pdftotext is None:
            os.environ.pop("DOCTRAIL_PDFTOTEXT", None)
        else:
            os.environ["DOCTRAIL_PDFTOTEXT"] = prev_pdftotext

    if native_extractor.is_complete_extraction(doc, file_path):
        result = native_extractor.to_result(file_path, file_sha1, doc, effective_html)
        metadata = result["metadata"]
        metadata.setdefault("original_file_path", file_path)
        metadata.setdefault("original_file_type", suffix.lstrip("."))
        metadata.setdefault("resourceName", path.name)
        return file_sha1, result["content"], metadata

    if doc.get("ocr_needed"):
        is_pdf = (doc.get("source_format") == "pdf") or suffix == ".pdf"
        content, method, engine_used = await run_ocr(
            file_path, file_sha1, ocr_engine=effective_ocr, is_pdf=is_pdf
        )
        return file_sha1, content, {
            "title": doc.get("title") or path.stem,
            "original_file_path": file_path,
            "original_file_type": doc.get("source_format") or suffix.lstrip("."),
            "Content-Type": doc.get("mime_type") or ("application/pdf" if is_pdf else "image"),
            "resourceName": path.name,
            "extraction_method": method,
            "processing_method": "rust-ingestor",
            "ocr_applied": True,
            "ocr_engine": engine_used,
        }

    if doc.get("status") == "extracted" and suffix in {".htm", ".html", ".mht", ".mhtml"}:
        raise SkippedFileException(doc.get("error") or "HTML contained no usable document text")

    reason = doc.get("error") or doc.get("ocr_reason") or f"status={doc.get('status')!r}"
    raise ValueError(f"Extraction failed for {path.name}: {reason}")
