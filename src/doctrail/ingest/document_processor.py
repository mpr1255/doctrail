"""
Document processing module for Doctrail ingestion.

This module contains the main document processing logic that coordinates
extraction from various file types.
"""

import os
import hashlib
import importlib.util
import asyncio
import subprocess
import platform
import shutil
import tempfile
import sys
import time
from pathlib import Path
from typing import Tuple, Dict, Optional
import chardet
import httpx
from bs4 import BeautifulSoup
from readability import Document
from loguru import logger

# Import from extractors
from ..extractors.mhtml_extractor import (
    extract_mhtml_metadata, process_mhtml_to_html, process_mhtml_to_html_python,
    extract_with_chrome_headless
)
from ..extractors.pdf_extractor import (
    check_for_existing_ocr_pdf, ocr_pdf_with_ocrmypdf, extract_text_with_mutool,
    extract_text_with_pymupdf, extract_text_with_pdftotext
)
from ..extractors.html_extractor import extract_text_with_w3m
from ..extractors.smart_html_extractor import extract_html_text_smart
from ..extractors.epub_extractor import extract_text_from_epub
from ..extractors.mobi_extractor import extract_text_from_mobi
from ..extractors.docx_extractor import extract_text_from_docx
from ..extractors.doc_extractor import extract_text_from_doc, extract_text_from_rtf
from ..extractors.djvu_extractor import extract_text_from_djvu
from ..extractors.spreadsheet_extractor import (
    extract_structured_from_delimited,
    extract_structured_from_xls,
    extract_structured_from_xlsx,
)
from ..extractors.presentation_extractor import extract_text_from_ppt, extract_text_from_pptx

# Import from text processing
from .text_processing import (
    add_page_markers, clean_extracted_text, is_text_garbage, 
    is_content_garbage, clean_ocr_text
)
from ..file_filters import (
    should_skip_file, get_unsupported_file_error, check_for_manual_override
)

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


# Custom exception for skipped files
class SkippedFileException(Exception):
    """Exception raised when a file is intentionally skipped"""
    pass


KNOWN_QUOTA_PAGE_MARKERS = (
    "今日的条目下载量已经达到极限",
    "建议您明天下载",
    "top.location.href='https://www.yearbookchina.com/index.aspx'",
)


def _read_prefix(file_path: str, size: int = 8192) -> bytes:
    with open(file_path, 'rb') as handle:
        return handle.read(size)


def _looks_like_pdf_bytes(prefix: bytes) -> bool:
    return prefix.lstrip().startswith(b'%PDF-')


def _looks_like_html_bytes(prefix: bytes) -> bool:
    sample = prefix.lstrip().lower()
    return (
        sample.startswith(b'<!doctype html')
        or sample.startswith(b'<html')
        or sample.startswith(b'<!--')
        or sample.startswith(b'<script')
        or b'<html' in sample
    )


def _looks_like_mhtml_bytes(prefix: bytes) -> bool:
    sample = prefix.lower()
    return (
        b'mime-version:' in sample
        and b'content-type: multipart/related' in sample
        and (
            b'snapshot-content-location:' in sample
            or b'content-location:' in sample
        )
    )


def _decode_sniff_text(prefix: bytes) -> str:
    if not prefix:
        return ""
    result = chardet.detect(prefix)
    encoding = result.get('encoding') or 'utf-8'
    return prefix.decode(encoding, errors='ignore')


def _is_known_quota_page(text: str) -> bool:
    return any(marker in text for marker in KNOWN_QUOTA_PAGE_MARKERS)


def _normalize_title_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _looks_like_broken_html_title(title: str) -> bool:
    lowered = title.lower()
    suspicious_tokens = (
        "function(",
        "function ",
        "window.",
        "document.",
        "location.",
        "top.location",
        "settimeout",
        "return false",
        "return true",
        "var ",
        "let ",
        "const ",
        "=>",
    )
    if any(token in lowered for token in suspicious_tokens):
        return True
    punctuation_hits = sum(1 for ch in title if ch in "{}();=")
    return punctuation_hits >= 6


def _clean_candidate_title(value: object) -> str:
    title = _normalize_title_text(value)
    if not title or _looks_like_broken_html_title(title):
        return ""
    return title


def _add_title_metadata(metadata: Dict, title: object) -> None:
    clean_title = _clean_candidate_title(title)
    if clean_title:
        metadata['title'] = clean_title


def _extract_html_title(soup: BeautifulSoup) -> str:
    candidates = []

    if soup.title:
        candidates.append(soup.title.get_text(" ", strip=True))

    for attrs in (
        {"property": "og:title"},
        {"name": "og:title"},
        {"name": "twitter:title"},
        {"name": "title"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            candidates.append(tag.get("content"))

    h1 = soup.find("h1")
    if h1:
        candidates.append(h1.get_text(" ", strip=True))

    fallback = ""
    for candidate in candidates:
        normalized = _normalize_title_text(candidate)
        if normalized and not fallback:
            fallback = normalized
        cleaned = _clean_candidate_title(candidate)
        if cleaned:
            return cleaned

    return fallback


def _load_mac_ocr_module():
    module_path = os.environ.get('DOCTRAIL_MAC_OCR_CLIENT_PATH')
    if not module_path:
        raise RuntimeError("DOCTRAIL_MAC_OCR_CLIENT_PATH is not configured")
    client_path = Path(module_path).expanduser()

    if not client_path.exists():
        raise RuntimeError(
            "Mac OCR client not found. Set DOCTRAIL_MAC_OCR_CLIENT_PATH to a local Python client file "
            "that exposes `ocr_async(file_path, node=None)`."
        )

    module_name = f"_doctrail_mac_ocr_{abs(hash(str(client_path)))}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(module_name, client_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Mac OCR client from {client_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    if not hasattr(module, 'ocr_async'):
        raise RuntimeError(f"Mac OCR client at {client_path} does not define ocr_async()")

    return module


def _mac_ocr_service_endpoints() -> Tuple[str, ...]:
    raw_endpoints = (
        os.environ.get("MAC_OCR__SERVICE_ENDPOINTS")
        or os.environ.get("WORKER__OCR_SERVICE_ENDPOINTS")
        or ""
    )
    if not raw_endpoints:
        # An editable pre-PyPI install should work from any cwd. Read only the
        # OCR endpoint key from its source checkout rather than importing every
        # API credential in that checkout's .env file.
        checkout_env = Path(__file__).resolve().parents[3] / ".env"
        if checkout_env.is_file():
            from ..core_utils import parse_env_file

            values = parse_env_file(checkout_env)
            raw_endpoints = (
                values.get("MAC_OCR__SERVICE_ENDPOINTS")
                or values.get("WORKER__OCR_SERVICE_ENDPOINTS")
                or ""
            )
    return tuple(
        endpoint.strip().rstrip("/")
        for endpoint in raw_endpoints.split(",")
        if endpoint.strip()
    )


def _mac_ocr_upload(file_path: str):
    """Return an OCR upload path, normalizing any detected raster to PNG."""
    from PIL import Image, UnidentifiedImageError

    source_path = Path(file_path)
    try:
        with Image.open(source_path) as image:
            image.load()
            if image.mode not in {"1", "L", "LA", "RGB", "RGBA"}:
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            temporary = tempfile.NamedTemporaryFile(suffix=".png")
            image.save(temporary, format="PNG")
            temporary.flush()
    except (UnidentifiedImageError, OSError):
        return source_path, source_path.name, None

    return Path(temporary.name), f"{source_path.stem}.png", temporary


async def _ocr_with_mac_ocr_service(file_path: str) -> str:
    endpoints = _mac_ocr_service_endpoints()
    if not endpoints:
        raise RuntimeError(
            "Mac OCR is not configured; set MAC_OCR__SERVICE_ENDPOINTS to the "
            "comma-separated Tailscale Funnel endpoints"
        )

    wait_seconds = float(os.environ.get("DOCTRAIL_MAC_OCR_CAPACITY_WAIT_SECONDS", "0"))
    poll_seconds = float(os.environ.get("DOCTRAIL_MAC_OCR_POLL_SECONDS", "2"))
    request_seconds = float(os.environ.get("DOCTRAIL_MAC_OCR_REQUEST_TIMEOUT_SECONDS", "0"))
    deadline = time.monotonic() + wait_seconds if wait_seconds > 0 else None
    errors = []
    timeout = (
        httpx.Timeout(request_seconds, connect=10.0)
        if request_seconds > 0
        else httpx.Timeout(None, connect=10.0)
    )
    upload_path, upload_name, temporary_upload = _mac_ocr_upload(file_path)
    with upload_path.open("rb") as upload_handle:
        upload_sha1 = hashlib.file_digest(upload_handle, "sha1").hexdigest()
    preferred_index = int(upload_sha1, 16) % len(endpoints)
    ordered_endpoints = endpoints[preferred_index:] + endpoints[:preferred_index]

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            while deadline is None or time.monotonic() < deadline:
                for endpoint_index, endpoint in enumerate(ordered_endpoints):
                    try:
                        response = await client.post(f"{endpoint}/reserve", timeout=10.0)
                        if response.status_code != 201:
                            if endpoint_index == 0 and response.status_code in {409, 429, 503}:
                                break
                            continue
                        reservation_id = response.json()["reservation_id"]
                        with upload_path.open("rb") as file_handle:
                            response = await client.post(
                                f"{endpoint}/ocr",
                                params={"reservation_id": reservation_id},
                                files={"file": (upload_name, file_handle)},
                                timeout=timeout,
                            )
                        if response.status_code >= 400:
                            raise RuntimeError(
                                f"Mac OCR rejected {Path(file_path).name}: "
                                f"HTTP {response.status_code} {response.text[:200]}"
                            )
                        response.raise_for_status()
                        text = response.json().get("text", "").strip()
                        if not text:
                            raise RuntimeError(f"Mac OCR returned empty text for {file_path}")
                        return text
                    except RuntimeError:
                        raise
                    except Exception as exc:
                        errors.append(f"{endpoint}: {exc}")
                await asyncio.sleep(poll_seconds)
    finally:
        if temporary_upload is not None:
            temporary_upload.close()

    detail = errors[-1] if errors else "no endpoint accepted a reservation"
    raise TimeoutError(f"Timed out waiting for Mac OCR capacity: {detail}")


async def process_document(
    file_path: str,
    file_sha1: str,
    use_readability: bool = False,
    html_extractor: str = 'default',
    skip_garbage_check: bool = False,
    pdf_engine: str = 'auto',
    ocr_engine: str = 'auto',
) -> Tuple[str, str, Dict]:
    """Process a document using specialized extractors and return (sha1, content, metadata)"""
    # Ensure proper UTF-8 encoding for Python I/O
    os.environ['PYTHONIOENCODING'] = 'utf8'
    
    # Check for manual override files first
    override_file = check_for_manual_override(file_path)
    if override_file:
        try:
            with open(override_file, 'r', encoding='utf-8') as f:
                override_content = f.read().strip()
            
            if len(override_content) > 50:  # Sanity check
                logger.info(f"Using manual override content from {override_file} ({len(override_content)} characters)")
                
                # Create metadata indicating manual override
                metadata = {
                    'title': Path(file_path).stem,
                    'original_file_path': file_path,
                    'override_file_path': override_file,
                    'original_file_type': Path(file_path).suffix.lower().lstrip('.'),
                    'Content-Type': 'text/plain',
                    'resourceName': Path(file_path).name,
                    'extraction_method': 'manual_override',
                    'processing_method': f'manual_override_{Path(override_file).stem.split("--")[-1]}'
                }
                
                return file_sha1, override_content, metadata
        except Exception as e:
            logger.warning(f"Failed to read manual override file {override_file}: {e}")
            # Continue with normal processing
    
    logger.info(f"Processing {file_path}")
    
    # Skip files that should be ignored
    if should_skip_file(file_path):
        logger.info(f"Skipping file: {file_path}")
        raise SkippedFileException("File should be skipped")
    
    original_file_path = file_path
    temp_html_file = None  # Initialize this variable at the start

    # Handle plain text files directly (TXT, MD)
    file_extension = Path(file_path).suffix.lower()
    if file_extension in SUPPORTED_EXTENSION_FAMILIES["html"]:
        if _looks_like_mhtml_bytes(_read_prefix(file_path, 65_536)):
            file_extension = '.mhtml'
    if file_extension in SUPPORTED_EXTENSION_FAMILIES["text"]:
        return await _process_text_file(file_path, file_sha1, original_file_path, file_extension)

    if file_extension in SUPPORTED_EXTENSION_FAMILIES["delimited"]:
        return await _process_delimited_file(file_path, file_sha1, original_file_path, file_extension)

    # Handle PDF files
    if file_extension == '.pdf':
        prefix = _read_prefix(file_path)
        if not _looks_like_pdf_bytes(prefix):
            if _looks_like_html_bytes(prefix):
                sniff_text = _decode_sniff_text(prefix)
                if _is_known_quota_page(sniff_text):
                    raise SkippedFileException("Download-limit HTML page saved with .pdf extension")
                return await _process_html_file(
                    file_path,
                    file_sha1,
                    original_file_path,
                    use_readability=use_readability,
                    mhtml_metadata=None,
                    html_extractor=html_extractor,
                    skip_garbage_check=True,
                )
            raise SkippedFileException("File has .pdf extension but does not contain PDF data")
        return await _process_pdf_file(file_path, file_sha1, original_file_path, pdf_engine=pdf_engine, ocr_engine=ocr_engine)

    # Handle EPUB files
    if file_extension == '.epub':
        return await _process_epub_file(file_path, file_sha1, original_file_path)

    # Handle MOBI files
    if file_extension == '.mobi':
        return await _process_mobi_file(file_path, file_sha1, original_file_path)

    # Handle DOC files (antiword, with RTF fallback for disguised files)
    if file_extension == '.doc':
        return await _process_doc_file(file_path, file_sha1, original_file_path)

    # Handle RTF files
    if file_extension == '.rtf':
        return await _process_rtf_file(file_path, file_sha1, original_file_path)

    # Handle DOCX files
    if file_extension == '.docx':
        return await _process_docx_file(file_path, file_sha1, original_file_path)

    # Handle Excel workbooks
    if file_extension == '.xlsx':
        prefix = _read_prefix(file_path)
        if _looks_like_html_bytes(prefix):
            sniff_text = _decode_sniff_text(prefix)
            if _is_known_quota_page(sniff_text):
                raise SkippedFileException("Download-limit HTML page saved with .xlsx extension")
        return await _process_xlsx_file(file_path, file_sha1, original_file_path)

    if file_extension == '.xls':
        prefix = _read_prefix(file_path)
        if _looks_like_html_bytes(prefix):
            sniff_text = _decode_sniff_text(prefix)
            if _is_known_quota_page(sniff_text):
                raise SkippedFileException("Download-limit HTML page saved with .xls extension")
        return await _process_xls_file(file_path, file_sha1, original_file_path)

    # Handle PowerPoint presentations
    if file_extension == '.pptx':
        return await _process_pptx_file(file_path, file_sha1, original_file_path)

    if file_extension == '.ppt':
        return await _process_ppt_file(file_path, file_sha1, original_file_path)

    # Handle DJVU files
    if file_extension == '.djvu':
        return await _process_djvu_file(file_path, file_sha1, original_file_path)

    # Handle MHTML files
    if file_extension in SUPPORTED_EXTENSION_FAMILIES["mhtml"]:
        # Convert to HTML first, then process as HTML
        file_path, file_extension, mhtml_metadata = await _convert_mhtml_to_html(file_path, original_file_path)
        temp_html_file = file_path  # Track for cleanup
    else:
        mhtml_metadata = None

    # Handle HTML files (including converted MHTML)
    if file_extension in SUPPORTED_EXTENSION_FAMILIES["html"]:
        result = await _process_html_file(file_path, file_sha1, original_file_path, use_readability, mhtml_metadata, html_extractor, skip_garbage_check)
        
        # Clean up temporary HTML file if it was created
        if temp_html_file and os.path.exists(temp_html_file):
            try:
                os.unlink(temp_html_file)
                logger.debug(f"Cleaned up temporary HTML file: {temp_html_file}")
            except Exception as e:
                logger.warning(f"Failed to clean up temporary HTML file {temp_html_file}: {e}")
        
        return result

    # Image OCR: honor --ocr-engine mac-ocr (the distributed farm, Chinese-capable)
    # when requested, otherwise local Textra then Tesseract. Routing images to the
    # farm matters at scale — tens of thousands of screenshots across 5 nodes.
    if file_extension in SUPPORTED_EXTENSION_FAMILIES["image_ocr"]:
        text = None
        extraction_method = None
        ocr_engine_used = None
        if ocr_engine == 'mac-ocr':
            mac_ocr_text = await _try_ocr_with_mac_ocr(file_path)
            if mac_ocr_text:
                text = clean_ocr_text(mac_ocr_text)
                extraction_method = 'mac_ocr'
                ocr_engine_used = 'mac-ocr'
        if text is None:
            text = _try_ocr_image_with_textra(file_path, file_sha1)
            extraction_method = 'textra_ocr'
            ocr_engine_used = 'textra'
        if text is None:
            text = _try_ocr_image_with_tesseract(file_path)
            extraction_method = 'tesseract_ocr'
            ocr_engine_used = 'tesseract'
        if text is not None:
            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': file_extension.lstrip('.'),
                'Content-Type': 'image',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': extraction_method,
                'ocr_applied': True,
                'ocr_engine': ocr_engine_used,
            }
            cleaned = clean_ocr_text(clean_extracted_text(text))
            return file_sha1, cleaned, metadata

    # If we get here, the file type is not supported
    raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_text_file(file_path: str, file_sha1: str, original_file_path: str, file_extension: str) -> Tuple[str, str, Dict]:
    """Process plain text files (TXT, MD)"""
    try:
        logger.info(f"Processing text file directly: {file_path}")
        
        # Read the file with encoding detection
        with open(file_path, 'rb') as f:
            raw_data = f.read()
        
        # Detect encoding
        result = chardet.detect(raw_data)
        encoding = result.get('encoding', 'utf-8')
        
        # Read as text
        content = raw_data.decode(encoding, errors='ignore').strip()
        
        if content:
            logger.info(f"Successfully read {len(content)} characters from text file")
            
            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': file_extension.lstrip('.'),
                'Content-Type': 'text/plain' if file_extension == '.txt' else 'text/markdown',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': 'direct_text_read',
                'encoding': encoding
            }
            
            # Clean up the extracted text
            content = clean_extracted_text(content)
            
            return file_sha1, content, metadata
        else:
            logger.warning(f"Text file is empty: {file_path}")
            raise ValueError(f"File is empty (0 bytes): {os.path.basename(file_path)}")
            
    except Exception as e:
        logger.error(f"Error reading text file {file_path}: {str(e)}")
        # Re-raise the original error, don't replace it with unsupported file error
        raise


async def _process_pdf_file(file_path: str, file_sha1: str, original_file_path: str, *, pdf_engine: str = 'auto', ocr_engine: str = 'auto') -> Tuple[str, str, Dict]:
    """
    Process PDF files with multiple extraction methods.

    Extraction order (Python-first approach):
    1. pymupdf (pure Python, fast) - PRIMARY
    2. pdftotext (system tool) - FALLBACK
    3. mutool (system tool) - FALLBACK
    4. OCR (textra or ocrmypdf) - LAST RESORT for scanned PDFs

    Args:
        pdf_engine: 'auto' (default), 'pymupdf', 'pdftotext', 'textra'
        ocr_engine: 'auto' (default), 'textra', 'ocrmypdf'
    """
    try:
        logger.info(f"Processing PDF file: {file_path}")
        
        # First check if an OCR'd version already exists
        existing_ocr_pdf = check_for_existing_ocr_pdf(file_path)
        if existing_ocr_pdf:
            logger.info(f"Using existing OCR'd PDF: {existing_ocr_pdf}")
            file_path = existing_ocr_pdf
            metadata_update = {'ocr_applied': True, 'ocr_file_path': existing_ocr_pdf}
        else:
            metadata_update = {}

        content = None
        extraction_method = None
        pymupdf_content = ""
        pdftotext_content = ""
        mutool_content = ""

        if pdf_engine == 'mac-ocr':
            mac_ocr_text = await _try_ocr_with_mac_ocr(file_path)
            if mac_ocr_text:
                content = clean_ocr_text(mac_ocr_text)
                extraction_method = 'mac_ocr'
                metadata_update['ocr_applied'] = True
                metadata_update['ocr_engine'] = 'mac-ocr'
        elif pdf_engine == 'pymupdf':
            pymupdf_content = extract_text_with_pymupdf(file_path)
            if pymupdf_content and not is_text_garbage(pymupdf_content):
                content = add_page_markers(pymupdf_content)
                extraction_method = 'pymupdf'
        elif pdf_engine == 'pdftotext':
            pdftotext_content = extract_text_with_pdftotext(file_path)
            if pdftotext_content and not is_text_garbage(pdftotext_content):
                content = add_page_markers(pdftotext_content)
                extraction_method = 'pdftotext'
        elif pdf_engine == 'mutool':
            mutool_content = extract_text_with_mutool(file_path)
            if mutool_content and not is_text_garbage(mutool_content):
                content = add_page_markers(mutool_content)
                extraction_method = 'mutool'
        else:
            pymupdf_content = extract_text_with_pymupdf(file_path)
            if pymupdf_content and not is_text_garbage(pymupdf_content):
                content = add_page_markers(pymupdf_content)
                extraction_method = 'pymupdf'
                logger.info("PDF extracted successfully with pymupdf")

            if not content:
                pdftotext_content = extract_text_with_pdftotext(file_path)
                if pdftotext_content and not is_text_garbage(pdftotext_content):
                    content = add_page_markers(pdftotext_content)
                    extraction_method = 'pdftotext'
                    logger.info("PDF extracted successfully with pdftotext")

            if not content:
                mutool_content = extract_text_with_mutool(file_path)
                if mutool_content and not is_text_garbage(mutool_content):
                    content = add_page_markers(mutool_content)
                    extraction_method = 'mutool'
                    logger.info("PDF extracted successfully with mutool")

        if not content:
            logger.info("PDF text extraction did not produce usable text, attempting OCR...")
            resolved_ocr_engine = 'textra' if ocr_engine == 'auto' else ocr_engine

            # OCR is a fallback cascade, not a single exclusive engine. The
            # requested engine only picks which step runs FIRST; every cheaper,
            # local step still runs while we have nothing, so a busy mac-ocr
            # farm (503 'no capacity') degrades to local Apple Vision instead of
            # storing a scanned PDF with empty content (silent data loss).

            # 1. mac-ocr farm (distributed Apple Vision) — only when requested.
            if resolved_ocr_engine == 'mac-ocr':
                mac_ocr_text = await _try_ocr_with_mac_ocr(file_path)
                if mac_ocr_text:
                    content = clean_ocr_text(mac_ocr_text)
                    extraction_method = 'mac_ocr'
                    metadata_update['ocr_applied'] = True
                    metadata_update['ocr_engine'] = 'mac-ocr'

            # 2. Local textra (Apple Vision, Chinese-capable) — the default OCR
            #    engine for 'auto'/'textra', and the fallback when the farm is busy.
            if not content and resolved_ocr_engine in {'mac-ocr', 'textra'}:
                textra_result = _try_ocr_with_textra(file_path, file_sha1)
                if textra_result is not None:
                    content = clean_ocr_text(textra_result)
                    extraction_method = 'textra_ocr'
                    metadata_update['ocr_applied'] = True
                    metadata_update['ocr_engine'] = 'textra'
                    if resolved_ocr_engine == 'mac-ocr':
                        metadata_update['ocr_fallback'] = 'mac-ocr->textra'

            # 3. ocrmypdf (tesseract) — universal last resort so no scanned PDF
            #    is ever stored empty, whatever engine was requested.
            if not content:
                try:
                    ocr_pdf_path = ocr_pdf_with_ocrmypdf(file_path)
                    ocr_content = extract_text_with_pymupdf(ocr_pdf_path)
                    if not ocr_content:
                        ocr_content = extract_text_with_pdftotext(ocr_pdf_path)
                    if ocr_content:
                        content = clean_ocr_text(ocr_content)
                        extraction_method = 'ocrmypdf'
                        metadata_update['ocr_applied'] = True
                        metadata_update['ocr_file_path'] = ocr_pdf_path
                        if resolved_ocr_engine != 'ocrmypdf':
                            metadata_update['ocr_fallback'] = f'{resolved_ocr_engine}->ocrmypdf'
                    else:
                        raise ValueError("OCR extraction failed")
                except Exception as ocr_e:
                    logger.error(f"OCR failed: {ocr_e}")

            if not content:
                content = pymupdf_content or pdftotext_content or mutool_content or ""
                extraction_method = 'extraction_failed'
                metadata_update['text_quality_issue'] = 'extraction_failed'

        # If we still have no content, something went very wrong
        if not content:
            content = ""
            extraction_method = 'no_text_extracted'
            metadata_update['text_quality_issue'] = 'no_text_extracted'
            logger.warning(f"Could not extract any text from PDF: {file_path}")

        # Clean up the text
        content = clean_extracted_text(content)
        
        
        # Optional: for MHTML, try Chrome directly on original file and pick the better-looking text
        if Path(original_file_path).suffix.lower() in ['.mht', '.mhtml'] and os.path.exists(original_file_path):
            try:
                alt_content, alt_title = extract_with_chrome_headless(original_file_path)
                def cjk_score(t: str) -> int:
                    return sum(1 for ch in t if '一' <= ch <= '鿿')
                if alt_content and len(alt_content) > 0:
                    if cjk_score(alt_content) > cjk_score(content):
                        content = alt_content
                        title = alt_title or title
                        extraction_method = 'chrome_headless_mhtml'
                        logger.info("Switched to Chrome-on-MHTML result based on CJK score")
            except Exception as e:
                logger.debug(f"Alt MHTML Chrome extraction failed: {e}")

# Build metadata
        metadata = {
            'original_file_path': original_file_path,
            'original_file_type': 'pdf',
            'Content-Type': 'application/pdf',
            'resourceName': os.path.basename(original_file_path),
            'extraction_method': extraction_method
        }
        metadata.update(metadata_update)
        
        logger.info(f"Successfully extracted {len(content)} characters from PDF using {extraction_method}")
        return file_sha1, content, metadata
    except subprocess.TimeoutExpired:
        logger.error(f"PDF processing timed out for {file_path}")
        raise ValueError(get_unsupported_file_error(original_file_path))
    except Exception as e:
        logger.error(f"Error processing PDF {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


def _is_macos() -> bool:
    try:
        return platform.system() == 'Darwin'
    except Exception:
        return False


def _ensure_textra_on_path():
    try:
        home = Path.home()
        textra_bin = home / ".textra" / "bin"
        if textra_bin.exists():
            path = os.environ.get('PATH', '')
            parts = path.split(os.pathsep) if path else []
            if str(textra_bin) not in parts:
                os.environ['PATH'] = os.pathsep.join([str(textra_bin)] + parts)
    except Exception:
        pass


def _has_textra() -> bool:
    _ensure_textra_on_path()
    return shutil.which('textra') is not None


def _try_ocr_with_textra(file_path: str, file_sha1: str) -> Optional[str]:
    """Run OCR using mutool + textra on macOS if available. Returns text or None if unavailable/failure."""
    if not _is_macos():
        return None
    if not _has_textra():
        logger.warning("Textra not installed; falling back to default OCR. Textra generally yields better results. See https://github.com/freedmand/textra")
        return None
    if shutil.which('mutool') is None:
        logger.warning("mutool not found; install with 'brew install mupdf-tools' for Textra OCR path")
        return None

    temp_dir = Path(f"/tmp/{file_sha1}")
    try:
        logger.info("Using Textra OCR path (mutool + textra)")
        temp_dir.mkdir(exist_ok=True, parents=True)
        # Convert PDF to page images
        cmd_convert = [
            'mutool', 'convert',
            '-O', 'resolution=300',
            '-o', str(temp_dir / 'page-%d.png'),
            str(file_path),
        ]
        res = subprocess.run(cmd_convert, capture_output=True, text=True)
        if res.returncode != 0:
            logger.warning(f"mutool convert failed: {res.stderr}")
            return None
        png_files = sorted(temp_dir.glob('page-*.png'), key=lambda p: int(p.stem.split('-')[1]))
        if not png_files:
            logger.warning("Textra OCR: no PNGs generated from PDF")
            return None

        ocr_parts = []
        for i, img in enumerate(png_files, 1):
            out_txt = temp_dir / f"page-{i}.txt"
            res = subprocess.run(['textra', str(img), '-o', str(out_txt)], capture_output=True, text=True)
            if res.returncode != 0:
                logger.warning(f"Textra failed for page {i}: {res.stderr}")
                ocr_parts.append(f"\n\n==== Page {i} ====\n\n[OCR FAILED]")
                continue
            if out_txt.exists():
                try:
                    text = out_txt.read_text(encoding='utf-8', errors='ignore').strip()
                except Exception:
                    text = out_txt.read_text(errors='ignore').strip()
                ocr_parts.append(f"\n\n==== Page {i} ====\n\n{text}")
            else:
                ocr_parts.append(f"\n\n==== Page {i} ====\n\n[OUTPUT FILE NOT FOUND]")
        return "".join(ocr_parts)
    except Exception as e:
        logger.error(f"Textra OCR error: {e}")
        return None
    finally:
        try:
            if temp_dir.exists():
                # Best-effort cleanup
                import shutil as _shutil
                _shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


async def _try_ocr_with_mac_ocr(file_path: str) -> Optional[str]:
    """OCR a file through Doctrail's configured Mac OCR Funnel endpoints."""
    try:
        return await ocr_with_mac_ocr(file_path)
    except Exception as e:
        logger.warning(f"Mac OCR failed for {file_path}: {e}")
        return None


async def ocr_with_mac_ocr(file_path: str) -> str:
    """Run configured Mac OCR and preserve the real backend error for callers."""
    if os.environ.get("DOCTRAIL_MAC_OCR_CLIENT_PATH"):
        ocr_client = _load_mac_ocr_module()
        text = await ocr_client.ocr_async(file_path, node=None)
    else:
        text = await _ocr_with_mac_ocr_service(file_path)
    cleaned = text.strip()
    if not cleaned:
        raise RuntimeError(f"Mac OCR returned empty text for {file_path}")
    return cleaned

def _try_ocr_image_with_textra(file_path: str, file_sha1: str) -> Optional[str]:
    """OCR a single image via textra on macOS. Returns text or None."""
    if not _is_macos() or not _has_textra():
        return None
    out_dir = Path(f"/tmp/{file_sha1}")
    try:
        out_dir.mkdir(exist_ok=True, parents=True)
        out_txt = out_dir / "output.txt"
        res = subprocess.run(
            ['textra', str(file_path), '-o', str(out_txt)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if res.returncode != 0:
            logger.warning(f"Textra failed on image: {res.stderr}")
            return None
        if out_txt.exists():
            text = out_txt.read_text(encoding='utf-8', errors='ignore').strip()
            return text or None
        return None
    except Exception as e:
        logger.error(f"Textra image OCR error: {e}")
        return None
    finally:
        try:
            import shutil as _shutil
            _shutil.rmtree(out_dir, ignore_errors=True)
        except Exception:
            pass


def _try_ocr_image_with_tesseract(file_path: str) -> Optional[str]:
    """OCR a single image with tesseract when Textra cannot handle the format."""
    tesseract = shutil.which('tesseract')
    if not tesseract:
        return None

    try:
        result = subprocess.run(
            [tesseract, file_path, 'stdout', '-l', 'eng', '--psm', '6'],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as e:
        logger.warning(f"Tesseract image OCR failed for {file_path}: {e}")
        return None

    if result.returncode != 0:
        logger.warning(f"Tesseract failed on image: {result.stderr}")
        return None

    text = result.stdout.strip()
    return text or None


async def _process_epub_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process EPUB files"""
    try:
        logger.info(f"Processing EPUB file: {file_path}")
        content, title = extract_text_from_epub(file_path)
        
        if content:
            # Clean up the extracted text
            content = clean_extracted_text(content)
            
            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'epub',
                'Content-Type': 'application/epub+zip',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': 'epub_direct'
            }
            _add_title_metadata(metadata, title)
            
            logger.info(f"Successfully extracted {len(content)} characters from EPUB")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from EPUB")
            
    except Exception as e:
        logger.error(f"Error processing EPUB {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_mobi_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process MOBI files"""
    try:
        logger.info(f"Processing MOBI file: {file_path}")
        content, title = extract_text_from_mobi(file_path)
        
        if content:
            # Clean up the extracted text
            content = clean_extracted_text(content)
            
            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'mobi',
                'Content-Type': 'application/x-mobipocket-ebook',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': 'ebook_convert'
            }
            _add_title_metadata(metadata, title)
            
            logger.info(f"Successfully extracted {len(content)} characters from MOBI")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from MOBI")
            
    except Exception as e:
        logger.error(f"Error processing MOBI {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_docx_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process DOCX files"""
    try:
        logger.info(f"Processing DOCX file: {file_path}")
        content, title = extract_text_from_docx(file_path)
        
        if content:
            # Clean up the extracted text
            content = clean_extracted_text(content)
            
            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'docx',
                'Content-Type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': 'python_docx'
            }
            _add_title_metadata(metadata, title)
            
            logger.info(f"Successfully extracted {len(content)} characters from DOCX")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from DOCX")
            
    except Exception as e:
        logger.error(f"Error processing DOCX {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_doc_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process legacy DOC files via antiword."""
    try:
        logger.info(f"Processing DOC file: {file_path}")
        content = extract_text_from_doc(file_path)

        if content:
            content = clean_extracted_text(content)

            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'doc',
                'Content-Type': 'application/msword',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': 'antiword'
            }

            logger.info(f"Successfully extracted {len(content)} characters from DOC")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from DOC")

    except RuntimeError as e:
        message = str(e)
        logger.error(f"antiword error for DOC {file_path}: {message}")
        raise ValueError(message)
    except Exception as e:
        logger.error(f"Error processing DOC {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_xlsx_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process XLSX spreadsheets."""
    try:
        logger.info(f"Processing XLSX file: {file_path}")
        content, extraction_method, structured_sheets = extract_structured_from_xlsx(file_path)

        if content:
            content = clean_extracted_text(content)

            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'xlsx',
                'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': extraction_method,
                'spreadsheet_sheet_count': len(structured_sheets),
                '_spreadsheet_sheets': structured_sheets,
            }

            logger.info(f"Successfully extracted {len(content)} characters from XLSX")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from XLSX")

    except RuntimeError as e:
        message = str(e)
        logger.error(f"XLSX extraction error for {file_path}: {message}")
        raise ValueError(message)
    except Exception as e:
        logger.error(f"Error processing XLSX {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_delimited_file(
    file_path: str,
    file_sha1: str,
    original_file_path: str,
    file_extension: str,
) -> Tuple[str, str, Dict]:
    """Process CSV/TSV spreadsheets."""
    try:
        logger.info(f"Processing {file_extension.upper().lstrip('.')} file: {file_path}")
        content, extraction_method, structured_sheets = extract_structured_from_delimited(file_path)

        if content:
            content = clean_extracted_text(content)
            content_type = 'text/tab-separated-values' if file_extension == '.tsv' else 'text/csv'

            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': file_extension.lstrip('.'),
                'Content-Type': content_type,
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': extraction_method,
                'spreadsheet_sheet_count': len(structured_sheets),
                '_spreadsheet_sheets': structured_sheets,
            }

            logger.info(f"Successfully extracted {len(content)} characters from {file_extension.upper().lstrip('.')}")
            return file_sha1, content, metadata
        else:
            raise ValueError(f"No content extracted from {file_extension.upper().lstrip('.')}")

    except RuntimeError as e:
        message = str(e)
        logger.error(f"{file_extension.upper().lstrip('.')} extraction error for {file_path}: {message}")
        raise ValueError(message)
    except Exception as e:
        logger.error(f"Error processing {file_extension.upper().lstrip('.')} {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_xls_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process legacy XLS spreadsheets."""
    try:
        logger.info(f"Processing XLS file: {file_path}")
        content, extraction_method, structured_sheets = extract_structured_from_xls(file_path)

        if content:
            content = clean_extracted_text(content)

            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'xls',
                'Content-Type': 'application/vnd.ms-excel',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': extraction_method,
                'spreadsheet_sheet_count': len(structured_sheets),
                '_spreadsheet_sheets': structured_sheets,
            }

            logger.info(f"Successfully extracted {len(content)} characters from XLS")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from XLS")

    except RuntimeError as e:
        message = str(e)
        logger.error(f"XLS extraction error for {file_path}: {message}")
        raise ValueError(message)
    except Exception as e:
        logger.error(f"Error processing XLS {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_pptx_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process PPTX presentations."""
    try:
        logger.info(f"Processing PPTX file: {file_path}")
        content, extraction_method = extract_text_from_pptx(file_path)

        if content:
            content = clean_extracted_text(content)

            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'pptx',
                'Content-Type': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': extraction_method
            }

            logger.info(f"Successfully extracted {len(content)} characters from PPTX")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from PPTX")

    except RuntimeError as e:
        message = str(e)
        logger.error(f"PPTX extraction error for {file_path}: {message}")
        raise ValueError(message)
    except Exception as e:
        logger.error(f"Error processing PPTX {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_ppt_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process legacy PPT presentations."""
    try:
        logger.info(f"Processing PPT file: {file_path}")
        content, extraction_method = extract_text_from_ppt(file_path)

        if content:
            content = clean_extracted_text(content)

            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'ppt',
                'Content-Type': 'application/vnd.ms-powerpoint',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': extraction_method
            }

            logger.info(f"Successfully extracted {len(content)} characters from PPT")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from PPT")

    except RuntimeError as e:
        message = str(e)
        logger.error(f"PPT extraction error for {file_path}: {message}")
        raise ValueError(message)
    except Exception as e:
        logger.error(f"Error processing PPT {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_rtf_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process RTF files via textutil/unrtf."""
    try:
        logger.info(f"Processing RTF file: {file_path}")
        content = extract_text_from_rtf(file_path)

        if content:
            content = clean_extracted_text(content)

            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'rtf',
                'Content-Type': 'application/rtf',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': 'textutil'
            }

            logger.info(f"Successfully extracted {len(content)} characters from RTF")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from RTF")

    except Exception as e:
        logger.error(f"Error processing RTF {file_path}: {str(e)}")
        raise ValueError(f"RTF extraction failed: {str(e)}")


async def _process_djvu_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process DJVU files"""
    try:
        logger.info(f"Processing DJVU file: {file_path}")
        content, title = extract_text_from_djvu(file_path)
        
        if content:
            # Clean up the extracted text
            content = clean_extracted_text(content)
            
            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'djvu',
                'Content-Type': 'image/vnd.djvu',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': 'djvutxt'
            }
            _add_title_metadata(metadata, title)
            
            logger.info(f"Successfully extracted {len(content)} characters from DJVU")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from DJVU")
            
    except Exception as e:
        logger.error(f"Error processing DJVU {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _convert_mhtml_to_html(file_path: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Convert MHTML to HTML and return (html_path, extension, metadata)"""
    try:
        logger.info(f"Processing MHTML file: {file_path}")
        
        # First extract metadata
        mhtml_metadata = extract_mhtml_metadata(file_path)
        
        # Convert MHTML to HTML
        try:
            temp_html_file = process_mhtml_to_html(file_path)
        except Exception as e:
            logger.warning(f"mhtml-to-html-py failed: {e}, trying fallback converter")
            try:
                temp_html_file = process_mhtml_to_html_python(file_path)
            except Exception as e2:
                logger.error(f"Both MHTML converters failed: {e2}")
                raise ValueError("Failed to convert MHTML to HTML")
        
        return temp_html_file, '.html', mhtml_metadata
        
    except Exception as e:
        logger.error(f"Error processing MHTML: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_html_file(file_path: str, file_sha1: str, original_file_path: str, 
                             use_readability: bool, mhtml_metadata: Optional[Dict],
                             html_extractor: str = 'default', skip_garbage_check: bool = False) -> Tuple[str, str, Dict]:
    """Process HTML files (including converted MHTML)"""
    try:
        logger.info(f"Processing HTML file: {file_path}")
        
        # Read the HTML file
        with open(file_path, 'rb') as f:
            raw_html = f.read()
        
        # Detect encoding
        result = chardet.detect(raw_html)
        encoding = result.get('encoding', 'utf-8')
        
        # Decode HTML
        html_content = raw_html.decode(encoding, errors='ignore')
        
        # First check if content is garbage (encoding issues) - unless skipped
        if not skip_garbage_check and is_content_garbage(html_content):
            logger.warning("HTML content appears to be garbage, trying alternative extraction methods...")

            # 0) Fast path: BeautifulSoup on current decoded HTML
            soup0 = BeautifulSoup(html_content, 'html.parser')
            if html_extractor == 'smart':
                bs_content = extract_html_text_smart(html_content)
            else:
                bs_content = soup0.get_text(separator='\n', strip=True)
            if bs_content and not is_content_garbage(bs_content):
                content = bs_content
                title = _extract_html_title(soup0)
                extraction_method = 'beautifulsoup' if html_extractor != 'smart' else 'beautifulsoup_smart'
            else:
                # 1) Manual re-decode with common encodings
                manual_decoded = None
                for enc in ['utf-8', 'gb18030', 'gbk', 'gb2312', 'big5', 'windows-1252']:
                    try:
                        candidate = raw_html.decode(enc, errors='ignore')
                        if not is_content_garbage(candidate):
                            manual_decoded = candidate
                            logger.info(f"Recovered from mojibake via manual decode: {enc}")
                            break
                    except Exception:
                        continue
                if manual_decoded is not None:
                    soup = BeautifulSoup(manual_decoded, 'html.parser')
                    if html_extractor == 'smart':
                        content = extract_html_text_smart(manual_decoded)
                    else:
                        content = soup.get_text(separator='\n', strip=True)
                    title = _extract_html_title(soup)
                    extraction_method = 'manual_decode_bs4' if html_extractor != 'smart' else 'manual_decode_smart'
                else:
                    # 2) w3m text extractor
                    w3m_content, w3m_title = extract_text_with_w3m(file_path)
                    if w3m_content and not is_content_garbage(w3m_content):
                        content = w3m_content
                        title = _clean_candidate_title(w3m_title)
                        extraction_method = 'w3m_browser'
                    else:
                        # 3) Chrome headless on converted HTML
                        chrome_content, chrome_title = extract_with_chrome_headless(file_path)
                        if chrome_content and not is_content_garbage(chrome_content):
                            content = chrome_content
                            title = _clean_candidate_title(chrome_title)
                            extraction_method = 'chrome_headless'
                        else:
                            # 4) Last resort: Chrome on original MHTML if present
                            if Path(original_file_path).suffix.lower() in ['.mht', '.mhtml'] and os.path.exists(original_file_path):
                                alt_content, alt_title = extract_with_chrome_headless(original_file_path)
                                if alt_content and not is_content_garbage(alt_content):
                                    content = alt_content
                                    title = _clean_candidate_title(alt_title)
                                    extraction_method = 'chrome_headless_mhtml'
                                else:
                                    soup = BeautifulSoup(html_content, 'html.parser')
                                    content = soup.get_text(separator='\n', strip=True) if html_extractor != 'smart' else extract_html_text_smart(html_content)
                                    title = _extract_html_title(soup)
                                    extraction_method = 'beautifulsoup_with_issues' if html_extractor != 'smart' else 'smart_with_issues'
                            else:
                                soup = BeautifulSoup(html_content, 'html.parser')
                                content = soup.get_text(separator='\n', strip=True) if html_extractor != 'smart' else extract_html_text_smart(html_content)
                                title = _extract_html_title(soup)
                                extraction_method = 'beautifulsoup_with_issues' if html_extractor != 'smart' else 'smart_with_issues'
        else:
            # Content looks OK; use readability if requested, otherwise BeautifulSoup
            if use_readability:
                try:
                    doc = Document(html_content)
                    title = _clean_candidate_title(doc.title())
                    if html_extractor == 'smart':
                        content = extract_html_text_smart(doc.summary())
                    else:
                        content = BeautifulSoup(doc.summary(), 'html.parser').get_text(separator='\n', strip=True)
                    extraction_method = 'readability' if html_extractor != 'smart' else 'readability_smart'
                except Exception as e:
                    logger.warning(f"Readability failed: {e}, falling back to BeautifulSoup")
                    soup = BeautifulSoup(html_content, 'html.parser')
                    content = soup.get_text(separator='\n', strip=True) if html_extractor != 'smart' else extract_html_text_smart(html_content)
                    title = _extract_html_title(soup)
                    extraction_method = 'beautifulsoup' if html_extractor != 'smart' else 'beautifulsoup_smart'
            else:
                soup = BeautifulSoup(html_content, 'html.parser')
                content = soup.get_text(separator='\n', strip=True) if html_extractor != 'smart' else extract_html_text_smart(html_content)
                title = _extract_html_title(soup)
                extraction_method = 'beautifulsoup' if html_extractor != 'smart' else 'beautifulsoup_smart'

        # Build metadata
        metadata = {
            'title': title,
            'original_file_path': original_file_path,
            'original_file_type': Path(original_file_path).suffix.lower().lstrip('.'),
            'Content-Type': 'text/html',
            'resourceName': os.path.basename(original_file_path),
            'extraction_method': extraction_method
        }
        
        # If this was an MHTML file, merge in the MHTML metadata
        if mhtml_metadata:
            metadata.update(mhtml_metadata)
            metadata['processing_method'] = extraction_method
        
        # Clean up the extracted text
        content = clean_extracted_text(content)
        
        if content:
            logger.info(f"Successfully extracted {len(content)} characters from HTML using {extraction_method}")
            return file_sha1, content, metadata
        else:
            logger.warning(f"No content extracted from HTML file: {file_path}")
            raise ValueError("No content extracted from HTML")
            
    except Exception as e:
        logger.error(f"Error processing HTML {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))
