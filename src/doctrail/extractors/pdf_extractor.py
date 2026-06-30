"""PDF file extraction module with OCR support.

Extraction priority:
1. pymupdf (Python, no system deps) - PRIMARY
2. pdftotext (system tool) - FALLBACK
3. mutool (system tool) - FALLBACK
4. OCR via ocrmypdf (requires tesseract) - LAST RESORT
"""

import os
import subprocess
import tempfile
import logging
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

# Track if we've warned about missing pymupdf
_pymupdf_warning_shown = False


def extract_text_with_pymupdf(pdf_path: str) -> str:
    """
    Extract text from PDF using pymupdf (fitz).
    This is the PRIMARY extraction method - pure Python, no system dependencies.

    Args:
        pdf_path: Path to the PDF file

    Returns:
        Extracted text content, or empty string if extraction fails
    """
    global _pymupdf_warning_shown
    try:
        import fitz  # pymupdf

        logger.info(f"Extracting text with pymupdf: {pdf_path}")

        doc = fitz.open(pdf_path)
        text_parts = []

        for page_num, page in enumerate(doc):
            text = page.get_text()
            if text.strip():
                text_parts.append(text)

        doc.close()

        content = "\n".join(text_parts).strip()
        if content:
            logger.info(f"Successfully extracted {len(content)} characters from PDF using pymupdf")
        return content

    except ImportError:
        if not _pymupdf_warning_shown:
            logger.warning("pymupdf not installed. Install with: uv add pymupdf")
            _pymupdf_warning_shown = True
        return ""
    except Exception as e:
        logger.warning(f"pymupdf extraction failed for {pdf_path}: {str(e)}")
        return ""


def extract_text_with_pdftotext(pdf_path: str) -> str:
    """
    Extract text from PDF using pdftotext (system tool).
    FALLBACK method when pymupdf fails or isn't available.

    Args:
        pdf_path: Path to the PDF file

    Returns:
        Extracted text content, or empty string if extraction fails
    """
    try:
        logger.info(f"Attempting text extraction with pdftotext: {pdf_path}")

        result = subprocess.run(
            ['pdftotext', pdf_path, '-'],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0 and result.stdout.strip():
            content = result.stdout.strip()
            logger.info(f"Successfully extracted {len(content)} characters from PDF using pdftotext")
            return content
        if result.returncode != 0:
            logger.warning(f"pdftotext failed with return code {result.returncode}")
        else:
            logger.debug(f"pdftotext returned no text for {pdf_path}")
        return ""

    except FileNotFoundError:
        logger.debug("pdftotext not found (system tool not installed)")
        return ""
    except subprocess.TimeoutExpired:
        logger.warning(f"pdftotext timeout for {pdf_path}")
        return ""
    except Exception as e:
        logger.warning(f"pdftotext extraction error for {pdf_path}: {str(e)}")
        return ""


def get_ocr_pdf_path(pdf_path: str) -> str:
    """
    Get the path where the OCR'd version of a PDF should be stored.
    Uses the new naming convention: filename--OCR.pdf
    """
    dir_path = os.path.dirname(pdf_path)
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    return os.path.join(dir_path, f"{base_name}--OCR.pdf")


def check_for_existing_ocr_pdf(pdf_path: str) -> Optional[str]:
    """
    Check if an OCR'd version of the PDF already exists using both old and new naming conventions.
    
    Returns:
        Path to the OCR'd PDF if it exists, None otherwise
    """
    # Check new naming convention first: filename--OCR.pdf
    new_ocr_path = get_ocr_pdf_path(pdf_path)
    if os.path.exists(new_ocr_path):
        logger.info(f"Found existing OCR'd PDF (new format): {new_ocr_path}")
        return new_ocr_path
    
    # Check old naming convention for backwards compatibility: filename_ocr.pdf
    dir_path = os.path.dirname(pdf_path)
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    old_ocr_path = os.path.join(dir_path, f"{base_name}_ocr.pdf")
    if os.path.exists(old_ocr_path):
        logger.info(f"Found existing OCR'd PDF (old format): {old_ocr_path}")
        return old_ocr_path
    
    return None


def ocr_pdf_with_ocrmypdf(pdf_path: str, output_dir: str = None) -> str:
    """
    Use ocrmypdf to OCR a PDF file and return the path to the OCR'd PDF.
    
    Args:
        pdf_path: Path to the original PDF
        output_dir: Directory to save OCR'd PDF (defaults to same directory as original)
        
    Returns:
        Path to the OCR'd PDF file
    """
    import tempfile
    
    # Use the same directory as the original PDF by default (for persistent caching)
    if output_dir is None:
        output_dir = os.path.dirname(pdf_path)
    
    # Create output filename using our new naming convention
    ocr_pdf_path = get_ocr_pdf_path(pdf_path)
    
    # If we're using a custom output_dir, adjust the path
    if output_dir != os.path.dirname(pdf_path):
        pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
        ocr_pdf_path = os.path.join(output_dir, f"{pdf_name}--OCR.pdf")
    
    try:
        # Run ocrmypdf with Chinese language support
        cmd = [
            'ocrmypdf',
            '-l', 'chi_sim+eng',  # Chinese simplified + English
            '--force-ocr',        # Force OCR even if text already exists
            '--output-type', 'pdf',
            pdf_path,
            ocr_pdf_path
        ]
        
        logger.info(f"Running OCR on PDF: {pdf_path}")
        logger.debug(f"OCR command: {' '.join(cmd)}")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800  # 30 minute timeout for OCR (large books need time)
        )
        
        if result.returncode == 0:
            logger.info(f"OCR completed successfully: {ocr_pdf_path}")
            return ocr_pdf_path
        else:
            logger.error(f"OCR failed with return code {result.returncode}")
            logger.error(f"OCR stderr: {result.stderr}")
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
            
    except subprocess.TimeoutExpired:
        logger.error(f"OCR timeout for {pdf_path}")
        raise
    except FileNotFoundError:
        logger.error("ocrmypdf not found. Install it with uv, for example: uv sync --extra ocr")
        raise
    except Exception as e:
        logger.error(f"OCR error for {pdf_path}: {str(e)}")
        raise


def extract_text_with_mutool(pdf_path: str) -> str:
    """
    Extract text from PDF using mutool draw command.
    Often works better than pdftotext for problematic PDFs.
    
    Args:
        pdf_path: Path to the PDF file
        
    Returns:
        Extracted text content
    """
    try:
        logger.info(f"Attempting text extraction with mutool: {pdf_path}")
        
        # Use mutool draw to extract text to stdout
        cmd = [
            'mutool', 'draw',
            '-F', 'text',  # Output format: text
            '-o', '-',     # Output to stdout
            pdf_path
        ]
        
        logger.debug(f"Running mutool command: {' '.join(cmd)}")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60  # 1 minute timeout
        )
        
        if result.returncode == 0 and result.stdout.strip():
            content = result.stdout.strip()
            logger.info(f"Successfully extracted {len(content)} characters from PDF using mutool")
            return content
        if result.returncode != 0:
            logger.warning(f"mutool failed with return code {result.returncode}")
            if result.stderr:
                logger.debug(f"mutool stderr: {result.stderr}")
        else:
            logger.debug(f"mutool returned no text for {pdf_path}")
        return ""
            
    except subprocess.TimeoutExpired:
        logger.warning(f"mutool timeout for {pdf_path}")
        return ""
    except FileNotFoundError:
        logger.debug("mutool not found, skipping mutool extraction")
        return ""
    except Exception as e:
        logger.warning(f"mutool extraction error for {pdf_path}: {str(e)}")
        return ""
