"""DJVU file extraction module."""

import os
import subprocess
import tempfile
import shutil
import logging
from typing import Tuple

logger = logging.getLogger(__name__)


def extract_text_from_djvu(file_path: str) -> tuple[str, str]:
    """
    Extract text from DJVU files using djvutxt.
    Returns (content, title)
    """
    logger.info(f"Attempting to extract text from DJVU: {file_path}")
    
    # DJVU files require djvutxt (part of djvulibre package)
    if shutil.which('djvutxt'):
        try:
            result = subprocess.run(
                ['djvutxt', file_path],
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode == 0 and result.stdout:
                content = result.stdout.strip()
                if content:
                    logger.info(f"Successfully extracted {len(content)} characters from DJVU using djvutxt")
                    # Extract title from first line or filename
                    lines = content.split('\n')
                    title = lines[0] if lines else os.path.splitext(os.path.basename(file_path))[0]
                    return content, title
        except Exception as e:
            logger.warning(f"djvutxt failed: {e}")
    else:
        logger.warning("djvutxt not found. Install djvulibre to process DJVU files.")
    
    # Try ebook-convert as fallback
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
                    logger.info(f"Successfully extracted {len(content)} characters from DJVU using ebook-convert")
                    title = os.path.splitext(os.path.basename(file_path))[0]
                    return content, title
        except Exception as e:
            logger.warning(f"ebook-convert failed for DJVU: {e}")
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    
    return "", ""