#!/usr/bin/env python3
"""
Check system dependencies for doctrail.
"""

import shutil
import subprocess
import sys
from typing import Dict, List, Tuple

# Define required and optional dependencies
REQUIRED_DEPENDENCIES = {
    # No longer need Java/Tika - using specialized extractors
}

OPTIONAL_DEPENDENCIES = {
    "ocrmypdf": "PDF OCR processing",
    "pdftotext": "PDF text extraction",
    "mutool": "Alternative PDF text extraction",
    "w3m": "HTML text extraction",
    "ebook-convert": "EPUB/MOBI/DJVU ebook text extraction (part of Calibre)",
    "pandoc": "DOCX document conversion",
    "djvutxt": "DJVU document text extraction (part of djvulibre)",
}

# Chrome/Chromium is special case - check multiple possible names
CHROME_VARIANTS = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]


def check_command_exists(command: str) -> bool:
    """Check if a command exists in PATH."""
    return shutil.which(command) is not None


def check_chrome_exists() -> Tuple[bool, str]:
    """Check if any Chrome variant exists."""
    for variant in CHROME_VARIANTS:
        if check_command_exists(variant):
            return True, variant
    return False, ""


def check_java_exists() -> bool:
    """Check if Java runtime is available."""
    try:
        result = subprocess.run(
            ["java", "-version"], 
            capture_output=True, 
            text=True,
            check=False
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def check_dependencies(skip_optional: bool = False) -> Tuple[bool, List[str], List[str]]:
    """
    Check all dependencies.
    
    Returns:
        Tuple of (all_required_met, missing_required, missing_optional)
    """
    missing_required = []
    missing_optional = []
    
    # Check required dependencies
    if not check_java_exists():
        missing_required.append(f"java - {REQUIRED_DEPENDENCIES['java']}")
    
    if skip_optional:
        return len(missing_required) == 0, missing_required, []
    
    # Check optional dependencies
    for cmd, description in OPTIONAL_DEPENDENCIES.items():
        if not check_command_exists(cmd):
            missing_optional.append(f"{cmd} - {description}")
    
    # Check Chrome/Chromium
    chrome_exists, _ = check_chrome_exists()
    if not chrome_exists:
        missing_optional.append("chrome/chromium - Web page extraction")
    
    return len(missing_required) == 0, missing_required, missing_optional


def print_dependency_report(missing_required: List[str], missing_optional: List[str]) -> None:
    """Print a formatted dependency report."""
    if missing_required:
        print("\n❌ Missing REQUIRED dependencies:", file=sys.stderr)
        for dep in missing_required:
            print(f"  - {dep}", file=sys.stderr)
        print("\nPlease install the required dependencies before continuing.", file=sys.stderr)
        print("Use --skip-requirements to bypass this check (not recommended).", file=sys.stderr)
    
    if missing_optional:
        print("\n⚠️  Missing OPTIONAL dependencies:", file=sys.stderr)
        for dep in missing_optional:
            print(f"  - {dep}", file=sys.stderr)
        print("\nSome features may not work without these dependencies.", file=sys.stderr)


def verify_dependencies(skip_requirements: bool = False) -> bool:
    """
    Verify all dependencies are available.
    
    Returns:
        True if all required dependencies are met or skip_requirements is True
    """
    if skip_requirements:
        return True
    
    all_required_met, missing_required, missing_optional = check_dependencies()
    
    if not all_required_met or missing_optional:
        print_dependency_report(missing_required, missing_optional)
    
    return all_required_met