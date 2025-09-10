"""MOBI file extraction module."""

import os
import subprocess
import tempfile
import shutil
import logging
from typing import Tuple

logger = logging.getLogger(__name__)


def extract_text_from_mobi(file_path: str) -> tuple[str, str]:
    """
    Extract text from MOBI files using ebook-convert.
    Returns (content, title)
    """
    logger.info(f"Attempting to extract text from MOBI: {file_path}")
    
    # MOBI files typically require ebook-convert from Calibre
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
                    logger.info(f"Successfully extracted {len(content)} characters from MOBI using ebook-convert")
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
            logger.warning(f"ebook-convert failed for MOBI: {e}")
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    else:
        logger.warning("ebook-convert not found. Install Calibre to process MOBI files.")
    
    return "", ""