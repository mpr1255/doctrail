"""HTML file extraction module using w3m and other tools."""

import os
import subprocess
import logging
from typing import Tuple

logger = logging.getLogger(__name__)


def extract_text_with_w3m(html_path: str) -> tuple[str, str]:
    """
    Extract text from HTML file using w3m text browser.
    Often works better than BeautifulSoup for complex encoding issues.
    
    Args:
        html_path: Path to the HTML file
        
    Returns:
        Tuple of (content, title) - title extraction is basic
    """
    try:
        logger.info(f"Attempting text extraction with w3m: {html_path}")
        
        # Create file:// URL for w3m
        if not html_path.startswith('file://'):
            # Convert to absolute path and create file URL
            abs_path = os.path.abspath(html_path)
            file_url = f"file://{abs_path}"
        else:
            file_url = html_path
        
        # Use w3m to dump text content
        cmd = [
            'w3m', '-dump',
            file_url
        ]
        
        logger.debug(f"Running w3m command: {' '.join(cmd)}")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30  # 30 second timeout
        )
        
        if result.returncode == 0 and result.stdout.strip():
            content = result.stdout.strip()
            logger.info(f"Successfully extracted {len(content)} characters from HTML using w3m")
            
            # Try to extract title from the content (w3m often puts it at the top)
            lines = content.split('\n')
            title = lines[0] if lines else ""
            
            return content, title
        else:
            logger.warning(f"w3m failed with return code {result.returncode}")
            if result.stderr:
                logger.debug(f"w3m stderr: {result.stderr}")
            return "", ""
            
    except subprocess.TimeoutExpired:
        logger.warning(f"w3m timeout for {html_path}")
        return "", ""
    except FileNotFoundError:
        logger.debug("w3m not found, skipping w3m extraction")
        return "", ""
    except Exception as e:
        logger.warning(f"w3m extraction error for {html_path}: {str(e)}")
        return "", ""