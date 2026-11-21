#!/usr/bin/env python3
"""Check core and optional extraction dependencies for Doctrail."""

import shutil
import sys
from typing import Optional, Tuple


def check_python_package(import_name: str) -> bool:
    """Check if a Python package is importable."""
    try:
        __import__(import_name)
        return True
    except ImportError:
        return False


def check_command_exists(command: str) -> bool:
    """Check if a command exists in PATH."""
    return shutil.which(command) is not None


def verify_dependencies(skip_requirements: bool = False) -> bool:
    """Verify core extraction extension is importable."""
    if skip_requirements:
        return True

    if not check_python_package("doctrail._ingest_native"):
        print(
            "doctrail native extractor not found. Reinstall doctrail or run `make native`.",
            file=sys.stderr,
        )

    return True


def check_ocr_available() -> Tuple[bool, Optional[str]]:
    """Check whether a local OCR tool is available for scanned files."""
    if check_command_exists("textra"):
        return True, "textra"
    if check_command_exists("ocrmypdf"):
        return True, "ocrmypdf"
    if check_command_exists("tesseract"):
        return True, "tesseract"
    return False, None


def print_ocr_status() -> None:
    """Print local OCR availability status."""
    available, method = check_ocr_available()
    if available:
        print(f"OCR available via {method}", file=sys.stderr)
    else:
        print("No local OCR tool detected (only needed for scanned PDFs and images)", file=sys.stderr)
        print("   Install textra / ocrmypdf / tesseract, or set DOCTRAIL_OCR_PLUGIN.", file=sys.stderr)
