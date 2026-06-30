#!/usr/bin/env python3
"""
Check dependencies for doctrail.

All required Python packages are in pyproject.toml and installed automatically.
This module only checks for OPTIONAL system tools (OCR, rare formats).
"""

import shutil
import sys
from typing import Dict, Tuple


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
    """
    Verify core dependencies are available.

    All Python packages should be installed via pyproject.toml.
    This just does a sanity check and returns True.
    """
    if skip_requirements:
        return True

    # Quick sanity check - these should always be installed
    if not check_python_package("fitz"):  # pymupdf
        print("pymupdf not found. Try reinstalling with uv sync.", file=sys.stderr)

    return True


def check_ocr_available() -> Tuple[bool, str]:
    """
    Check if OCR is available for scanned PDFs.

    Returns:
        (available, method) - method is 'ocrmypdf', 'tesseract', or None
    """
    # Check ocrmypdf (Python package + tesseract)
    if check_python_package("ocrmypdf") and check_command_exists("tesseract"):
        return True, "ocrmypdf"

    # Check just tesseract
    if check_command_exists("tesseract"):
        return True, "tesseract"

    return False, None


def print_ocr_status() -> None:
    """Print OCR availability status."""
    available, method = check_ocr_available()

    if available:
        print(f"✓ OCR available via {method}", file=sys.stderr)
    else:
        print("OCR not configured (optional; only needed for scanned PDFs)", file=sys.stderr)
        print("   To enable: brew install tesseract tesseract-lang", file=sys.stderr)
        print("   Then: uv add 'doctrail[ocr]'", file=sys.stderr)
