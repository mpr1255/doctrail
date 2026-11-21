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
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

from .text_processing import clean_ocr_text

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

    module_name = f"_doctrail_ocr_plugin_{abs(hash(str(client_path.resolve())))}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(module_name, client_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the OCR plugin from {client_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    if not (hasattr(module, "ocr_async") or hasattr(module, "ocr")):
        raise RuntimeError(
            f"OCR plugin at {client_path} must define `async def ocr_async(file_path) -> str` "
            "or `def ocr(file_path) -> str`"
        )
    return module


async def ocr_with_plugin(file_path: str) -> str:
    """OCR a file with the configured plugin; its errors reach the caller."""
    module = load_ocr_plugin()
    if hasattr(module, "ocr_async"):
        result = module.ocr_async(file_path)
        text = await result if inspect.isawaitable(result) else result
    else:
        text = await asyncio.to_thread(module.ocr, file_path)
    cleaned = (text or "").strip()
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

    temp_dir = Path(f"/tmp/doctrail-textra-{file_sha1}")
    try:
        temp_dir.mkdir(exist_ok=True, parents=True)
        res = subprocess.run(
            [
                "mutool", "convert",
                "-O", "resolution=300",
                "-o", str(temp_dir / "page-%d.png"),
                str(file_path),
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            logger.warning(f"mutool convert failed: {res.stderr}")
            return None
        png_files = sorted(temp_dir.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[1]))
        if not png_files:
            logger.warning("Textra OCR: no PNGs generated from PDF")
            return None

        ocr_parts = []
        for i, img in enumerate(png_files, 1):
            out_txt = temp_dir / f"page-{i}.txt"
            page_res = subprocess.run(
                ["textra", str(img), "-o", str(out_txt)],
                capture_output=True,
                text=True,
            )
            if page_res.returncode != 0 or not out_txt.exists():
                logger.warning(f"Textra failed for page {i}: {page_res.stderr}")
                continue
            text = out_txt.read_text(encoding="utf-8", errors="ignore").strip()
            ocr_parts.append(f"\n\n==== Page {i} ====\n\n{text}")
        joined = "".join(ocr_parts).strip()
        return joined or None
    except Exception as exc:
        logger.error(f"Textra OCR error: {exc}")
        return None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _try_ocr_pdf_with_ocrmypdf(file_path: str, file_sha1: str) -> Optional[str]:
    """OCR a PDF with ocrmypdf and extract the resulting text layer."""
    if shutil.which("ocrmypdf") is None:
        return None

    temp_dir = Path(f"/tmp/doctrail-ocrmypdf-{file_sha1}")
    try:
        temp_dir.mkdir(exist_ok=True, parents=True)
        out_pdf = temp_dir / "ocr.pdf"
        res = subprocess.run(
            [
                "ocrmypdf",
                "-l", "chi_sim+eng",
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
            logger.warning(f"ocrmypdf failed for {file_path}: {res.stderr.strip()[-300:]}")
            return None
        from . import native_extractor

        doc = native_extractor.extract_one(str(out_pdf))
        text = (doc.get("content") or "").strip()
        return text or None
    except Exception as exc:
        logger.warning(f"ocrmypdf error for {file_path}: {exc}")
        return None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _try_ocr_image_with_textra(file_path: str, file_sha1: str) -> Optional[str]:
    """OCR a single image via textra on macOS. Returns text or None."""
    if not _is_macos() or not _has_textra():
        return None
    out_dir = Path(f"/tmp/doctrail-textra-img-{file_sha1}")
    try:
        out_dir.mkdir(exist_ok=True, parents=True)
        out_txt = out_dir / "output.txt"
        res = subprocess.run(
            ["textra", str(file_path), "-o", str(out_txt)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if res.returncode != 0 or not out_txt.exists():
            return None
        text = out_txt.read_text(encoding="utf-8", errors="ignore").strip()
        return text or None
    except Exception as exc:
        logger.error(f"Textra image OCR error: {exc}")
        return None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


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
        logger.warning(f"Tesseract image OCR failed for {file_path}: {exc}")
        return None
    if result.returncode != 0:
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
        raw = await ocr_with_plugin(file_path)
        return clean_ocr_text(raw), "ocr_plugin", "plugin"

    if is_pdf:
        if engine in {"auto", "textra"}:
            text = await asyncio.to_thread(_try_ocr_pdf_with_textra, file_path, file_sha1)
            if text:
                return clean_ocr_text(text), "textra_ocr", "textra"
            if engine == "textra":
                raise RuntimeError(f"Textra OCR produced no text for {file_path}")
        if engine in {"auto", "ocrmypdf"}:
            text = await asyncio.to_thread(_try_ocr_pdf_with_ocrmypdf, file_path, file_sha1)
            if text:
                return clean_ocr_text(text), "ocrmypdf", "ocrmypdf"
            if engine == "ocrmypdf":
                raise RuntimeError(f"ocrmypdf produced no text for {file_path}")
    else:
        if engine in {"auto", "textra"}:
            text = await asyncio.to_thread(_try_ocr_image_with_textra, file_path, file_sha1)
            if text:
                return clean_ocr_text(text), "textra_ocr", "textra"
            if engine == "textra":
                raise RuntimeError(f"Textra image OCR produced no text for {file_path}")
        text = await asyncio.to_thread(_try_ocr_image_with_tesseract, file_path)
        if text:
            return clean_ocr_text(text), "tesseract_ocr", "tesseract"

    raise RuntimeError(
        "No local OCR tool could read this file. Install textra/ocrmypdf/tesseract, "
        f"or pass --ocr-engine plugin with {OCR_PLUGIN_ENV} set."
    )
