"""Extraction helpers for legacy Microsoft Word (.doc) and RTF files."""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _resolve_antiword() -> Optional[str]:
    """Return the antiword binary path if available."""
    return shutil.which("antiword")


def extract_text_from_rtf(file_path: str) -> str:
    """Extract text from an RTF file using textutil (macOS) or unrtf."""
    textutil = shutil.which("textutil")
    if textutil:
        cmd = [textutil, "-convert", "txt", "-stdout", file_path]
        logger.debug("Running textutil: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout

    unrtf = shutil.which("unrtf")
    if unrtf:
        cmd = [unrtf, "--text", file_path]
        logger.debug("Running unrtf: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout

    raise RuntimeError(
        "RTF extraction failed. Install textutil (macOS built-in) or unrtf."
    )


def extract_text_from_doc(file_path: str) -> str:
    """Extract text from a .doc file. Tries antiword first, falls back to RTF extraction."""
    antiword = _resolve_antiword()
    if not antiword:
        raise RuntimeError("antiword is not installed. Install antiword to ingest .doc files.")

    if not Path(file_path).is_file():
        raise RuntimeError(f"File not found: {file_path}")

    cmd = [antiword, "-mUTF-8", "-w", "0", file_path]
    logger.debug("Running antiword: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        return result.stdout

    # antiword often fails on RTF files disguised as .doc
    stderr = result.stderr.strip()
    if "Rich Text Format" in stderr or "not a Word Document" in stderr:
        logger.info(f"File is RTF disguised as .doc, trying RTF extraction: {file_path}")
        return extract_text_from_rtf(file_path)

    raise RuntimeError(f"antiword failed (exit {result.returncode}): {stderr}")
