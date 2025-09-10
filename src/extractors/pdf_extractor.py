"""PDF file extraction module with OCR support."""

import os
import subprocess
import tempfile
import logging
from typing import Tuple, Optional

logger = logging.getLogger(__name__)


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
        logger.error("ocrmypdf not found. Please install it: pip install ocrmypdf")
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
        else:
            logger.warning(f"mutool failed with return code {result.returncode}")
            if result.stderr:
                logger.debug(f"mutool stderr: {result.stderr}")
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