"""EPUB file extraction module."""

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


def extract_text_from_epub(file_path: str) -> tuple[str, str]:
    """
    Extract text from EPUB files using epub2txt or ebook-convert.
    Returns (content, title)
    """
    logger.info(f"Attempting to extract text from EPUB: {file_path}")
    
    # First try epub2txt if available (faster and simpler)
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
                    # Try to extract title from the first lines
                    lines = content.split('\n')
                    title = lines[0] if lines else ""
                    return content, title
        except Exception as e:
            logger.warning(f"epub2txt failed: {e}")
    
    # Try ebook-convert from Calibre
    if shutil.which('ebook-convert'):
        try:
            # Create a temporary text file
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
                
                # Clean up temp file
                os.unlink(tmp_path)
                
                if content:
                    logger.info(f"Successfully extracted {len(content)} characters from EPUB using ebook-convert")
                    # Extract title from metadata if possible
                    title = ""
                    if result.stderr:
                        # ebook-convert often prints metadata to stderr
                        for line in result.stderr.split('\n'):
                            if 'Title' in line:
                                title = line.split(':', 1)[1].strip() if ':' in line else ""
                                break
                    return content, title
        except Exception as e:
            logger.warning(f"ebook-convert failed: {e}")
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    
    # If both methods fail, try to extract as ZIP (EPUB is a ZIP file)
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
    except Exception as e:
        logger.error(f"Failed to extract EPUB as ZIP: {e}")
    
    return "", ""