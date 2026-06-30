"""EPUB file extraction module.

Extraction priority:
1. ebooklib (Python, no system deps) - PRIMARY
2. ZIP extraction with BeautifulSoup (Python fallback)
3. epub2txt (system tool) - FALLBACK
4. ebook-convert/Calibre (system tool) - FALLBACK
"""

import os
import subprocess
import tempfile
import shutil
import logging
import zipfile
import re
from typing import Tuple
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Track if we've warned about missing ebooklib
_ebooklib_warning_shown = False


def _extract_with_ebooklib(file_path: str) -> tuple[str, str]:
    """
    Extract text from EPUB using ebooklib (pure Python).
    PRIMARY extraction method.
    """
    global _ebooklib_warning_shown
    try:
        import ebooklib
        from ebooklib import epub

        logger.info(f"Extracting EPUB with ebooklib: {file_path}")

        book = epub.read_epub(file_path, options={'ignore_ncx': True})

        # Get title from metadata
        title = ""
        try:
            title_meta = book.get_metadata('DC', 'title')
            if title_meta:
                title = title_meta[0][0]
        except Exception:
            pass

        # Extract text from all document items
        content_parts = []
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                html_content = item.get_content().decode('utf-8', errors='ignore')
                soup = BeautifulSoup(html_content, 'html.parser')
                text = soup.get_text(separator='\n', strip=True)
                if text:
                    content_parts.append(text)

        content = '\n\n'.join(content_parts)
        if content:
            logger.info(f"Successfully extracted {len(content)} characters from EPUB using ebooklib")
            return content, title

        return "", ""

    except ImportError:
        if not _ebooklib_warning_shown:
            logger.debug("ebooklib not installed. Install with: uv add ebooklib")
            _ebooklib_warning_shown = True
        return "", ""
    except Exception as e:
        logger.warning(f"ebooklib extraction failed for {file_path}: {str(e)}")
        return "", ""


def _extract_with_zip(file_path: str) -> tuple[str, str]:
    """
    Extract text from EPUB by treating it as a ZIP file.
    Python fallback when ebooklib isn't available.
    """
    try:
        content_parts = []
        title = ""

        with zipfile.ZipFile(file_path, 'r') as epub:
            # Look for content.opf to get metadata
            for name in epub.namelist():
                if name.endswith('.opf'):
                    with epub.open(name) as opf_file:
                        opf_content = opf_file.read().decode('utf-8', errors='ignore')
                        # Simple regex to extract title from OPF
                        title_match = re.search(r'<dc:title>([^<]+)</dc:title>', opf_content)
                        if not title_match:
                            title_match = re.search(r'<title>([^<]+)</title>', opf_content)
                        if title_match:
                            title = title_match.group(1).strip()
                        break

            # Extract text from HTML/XHTML files
            for name in sorted(epub.namelist()):
                if name.endswith(('.html', '.xhtml', '.htm')):
                    with epub.open(name) as html_file:
                        html_content = html_file.read().decode('utf-8', errors='ignore')
                        soup = BeautifulSoup(html_content, 'html.parser')
                        text = soup.get_text(separator='\n', strip=True)
                        if text:
                            content_parts.append(text)

        content = '\n\n'.join(content_parts)
        if content:
            logger.info(f"Successfully extracted {len(content)} characters from EPUB using ZIP extraction")
            return content, title

        return "", ""

    except Exception as e:
        logger.warning(f"ZIP extraction failed for EPUB {file_path}: {e}")
        return "", ""


def extract_text_from_epub(file_path: str) -> tuple[str, str]:
    """
    Extract text from EPUB files.
    Returns (content, title)

    Tries in order:
    1. ebooklib (Python)
    2. ZIP extraction (Python fallback)
    3. epub2txt (system tool)
    4. ebook-convert (system tool, Calibre)
    """
    logger.info(f"Attempting to extract text from EPUB: {file_path}")

    # 1. Try ebooklib first (pure Python, best quality)
    content, title = _extract_with_ebooklib(file_path)
    if content:
        return content, title

    # 2. Try ZIP extraction (pure Python fallback)
    content, title = _extract_with_zip(file_path)
    if content:
        return content, title

    # 3. Fallback to epub2txt if available (system tool)
    if shutil.which('epub2txt'):
        try:
            result = subprocess.run(
                ['epub2txt', file_path],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0 and result.stdout:
                content = result.stdout.strip()
                if content:
                    logger.info(f"Successfully extracted {len(content)} characters from EPUB using epub2txt")
                    lines = content.split('\n')
                    title = lines[0] if lines else ""
                    return content, title
        except Exception as e:
            logger.warning(f"epub2txt failed: {e}")

    # 4. Fallback to ebook-convert from Calibre (system tool)
    if shutil.which('ebook-convert'):
        try:
            with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as tmp:
                tmp_path = tmp.name

            result = subprocess.run(
                ['ebook-convert', file_path, tmp_path, '--txt-output-encoding=utf-8'],
                capture_output=True,
                text=True,
                timeout=120
            )

            if result.returncode == 0 and os.path.exists(tmp_path):
                with open(tmp_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()

                os.unlink(tmp_path)

                if content:
                    logger.info(f"Successfully extracted {len(content)} characters from EPUB using ebook-convert")
                    title = ""
                    if result.stderr:
                        for line in result.stderr.split('\n'):
                            if 'Title' in line:
                                title = line.split(':', 1)[1].strip() if ':' in line else ""
                                break
                    return content, title
        except Exception as e:
            logger.warning(f"ebook-convert failed: {e}")
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    logger.error(f"All EPUB extraction methods failed for: {file_path}")
    return "", ""