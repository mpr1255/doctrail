"""
Document processing module for Doctrail ingestion.

This module contains the main document processing logic that coordinates
extraction from various file types.
"""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Tuple, Dict, Optional
import chardet
from bs4 import BeautifulSoup
from readability import Document
from loguru import logger

# Import from extractors
from ..extractors.mhtml_extractor import (
    extract_mhtml_metadata, process_mhtml_to_html, process_mhtml_to_html_python,
    extract_with_chrome_headless
)
from ..extractors.pdf_extractor import (
    check_for_existing_ocr_pdf, ocr_pdf_with_ocrmypdf, extract_text_with_mutool
)
from ..extractors.html_extractor import extract_text_with_w3m
from ..extractors.smart_html_extractor import extract_html_text_smart
from ..extractors.epub_extractor import extract_text_from_epub
from ..extractors.mobi_extractor import extract_text_from_mobi
from ..extractors.docx_extractor import extract_text_from_docx
from ..extractors.djvu_extractor import extract_text_from_djvu

# Import from text processing
from .text_processing import (
    add_page_markers, clean_extracted_text, is_text_garbage, 
    is_content_garbage, clean_ocr_text
)
from ..file_filters import (
    should_skip_file, get_unsupported_file_error, check_for_manual_override
)

# Custom exception for skipped files
class SkippedFileException(Exception):
    """Exception raised when a file is intentionally skipped"""
    pass


async def process_document(file_path: str, file_sha1: str, use_readability: bool = False, html_extractor: str = 'default', skip_garbage_check: bool = False) -> Tuple[str, str, Dict]:
    """Process a document using specialized extractors and return (sha1, content, metadata)"""
    # Ensure proper UTF-8 encoding for Python I/O
    os.environ['PYTHONIOENCODING'] = 'utf8'
    
    # Check for manual override files first
    override_file = check_for_manual_override(file_path)
    if override_file:
        try:
            with open(override_file, 'r', encoding='utf-8') as f:
                override_content = f.read().strip()
            
            if len(override_content) > 50:  # Sanity check
                logger.info(f"Using manual override content from {override_file} ({len(override_content)} characters)")
                
                # Create metadata indicating manual override
                metadata = {
                    'title': Path(file_path).stem,
                    'original_file_path': file_path,
                    'override_file_path': override_file,
                    'original_file_type': Path(file_path).suffix.lower().lstrip('.'),
                    'Content-Type': 'text/plain',
                    'resourceName': Path(file_path).name,
                    'extraction_method': 'manual_override',
                    'processing_method': f'manual_override_{Path(override_file).stem.split("--")[-1]}'
                }
                
                return file_sha1, override_content, metadata
        except Exception as e:
            logger.warning(f"Failed to read manual override file {override_file}: {e}")
            # Continue with normal processing
    
    logger.info(f"Processing {file_path}")
    
    # Skip files that should be ignored
    if should_skip_file(file_path):
        logger.info(f"Skipping file: {file_path}")
        raise SkippedFileException("File should be skipped")
    
    original_file_path = file_path
    temp_html_file = None  # Initialize this variable at the start

    # Handle plain text files directly (TXT, MD)
    file_extension = Path(file_path).suffix.lower()
    if file_extension in ['.txt', '.md']:
        return await _process_text_file(file_path, file_sha1, original_file_path, file_extension)

    # Handle PDF files
    if file_extension == '.pdf':
        return await _process_pdf_file(file_path, file_sha1, original_file_path)

    # Handle EPUB files
    if file_extension == '.epub':
        return await _process_epub_file(file_path, file_sha1, original_file_path)

    # Handle MOBI files
    if file_extension == '.mobi':
        return await _process_mobi_file(file_path, file_sha1, original_file_path)

    # Handle DOCX files
    if file_extension == '.docx':
        return await _process_docx_file(file_path, file_sha1, original_file_path)

    # Handle DJVU files
    if file_extension == '.djvu':
        return await _process_djvu_file(file_path, file_sha1, original_file_path)

    # Handle MHTML files
    if file_extension in ['.mhtml', '.mht']:
        # Convert to HTML first, then process as HTML
        file_path, file_extension, mhtml_metadata = await _convert_mhtml_to_html(file_path, original_file_path)
        temp_html_file = file_path  # Track for cleanup
    else:
        mhtml_metadata = None

    # Handle HTML files (including converted MHTML)
    if file_extension in ['.html', '.htm']:
        result = await _process_html_file(file_path, file_sha1, original_file_path, use_readability, mhtml_metadata, html_extractor, skip_garbage_check)
        
        # Clean up temporary HTML file if it was created
        if temp_html_file and os.path.exists(temp_html_file):
            try:
                os.unlink(temp_html_file)
                logger.debug(f"Cleaned up temporary HTML file: {temp_html_file}")
            except Exception as e:
                logger.warning(f"Failed to clean up temporary HTML file {temp_html_file}: {e}")
        
        return result

    # If we get here, the file type is not supported
    raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_text_file(file_path: str, file_sha1: str, original_file_path: str, file_extension: str) -> Tuple[str, str, Dict]:
    """Process plain text files (TXT, MD)"""
    try:
        logger.info(f"Processing text file directly: {file_path}")
        
        # Read the file with encoding detection
        with open(file_path, 'rb') as f:
            raw_data = f.read()
        
        # Detect encoding
        result = chardet.detect(raw_data)
        encoding = result.get('encoding', 'utf-8')
        
        # Read as text
        content = raw_data.decode(encoding, errors='ignore').strip()
        
        if content:
            logger.info(f"Successfully read {len(content)} characters from text file")
            
            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': file_extension.lstrip('.'),
                'Content-Type': 'text/plain' if file_extension == '.txt' else 'text/markdown',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': 'direct_text_read',
                'encoding': encoding
            }
            
            # Clean up the extracted text
            content = clean_extracted_text(content)
            
            return file_sha1, content, metadata
        else:
            logger.warning(f"Text file is empty: {file_path}")
            raise ValueError(get_unsupported_file_error(file_path))
            
    except Exception as e:
        logger.error(f"Error reading text file {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(file_path))


async def _process_pdf_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process PDF files with multiple extraction methods"""
    try:
        logger.info(f"Processing PDF file: {file_path}")
        
        # First check if an OCR'd version already exists
        existing_ocr_pdf = check_for_existing_ocr_pdf(file_path)
        if existing_ocr_pdf:
            logger.info(f"Using existing OCR'd PDF: {existing_ocr_pdf}")
            file_path = existing_ocr_pdf
            metadata_update = {'ocr_applied': True, 'ocr_file_path': existing_ocr_pdf}
        else:
            metadata_update = {}
        
        # Try pdftotext first
        result = subprocess.run(
            ['pdftotext', file_path, '-'],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode == 0 and result.stdout.strip():
            content = result.stdout.strip()
            
            # Add page markers if form feeds are present
            content = add_page_markers(content)
            
            # Check if text looks like garbage (encoding issues, etc.)
            if is_text_garbage(content):
                logger.warning(f"PDF text appears to be garbage, trying alternative methods...")
                # Try mutool as alternative
                mutool_content = extract_text_with_mutool(file_path)
                if mutool_content and not is_text_garbage(mutool_content):
                    content = mutool_content
                    extraction_method = 'mutool'
                else:
                    # If still garbage, try OCR
                    logger.info("PDF text extraction failed, attempting OCR...")
                    try:
                        ocr_pdf_path = ocr_pdf_with_ocrmypdf(file_path)
                        # Try extracting from OCR'd PDF
                        result = subprocess.run(
                            ['pdftotext', ocr_pdf_path, '-'],
                            capture_output=True,
                            text=True,
                            timeout=60
                        )
                        if result.returncode == 0 and result.stdout.strip():
                            content = result.stdout.strip()
                            content = clean_ocr_text(content)
                            extraction_method = 'ocrmypdf'
                            metadata_update['ocr_applied'] = True
                            metadata_update['ocr_file_path'] = ocr_pdf_path
                        else:
                            raise ValueError("OCR extraction failed")
                    except Exception as ocr_e:
                        logger.error(f"OCR failed: {ocr_e}")
                        content = content  # Use original garbage content
                        extraction_method = 'pdftotext_with_issues'
                        metadata_update['text_quality_issue'] = 'garbage_text_detected'
            else:
                extraction_method = 'pdftotext'
        else:
            # pdftotext failed, try alternatives
            logger.warning(f"pdftotext failed for {file_path}, trying mutool...")
            mutool_content = extract_text_with_mutool(file_path)
            
            if mutool_content:
                content = mutool_content
                extraction_method = 'mutool'
            else:
                # Last resort: OCR
                logger.info("All PDF text extraction methods failed, attempting OCR...")
                try:
                    ocr_pdf_path = ocr_pdf_with_ocrmypdf(file_path)
                    result = subprocess.run(
                        ['pdftotext', ocr_pdf_path, '-'],
                        capture_output=True,
                        text=True,
                        timeout=60
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        content = clean_ocr_text(result.stdout.strip())
                        extraction_method = 'ocrmypdf'
                        metadata_update['ocr_applied'] = True
                        metadata_update['ocr_file_path'] = ocr_pdf_path
                    else:
                        raise ValueError("All PDF extraction methods failed")
                except Exception as ocr_e:
                    logger.error(f"OCR failed: {ocr_e}")
                    metadata_update['ocr_attempted'] = True
                    metadata_update['ocr_failed'] = str(ocr_e)
                    raise ValueError(get_unsupported_file_error(original_file_path))
        
        # Clean up the text
        content = clean_extracted_text(content)
        
        # Build metadata
        metadata = {
            'original_file_path': original_file_path,
            'original_file_type': 'pdf',
            'Content-Type': 'application/pdf',
            'resourceName': os.path.basename(original_file_path),
            'extraction_method': extraction_method
        }
        metadata.update(metadata_update)
        
        logger.info(f"Successfully extracted {len(content)} characters from PDF using {extraction_method}")
        return file_sha1, content, metadata
        
    except subprocess.TimeoutExpired:
        logger.error(f"PDF processing timed out for {file_path}")
        raise ValueError(get_unsupported_file_error(original_file_path))
    except Exception as e:
        logger.error(f"Error processing PDF {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_epub_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process EPUB files"""
    try:
        logger.info(f"Processing EPUB file: {file_path}")
        content = extract_text_from_epub(file_path)
        
        if content:
            # Clean up the extracted text
            content = clean_extracted_text(content)
            
            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'epub',
                'Content-Type': 'application/epub+zip',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': 'epub_direct'
            }
            
            logger.info(f"Successfully extracted {len(content)} characters from EPUB")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from EPUB")
            
    except Exception as e:
        logger.error(f"Error processing EPUB {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_mobi_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process MOBI files"""
    try:
        logger.info(f"Processing MOBI file: {file_path}")
        content = extract_text_from_mobi(file_path)
        
        if content:
            # Clean up the extracted text
            content = clean_extracted_text(content)
            
            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'mobi',
                'Content-Type': 'application/x-mobipocket-ebook',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': 'ebook_convert'
            }
            
            logger.info(f"Successfully extracted {len(content)} characters from MOBI")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from MOBI")
            
    except Exception as e:
        logger.error(f"Error processing MOBI {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_docx_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process DOCX files"""
    try:
        logger.info(f"Processing DOCX file: {file_path}")
        content = extract_text_from_docx(file_path)
        
        if content:
            # Clean up the extracted text
            content = clean_extracted_text(content)
            
            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'docx',
                'Content-Type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': 'python_docx'
            }
            
            logger.info(f"Successfully extracted {len(content)} characters from DOCX")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from DOCX")
            
    except Exception as e:
        logger.error(f"Error processing DOCX {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_djvu_file(file_path: str, file_sha1: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Process DJVU files"""
    try:
        logger.info(f"Processing DJVU file: {file_path}")
        content = extract_text_from_djvu(file_path)
        
        if content:
            # Clean up the extracted text
            content = clean_extracted_text(content)
            
            metadata = {
                'original_file_path': original_file_path,
                'original_file_type': 'djvu',
                'Content-Type': 'image/vnd.djvu',
                'resourceName': os.path.basename(original_file_path),
                'extraction_method': 'djvutxt'
            }
            
            logger.info(f"Successfully extracted {len(content)} characters from DJVU")
            return file_sha1, content, metadata
        else:
            raise ValueError("No content extracted from DJVU")
            
    except Exception as e:
        logger.error(f"Error processing DJVU {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _convert_mhtml_to_html(file_path: str, original_file_path: str) -> Tuple[str, str, Dict]:
    """Convert MHTML to HTML and return (html_path, extension, metadata)"""
    try:
        logger.info(f"Processing MHTML file: {file_path}")
        
        # First extract metadata
        mhtml_metadata = extract_mhtml_metadata(file_path)
        
        # Convert MHTML to HTML
        try:
            temp_html_file = process_mhtml_to_html(file_path)
        except Exception as e:
            logger.warning(f"mhtml-to-html-py failed: {e}, trying fallback converter")
            try:
                temp_html_file = process_mhtml_to_html_python(file_path)
            except Exception as e2:
                logger.error(f"Both MHTML converters failed: {e2}")
                raise ValueError("Failed to convert MHTML to HTML")
        
        return temp_html_file, '.html', mhtml_metadata
        
    except Exception as e:
        logger.error(f"Error processing MHTML: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))


async def _process_html_file(file_path: str, file_sha1: str, original_file_path: str, 
                             use_readability: bool, mhtml_metadata: Optional[Dict],
                             html_extractor: str = 'default', skip_garbage_check: bool = False) -> Tuple[str, str, Dict]:
    """Process HTML files (including converted MHTML)"""
    try:
        logger.info(f"Processing HTML file: {file_path}")
        
        # Read the HTML file
        with open(file_path, 'rb') as f:
            raw_html = f.read()
        
        # Detect encoding
        result = chardet.detect(raw_html)
        encoding = result.get('encoding', 'utf-8')
        
        # Decode HTML
        html_content = raw_html.decode(encoding, errors='ignore')
        
        # First check if content is garbage (encoding issues) - unless skipped
        if not skip_garbage_check and is_content_garbage(html_content):
            logger.warning("HTML content appears to be garbage, trying alternative extraction methods...")
            
            # Try w3m extraction
            w3m_content, w3m_title = extract_text_with_w3m(file_path)
            if w3m_content and not is_content_garbage(w3m_content):
                content = w3m_content
                title = w3m_title
                extraction_method = 'w3m_browser'
            else:
                # Try Chrome headless extraction
                chrome_content, chrome_title = extract_with_chrome_headless(file_path)
                if chrome_content and not is_content_garbage(chrome_content):
                    content = chrome_content
                    title = chrome_title
                    extraction_method = 'chrome_headless'
                else:
                    # Use BeautifulSoup as last resort
                    soup = BeautifulSoup(html_content, 'html.parser')
                    if html_extractor == 'smart':
                        content = extract_html_text_smart(html_content)
                    else:
                        content = soup.get_text(separator='\n', strip=True)
                    title = soup.title.string if soup.title else ""
                    extraction_method = 'beautifulsoup_with_issues' if html_extractor != 'smart' else 'smart_with_issues'
        else:
            # Content looks good, use readability if requested
            if use_readability:
                try:
                    doc = Document(html_content)
                    title = doc.title()
                    if html_extractor == 'smart':
                        content = extract_html_text_smart(doc.summary())
                    else:
                        content = BeautifulSoup(doc.summary(), 'html.parser').get_text(separator='\n', strip=True)
                    extraction_method = 'readability' if html_extractor != 'smart' else 'readability_smart'
                except Exception as e:
                    logger.warning(f"Readability failed: {e}, falling back to BeautifulSoup")
                    soup = BeautifulSoup(html_content, 'html.parser')
                    if html_extractor == 'smart':
                        content = extract_html_text_smart(html_content)
                    else:
                        content = soup.get_text(separator='\n', strip=True)
                    title = soup.title.string if soup.title else ""
                    extraction_method = 'beautifulsoup' if html_extractor != 'smart' else 'beautifulsoup_smart'
            else:
                soup = BeautifulSoup(html_content, 'html.parser')
                if html_extractor == 'smart':
                    content = extract_html_text_smart(html_content)
                else:
                    content = soup.get_text(separator='\n', strip=True)
                title = soup.title.string if soup.title else ""
                extraction_method = 'beautifulsoup' if html_extractor != 'smart' else 'beautifulsoup_smart'
        
        # Build metadata
        metadata = {
            'title': title,
            'original_file_path': original_file_path,
            'original_file_type': Path(original_file_path).suffix.lower().lstrip('.'),
            'Content-Type': 'text/html',
            'resourceName': os.path.basename(original_file_path),
            'extraction_method': extraction_method
        }
        
        # If this was an MHTML file, merge in the MHTML metadata
        if Path(original_file_path).suffix.lower() in ['.mhtml', '.mht'] and mhtml_metadata:
            metadata.update(mhtml_metadata)
            metadata['processing_method'] = extraction_method
        
        # Clean up the extracted text
        content = clean_extracted_text(content)
        
        if content:
            logger.info(f"Successfully extracted {len(content)} characters from HTML using {extraction_method}")
            return file_sha1, content, metadata
        else:
            logger.warning(f"No content extracted from HTML file: {file_path}")
            raise ValueError("No content extracted from HTML")
            
    except Exception as e:
        logger.error(f"Error processing HTML {file_path}: {str(e)}")
        raise ValueError(get_unsupported_file_error(original_file_path))