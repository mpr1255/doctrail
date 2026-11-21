"""OCR routing and external OCR plugin contract for Doctrail ingestion.

Doctrail ships no OCR service of its own. For scanned PDFs and images it either:

1. Runs a user-supplied OCR plugin (`--ocr-engine plugin`, pointing
   `DOCTRAIL_OCR_PLUGIN` at a Python file that defines
   `async def ocr_async(file_path: str) -> str` or `def ocr(file_path: str) -> str`).
   Plugin mode never falls back to local OCR: a failure raises so the file is
   recorded as failed and retried on a later run.
2. Uses locally installed tools when `--ocr-engine` is `auto`, `textra`, or
   `ocrmypdf` (`textra` + `mutool` on macOS, `ocrmypdf` for PDFs, `tesseract`
   for images).
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

from .text_processing import clean_ocr_text

_PLUGIN_LOCK = threading.Lock()
_TESSERACT_LANGS_LOCK = threading.Lock()
_TESSERACT_LANGS_CACHE: Optional[frozenset[str]] = None
_LOCAL_OCR_STATE = threading.local()

OCR_PLUGIN_ENV = "DOCTRAIL_OCR_PLUGIN"
# Legacy env var name kept as a silent fallback for existing scripts and crons.
LEGACY_OCR_CLIENT_ENV = "DOCTRAIL_MAC_OCR_CLIENT_PATH"

PLUGIN_OCR_ENGINES = frozenset({"plugin", "mac-ocr", "mac-ocr-only"})
VALID_OCR_ENGINES = frozenset({"auto", "plugin", "textra", "ocrmypdf", "mac-ocr", "mac-ocr-only"})

PDFTOTEXT_INSTALL_HINT = (
    "pdftotext is the default PDF extractor but was not found. Install Poppler "
    "(macOS: `brew install poppler`; Debian/Ubuntu: `apt install poppler-utils`), "
    "or set DOCTRAIL_PDFTOTEXT to the pdftotext binary. "
    "To use the bundled MuPDF instead, pass --pdf-engine pymupdf."
)


def find_pdftotext() -> Optional[str]:
    """Return the pdftotext binary: $DOCTRAIL_PDFTOTEXT, then PATH, then common install dirs."""
    configured = os.environ.get("DOCTRAIL_PDFTOTEXT")
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    found = shutil.which("pdftotext")
    if found:
        return found
    for directory in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        candidate = os.path.join(directory, "pdftotext")
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def require_pdftotext() -> str:
    """Return the pdftotext binary or raise with install instructions."""
    path = find_pdftotext()
    if not path:
        raise RuntimeError(PDFTOTEXT_INSTALL_HINT)
    return path


def normalize_ocr_engine(ocr_engine: Optional[str]) -> str:
    """Normalize OCR engine names, mapping legacy aliases to `plugin`."""
    value = (ocr_engine or "auto").strip().lower()
    if value in PLUGIN_OCR_ENGINES:
        return "plugin"
    if value in {"auto", "textra", "ocrmypdf"}:
        return value
    raise ValueError(
        f"Unknown OCR engine {ocr_engine!r}; choose auto, plugin, textra, or ocrmypdf."
    )


def _configured_plugin_path() -> Tuple[Optional[str], str]:
    primary = os.environ.get(OCR_PLUGIN_ENV)
    if primary:
        return primary, OCR_PLUGIN_ENV
    legacy = os.environ.get(LEGACY_OCR_CLIENT_ENV)
    if legacy:
        return legacy, LEGACY_OCR_CLIENT_ENV
    return None, OCR_PLUGIN_ENV


def load_ocr_plugin():
    """Load the user-supplied OCR plugin module named in DOCTRAIL_OCR_PLUGIN."""
    configured, env_name = _configured_plugin_path()
    if not configured:
        raise RuntimeError(
            f"--ocr-engine plugin needs an OCR plugin: set {OCR_PLUGIN_ENV} to a Python file "
            "that defines `async def ocr_async(file_path) -> str` (or `def ocr(file_path) -> str`)."
        )
    client_path = Path(configured).expanduser()
    if not client_path.exists():
        raise RuntimeError(f"OCR plugin not found at {client_path} ({env_name})")

    resolved = str(client_path.resolve())
    module_name = f"_doctrail_ocr_plugin_{abs(hash(resolved))}"
    with _PLUGIN_LOCK:
        existing = sys.modules.get(module_name)
        if existing is not None:
            return existing

        spec = importlib.util.spec_from_file_location(module_name, client_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load the OCR plugin from {client_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            has_async = callable(getattr(module, "ocr_async", None))
            has_sync = callable(getattr(module, "ocr", None))
            if not (has_async or has_sync):
                raise RuntimeError(
                    f"OCR plugin at {client_path} must define callable `async def ocr_async(file_path) -> str` "
                    "or `def ocr(file_path) -> str`"
                )
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module


async def ocr_with_plugin(file_path: str) -> str:
    """OCR a file with the configured plugin; its errors reach the caller."""
    module = load_ocr_plugin()
    if callable(getattr(module, "ocr_async", None)):
        result = module.ocr_async(file_path)
        text = await result if inspect.isawaitable(result) else result
    else:
        text = await asyncio.to_thread(module.ocr, file_path)
    if text is None:
        cleaned = ""
    elif not isinstance(text, str):
        raise RuntimeError(
            f"OCR plugin returned {type(text).__name__} instead of str for {file_path}"
        )
    else:
        cleaned = clean_ocr_text(text).strip()
    if not cleaned:
        raise RuntimeError(f"OCR plugin returned empty text for {file_path}")
    return cleaned


async def try_ocr_with_plugin(file_path: str) -> Optional[str]:
    """OCR a file with the configured plugin; return None on failure."""
    try:
        return await ocr_with_plugin(file_path)
    except Exception as exc:
        logger.warning(f"OCR plugin failed for {file_path}: {exc}")
        return None


def _is_macos() -> bool:
    try:
        return platform.system() == "Darwin"
    except Exception:
        return False


def _ensure_textra_on_path() -> None:
    try:
        textra_bin = Path.home() / ".textra" / "bin"
        if textra_bin.exists():
            path = os.environ.get("PATH", "")
            parts = path.split(os.pathsep) if path else []
            if str(textra_bin) not in parts:
                os.environ["PATH"] = os.pathsep.join([str(textra_bin)] + parts)
    except Exception:
        pass


def _has_textra() -> bool:
    _ensure_textra_on_path()
    return shutil.which("textra") is not None


def _try_ocr_pdf_with_textra(file_path: str, file_sha1: str) -> Optional[str]:
    """OCR a PDF via mutool + textra on macOS. Returns text or None."""
    if not _is_macos() or not _has_textra() or shutil.which("mutool") is None:
        return None

    try:
        with tempfile.TemporaryDirectory(prefix=f"doctrail-textra-{file_sha1[:8]}-") as tmp:
            temp_dir = Path(tmp)
            res = subprocess.run(
                [
                    "mutool", "convert",
                    "-O", "resolution=300",
                    "-o", str(temp_dir / "page-%d.png"),
                    str(file_path),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if res.returncode != 0:
                detail = res.stderr.strip()[-300:] or f"exit status {res.returncode}"
                _record_local_ocr_error(f"mutool convert failed: {detail}")
                logger.warning(f"mutool convert failed: {res.stderr}")
                return None
            png_files = sorted(temp_dir.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[1]))
            if not png_files:
                _record_local_ocr_error("mutool generated no page PNGs")
                logger.warning("Textra OCR: no PNGs generated from PDF")
                return None

            ocr_parts = []
            failed_pages = []
            non_empty_pages = 0
            for i, img in enumerate(png_files, 1):
                out_txt = temp_dir / f"page-{i}.txt"
                page_res = subprocess.run(
                    ["textra", str(img), "-o", str(out_txt)],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                if page_res.returncode != 0 or not out_txt.exists():
                    logger.warning(f"Textra failed for page {i}: {page_res.stderr}")
                    failed_pages.append(i)
                    continue
                text = out_txt.read_text(encoding="utf-8", errors="ignore").strip()
                if text:
                    non_empty_pages += 1
                    ocr_parts.append(f"\n\n==== Page {i} ====\n\n{text}")
            if failed_pages:
                _record_local_ocr_error(f"textra failed on page(s) {failed_pages}")
                logger.warning(
                    f"Textra OCR incomplete for {file_path}: failed page(s) {failed_pages}"
                )
                return None
            if non_empty_pages == 0:
                return None
            return "".join(ocr_parts).strip() or None
    except Exception as exc:
        _record_local_ocr_error(f"textra error: {exc}")
        logger.error(f"Textra OCR error: {exc}")
        return None


def _record_local_ocr_error(message: str) -> None:
    _LOCAL_OCR_STATE.last_error = message


def _pop_local_ocr_error() -> Optional[str]:
    message = getattr(_LOCAL_OCR_STATE, "last_error", None)
    _LOCAL_OCR_STATE.last_error = None
    return message


def _tesseract_languages() -> frozenset[str]:
    """Return installed Tesseract language codes, cached for the process."""
    global _TESSERACT_LANGS_CACHE
    if _TESSERACT_LANGS_CACHE is not None:
        return _TESSERACT_LANGS_CACHE
    with _TESSERACT_LANGS_LOCK:
        if _TESSERACT_LANGS_CACHE is not None:
            return _TESSERACT_LANGS_CACHE
        tesseract = shutil.which("tesseract")
        if not tesseract:
            _TESSERACT_LANGS_CACHE = frozenset()
            return _TESSERACT_LANGS_CACHE
        try:
            res = subprocess.run(
                [tesseract, "--list-langs"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if res.returncode == 0:
                lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
                langs = frozenset(
                    line for line in lines if " " not in line and not line.endswith(":")
                )
                _TESSERACT_LANGS_CACHE = langs
                return langs
        except Exception as exc:
            logger.debug(f"Could not inspect tesseract languages: {exc}")
        _TESSERACT_LANGS_CACHE = frozenset()
        return _TESSERACT_LANGS_CACHE


def _ocrmypdf_language_arg() -> str:
    """Use chi_sim+eng when chi_sim is installed, otherwise eng."""
    langs = _tesseract_languages()
    if "chi_sim" in langs and "eng" in langs:
        return "chi_sim+eng"
    if "chi_sim" in langs:
        return "chi_sim"
    return "eng"


def _try_ocr_pdf_with_ocrmypdf(file_path: str, file_sha1: str) -> Optional[str]:
    """OCR a PDF with ocrmypdf and extract the resulting text layer."""
    if shutil.which("ocrmypdf") is None:
        return None

    lang_arg = _ocrmypdf_language_arg()
    try:
        with tempfile.TemporaryDirectory(prefix=f"doctrail-ocrmypdf-{file_sha1[:8]}-") as tmp:
            out_pdf = Path(tmp) / "ocr.pdf"
            res = subprocess.run(
                [
                    "ocrmypdf",
                    "-l", lang_arg,
                    "--force-ocr",
                    "--output-type", "pdf",
                    str(file_path),
                    str(out_pdf),
                ],
                capture_output=True,
                text=True,
                timeout=1800,
            )
            if res.returncode != 0 or not out_pdf.exists():
                detail = res.stderr.strip()[-300:] or f"exit status {res.returncode}"
                _record_local_ocr_error(f"ocrmypdf failed: {detail}")
                logger.warning(f"ocrmypdf failed for {file_path}: {detail}")
                return None
            from . import native_extractor

            doc = native_extractor.extract_one(str(out_pdf))
            text = (doc.get("content") or "").strip()
            return text or None
    except Exception as exc:
        _record_local_ocr_error(f"ocrmypdf error: {exc}")
        logger.warning(f"ocrmypdf error for {file_path}: {exc}")
        return None


def _try_ocr_image_with_textra(file_path: str, file_sha1: str) -> Optional[str]:
    """OCR a single image via textra on macOS. Returns text or None."""
    if not _is_macos() or not _has_textra():
        return None
    try:
        with tempfile.TemporaryDirectory(prefix=f"doctrail-textra-img-{file_sha1[:8]}-") as tmp:
            out_txt = Path(tmp) / "output.txt"
            res = subprocess.run(
                ["textra", str(file_path), "-o", str(out_txt)],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if res.returncode != 0 or not out_txt.exists():
                detail = res.stderr.strip()[-300:] or f"exit status {res.returncode}"
                _record_local_ocr_error(f"textra failed: {detail}")
                return None
            text = out_txt.read_text(encoding="utf-8", errors="ignore").strip()
            return text or None
    except Exception as exc:
        _record_local_ocr_error(f"textra error: {exc}")
        logger.error(f"Textra image OCR error: {exc}")
        return None


def _try_ocr_image_with_tesseract(file_path: str) -> Optional[str]:
    """OCR a single image with tesseract. Returns text or None."""
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return None
    try:
        result = subprocess.run(
            [tesseract, str(file_path), "stdout", "-l", "eng", "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        _record_local_ocr_error(f"tesseract error: {exc}")
        logger.warning(f"Tesseract image OCR failed for {file_path}: {exc}")
        return None
    if result.returncode != 0:
        detail = result.stderr.strip()[-300:] or f"exit status {result.returncode}"
        _record_local_ocr_error(f"tesseract failed: {detail}")
        return None
    return result.stdout.strip() or None


async def run_ocr(
    file_path: str,
    file_sha1: str,
    *,
    ocr_engine: str = "auto",
    is_pdf: bool = True,
) -> Tuple[str, str, str]:
    """Run OCR on a scanned PDF or image.

    Returns ``(cleaned_text, extraction_method, ocr_engine_used)``.
    Raises RuntimeError when OCR is unavailable or fails.
    """
    engine = normalize_ocr_engine(ocr_engine)
    if engine == "plugin":
        cleaned = await ocr_with_plugin(file_path)
        return cleaned, "ocr_plugin", "plugin"

    last_error: Optional[str] = None

    def _call_local(fn, *args):
        _pop_local_ocr_error()
        text = fn(*args)
        return text, _pop_local_ocr_error()

    if is_pdf:
        if engine in {"auto", "textra"}:
            text, err = await asyncio.to_thread(_call_local, _try_ocr_pdf_with_textra, file_path, file_sha1)
            if err:
                last_error = err
            cleaned = clean_ocr_text(text).strip() if text else ""
            if cleaned:
                return cleaned, "textra_ocr", "textra"
            if engine == "textra":
                suffix = f" ({last_error})" if last_error else ""
                raise RuntimeError(f"Textra OCR produced no text for {file_path}{suffix}")
        if engine in {"auto", "ocrmypdf"}:
            text, err = await asyncio.to_thread(_call_local, _try_ocr_pdf_with_ocrmypdf, file_path, file_sha1)
            if err:
                last_error = err
            cleaned = clean_ocr_text(text).strip() if text else ""
            if cleaned:
                return cleaned, "ocrmypdf", "ocrmypdf"
            if engine == "ocrmypdf":
                suffix = f" ({last_error})" if last_error else ""
                raise RuntimeError(f"ocrmypdf produced no text for {file_path}{suffix}")
    else:
        if engine in {"auto", "textra"}:
            text, err = await asyncio.to_thread(_call_local, _try_ocr_image_with_textra, file_path, file_sha1)
            if err:
                last_error = err
            cleaned = clean_ocr_text(text).strip() if text else ""
            if cleaned:
                return cleaned, "textra_ocr", "textra"
            if engine == "textra":
                suffix = f" ({last_error})" if last_error else ""
                raise RuntimeError(f"Textra image OCR produced no text for {file_path}{suffix}")
        text, err = await asyncio.to_thread(_call_local, _try_ocr_image_with_tesseract, file_path)
        if err:
            last_error = err
        cleaned = clean_ocr_text(text).strip() if text else ""
        if cleaned:
            return cleaned, "tesseract_ocr", "tesseract"

    detail = f" Last error: {last_error}." if last_error else ""
    raise RuntimeError(
        f"No local OCR tool could read this file.{detail} Install textra/ocrmypdf/tesseract, "
        f"or pass --ocr-engine plugin with {OCR_PLUGIN_ENV} set."
    )
