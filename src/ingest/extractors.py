"""
Text extraction utilities for document ingestion.

This module contains text extraction functions using specialized extractors
for different file types (PDF, DOC, EPUB, HTML, etc.).
"""

import os
import logging
from pathlib import Path
from typing import Tuple, Dict

logger = logging.getLogger(__name__)


def get_supported_file_types() -> Dict[str, str]:
    """
    Return a dictionary of supported file types and their extraction methods.
    """
    return {
        '.pdf': 'pdftotext (simple text extraction)',
        '.txt': 'direct text reading',
        '.md': 'direct text reading',
        '.doc': 'antiword',
        '.docx': 'python-docx',
        '.html': 'python-readability + BeautifulSoup',
        '.htm': 'python-readability + BeautifulSoup',
        '.mhtml': 'mhtml-to-html-py + python-readability',
        '.mht': 'mhtml-to-html-py + python-readability',
        '.epub': 'calibre ebook-convert',
        '.mobi': 'calibre ebook-convert',
        '.azw': 'calibre ebook-convert',
        '.azw3': 'calibre ebook-convert',
        '.djvu': 'djvutxt + calibre ebook-convert'
    }


def is_file_type_supported(file_path: str) -> bool:
    """
    Check if a file type is supported by the current extractors.
    """
    file_ext = Path(file_path).suffix.lower()
    return file_ext in get_supported_file_types()


# NOTE: The main extraction logic is now in ingester.py
# Each file type has its own specialized extraction method