"""MHTML file extraction and processing module."""

import os
import re
import sys
import json
import tempfile
import subprocess
import importlib.util
import logging
from typing import Tuple, Optional, Dict

logger = logging.getLogger(__name__)


def is_custom_archive_format(file_path: str) -> bool:
    """
    Check if the MHTML file is in the custom X-Archive format
    """
    try:
        with open(file_path, 'rb') as f:
            # Read first 2KB to check for X-Archive meta tags
            content = f.read(2048)
            text = content.decode('utf-8', errors='ignore')
            return '<meta name="X-Archive-' in text
    except Exception:
        return False


def extract_custom_archive_metadata(file_path: str) -> dict:
    """
    Extract metadata from custom X-Archive format MHTML files
    """
    metadata = {}
    
    try:
        with open(file_path, 'rb') as f:
            # Read more content for custom format as meta tags might be deeper
            content = f.read(100000)
        
        # Detect encoding
        import chardet
        result = chardet.detect(content)
        encoding = result.get('encoding', 'utf-8')
        
        try:
            text = content.decode(encoding, errors='ignore')
        except:
            text = content.decode('utf-8', errors='ignore')
        
        # Extract X-Archive meta tags
        import re
        
        # Pattern to match X-Archive meta tags
        meta_pattern = r'<meta\s+name="X-Archive-([^"]+)"\s+content="([^"]+)"'
        matches = re.findall(meta_pattern, text, re.IGNORECASE)
        
        for name, value in matches:
            # Convert X-Archive names to lowercase with underscores
            key = f"x_archive_{name.lower().replace('-', '_')}"
            metadata[key] = value
            
            # Also create convenience mappings for common fields
            if name.upper() == "ORIGINAL-URL":
                metadata['original_url'] = value
                metadata['source_url'] = value
            elif name.upper() == "CAPTURE-DATE":
                metadata['capture_date'] = value
                metadata['save_date'] = value
            elif name.upper() == "TITLE":
                metadata['page_title'] = value
            elif name.upper() == "USERNAME":
                metadata['archive_username'] = value
            elif name.upper() == "USER-AGENT":
                metadata['user_agent'] = value
            elif name.upper() == "URL-SHA1":
                metadata['url_sha1'] = value
            elif name.upper() == "PRISTINE-MHTML-SHA256-HASH":
                metadata['pristine_sha256'] = value
        
        # Extract standard MHTML headers too
        # Look for From, Subject, Date, etc.
        for line in text.split('\n'):
            line = line.strip()
            if line.startswith('From:'):
                metadata['mhtml_from'] = line.split(':', 1)[1].strip()
            elif line.startswith('Subject:'):
                subject = line.split(':', 1)[1].strip()
                metadata['mhtml_subject'] = subject
                # Decode if encoded (handles multi-line subjects)
                if '=?utf-8?' in subject.lower():
                    try:
                        import email.header
                        # Collect full subject (may span multiple lines)
                        full_subject = subject
                        # Look ahead for continuation lines
                        line_idx = text.find(line) + len(line)
                        next_lines = text[line_idx:].split('\n')
                        for next_line in next_lines:
                            if next_line.startswith(' ') and '=?' in next_line:
                                full_subject += next_line
                            else:
                                break
                        # Now decode the full subject
                        decoded_parts = []
                        for part, encoding in email.header.decode_header(full_subject):
                            if isinstance(part, bytes):
                                decoded_parts.append(part.decode(encoding or 'utf-8'))
                            else:
                                decoded_parts.append(part)
                        metadata['mhtml_subject_decoded'] = ''.join(decoded_parts)
                    except Exception as e:
                        logger.debug(f"Could not decode subject: {e}")
            elif line.startswith('Date:'):
                metadata['mhtml_date'] = line.split(':', 1)[1].strip()
            elif line.startswith('Snapshot-Content-Location:'):
                metadata['snapshot_url'] = line.split(':', 1)[1].strip()
                if 'original_url' not in metadata:
                    metadata['original_url'] = metadata['snapshot_url']
        
        metadata['file_type'] = 'custom_archive_mhtml'
        metadata['extraction_method'] = 'x_archive_parsing'
        
        logger.info(f"Extracted custom archive metadata keys: {list(metadata.keys())}")
        
        return metadata
        
    except Exception as e:
        logger.error(f"Error extracting custom archive metadata from {file_path}: {e}")
        return {'file_type': 'custom_archive_mhtml', 'extraction_error': str(e)}


def extract_mhtml_metadata(mhtml_path: str) -> dict:
    """
    Extract metadata from MHTML file headers including original URL, save date, etc.
    
    Args:
        mhtml_path: Path to the MHTML file
        
    Returns:
        Dictionary containing extracted metadata
    """
    # Check if it's a custom archive format first
    if is_custom_archive_format(mhtml_path):
        logger.info(f"Detected custom X-Archive format for {mhtml_path}")
        return extract_custom_archive_metadata(mhtml_path)
    
    metadata = {}
    
    try:
        # Read the file with encoding detection
        import chardet
        
        with open(mhtml_path, 'rb') as f:
            # Read first 8KB to get headers (MHTML headers are typically at the start)
            header_content = f.read(8192)
            
        # Detect encoding
        result = chardet.detect(header_content)
        encoding = result.get('encoding', 'utf-8')
        
        # Decode the header content
        try:
            header_text = header_content.decode(encoding, errors='ignore')
        except:
            header_text = header_content.decode('utf-8', errors='ignore')
        
        # Split into lines for header parsing
        lines = header_text.split('\n')
        
        # Extract MHTML headers
        for line in lines:
            line = line.strip()
            
            # Stop when we hit the boundary or HTML content
            if line.startswith('------') or line.startswith('<html') or line.startswith('<!DOCTYPE'):
                break
                
            # Parse header lines (format: "Header-Name: value")
            if ':' in line and not line.startswith('Content-Type: text/html'):
                try:
                    key, value = line.split(':', 1)
                    key = key.strip()
                    value = value.strip()
                    
                    # Map common MHTML headers to our metadata
                    if key.lower() == 'snapshot-content-location':
                        metadata['original_url'] = value
                        metadata['source_url'] = value  # Alternative key
                    elif key.lower() == 'date':
                        metadata['save_date'] = value
                        metadata['mhtml_date'] = value
                    elif key.lower() == 'subject':
                        # Subject often contains the page title (sometimes encoded)
                        metadata['mhtml_subject'] = value
                        # Try to decode if it's encoded
                        if '=?utf-8?' in value.lower():
                            try:
                                import email.header
                                decoded = email.header.decode_header(value)
                                if decoded and decoded[0][0]:
                                    if isinstance(decoded[0][0], bytes):
                                        metadata['mhtml_subject_decoded'] = decoded[0][0].decode(decoded[0][1] or 'utf-8')
                                    else:
                                        metadata['mhtml_subject_decoded'] = decoded[0][0]
                            except Exception as e:
                                logger.debug(f"Failed to decode subject header: {e}")
                    elif key.lower() == 'from':
                        metadata['mhtml_from'] = value
                    elif key.lower() == 'mime-version':
                        metadata['mime_version'] = value
                    elif key.lower() == 'content-type' and 'multipart' in value.lower():
                        metadata['content_type'] = value
                        # Extract boundary if present
                        if 'boundary=' in value:
                            boundary_part = value.split('boundary=')[1]
                            if '"' in boundary_part:
                                boundary = boundary_part.split('"')[1]
                            else:
                                boundary = boundary_part.split(';')[0].strip()
                            metadata['mhtml_boundary'] = boundary
                    else:
                        # Store other headers with mhtml_ prefix
                        metadata[f'mhtml_{key.lower().replace("-", "_")}'] = value
                        
                except Exception as e:
                    logger.debug(f"Error parsing header line '{line}': {e}")
                    continue
        
        # Add file-level metadata
        metadata['file_type'] = 'mhtml'
        metadata['extraction_method'] = 'mhtml_header_parsing'
        
        logger.debug(f"Extracted MHTML metadata: {list(metadata.keys())}")
        if 'original_url' in metadata:
            logger.info(f"Found original URL in MHTML: {metadata['original_url']}")
        if 'save_date' in metadata:
            logger.info(f"Found save date in MHTML: {metadata['save_date']}")
            
        return metadata
        
    except Exception as e:
        logger.error(f"Error extracting MHTML metadata from {mhtml_path}: {str(e)}")
        return {'file_type': 'mhtml', 'extraction_method': 'mhtml_header_parsing', 'extraction_error': str(e)}


def process_custom_archive_to_html(file_path: str) -> str:
    """
    Extract and process HTML content from custom X-Archive format files
    Returns the path to a temporary HTML file with clean content
    """
    logger.info(f"Processing custom archive format: {file_path}")
    
    try:
        # Read the entire file
        with open(file_path, 'rb') as f:
            raw_content = f.read()
        
        # Try multiple encodings for Chinese content
        encodings_to_try = ['utf-8', 'gb2312', 'gbk', 'gb18030', 'big5']
        decoded_content = None
        used_encoding = None
        
        for encoding in encodings_to_try:
            try:
                decoded = raw_content.decode(encoding)
                # Check if content looks reasonable using garbage detection
                from ..text_processing import is_content_garbage
                if not is_content_garbage(decoded):
                    decoded_content = decoded
                    used_encoding = encoding
                    logger.info(f"Successfully decoded with {encoding}")
                    break
            except:
                continue
        
        if not decoded_content:
            # Fallback to chardet
            import chardet
            result = chardet.detect(raw_content)
            encoding = result.get('encoding', 'utf-8')
            decoded_content = raw_content.decode(encoding, errors='ignore')
            used_encoding = encoding
            logger.warning(f"Using chardet detected encoding: {encoding}")
        
        # Look for the MIME boundary
        boundary_match = re.search(r'boundary="([^"]+)"', decoded_content)
        if not boundary_match:
            boundary_match = re.search(r'boundary=([^\s]+)', decoded_content)
        
        if boundary_match:
            boundary = boundary_match.group(1)
            logger.info(f"Found MIME boundary: {boundary}")
            
            # Split by boundary and find HTML part
            parts = decoded_content.split(f'--{boundary}')
            
            for part in parts:
                if 'Content-Type: text/html' in part:
                    # Found HTML part
                    logger.info("Found HTML part in MIME structure")
                    
                    # Check for quoted-printable encoding
                    if 'Content-Transfer-Encoding: quoted-printable' in part:
                        logger.info("Detected quoted-printable encoding")
                        
                        # Extract just the content part (after headers)
                        content_start = part.find('\n\n')
                        if content_start == -1:
                            content_start = part.find('\r\n\r\n')
                        
                        if content_start != -1:
                            html_content = part[content_start:].strip()
                            
                            # Decode quoted-printable
                            import quopri
                            try:
                                # For quoted-printable, we need to handle it as bytes
                                html_bytes = quopri.decodestring(html_content.encode('latin-1'))
                                
                                # Now decode with the proper encoding
                                from ..text_processing import is_content_garbage
                                for enc in [used_encoding] + encodings_to_try:
                                    try:
                                        html_content = html_bytes.decode(enc)
                                        if not is_content_garbage(html_content):
                                            logger.info(f"Decoded quoted-printable content with {enc}")
                                            break
                                    except:
                                        continue
                                else:
                                    html_content = html_bytes.decode(used_encoding, errors='ignore')
                            except Exception as e:
                                logger.warning(f"Error decoding quoted-printable: {e}")
                    else:
                        # Not quoted-printable, extract HTML directly
                        html_start = part.find('<')
                        if html_start != -1:
                            html_content = part[html_start:]
                    
                    # Clean up any trailing boundaries
                    if '--' in html_content:
                        html_content = html_content.split('--')[0]
                    
                    # Try readability for clean extraction before saving
                    try:
                        from readability import Document
                        doc = Document(html_content)
                        title = doc.title()
                        clean_html = doc.summary()
                        
                        # Extract text from cleaned HTML to check quality
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(clean_html, 'html.parser')
                        content = soup.get_text(separator='\n', strip=True)
                        
                        # If readability gave us good content, use the clean HTML
                        from ..text_processing import is_content_garbage
                        if len(content) > 100 and not is_content_garbage(content):
                            logger.info(f"Successfully cleaned HTML content with readability: {len(content)} characters")
                            html_content = clean_html
                        
                    except Exception as e:
                        logger.debug(f"Readability processing failed, using original HTML: {e}")
                    
                    # Save to temporary file
                    temp_html = tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8')
                    temp_html.write(html_content)
                    temp_html.close()
                    
                    logger.info(f"Saved HTML to temporary file: {temp_html.name}")
                    return temp_html.name
        
        # Fallback: look for HTML content directly
        logger.warning("No MIME structure found, trying direct HTML extraction")
        html_start = decoded_content.find('<!DOCTYPE')
        if html_start == -1:
            html_start = decoded_content.find('<html')
        if html_start == -1:
            html_start = decoded_content.find('<HTML')
        
        if html_start != -1:
            html_content = decoded_content[html_start:]
            if '------' in html_content:
                html_content = html_content.split('------')[0]
            
            # Try readability for clean extraction before saving
            try:
                from readability import Document
                doc = Document(html_content)
                title = doc.title()
                clean_html = doc.summary()
                
                # Extract text from cleaned HTML to check quality
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(clean_html, 'html.parser')
                content = soup.get_text(separator='\n', strip=True)
                
                # If readability gave us good content, use the clean HTML
                from ..text_processing import is_content_garbage
                if len(content) > 100 and not is_content_garbage(content):
                    logger.info(f"Successfully cleaned fallback HTML content with readability: {len(content)} characters")
                    html_content = clean_html
                
            except Exception as e:
                logger.debug(f"Readability processing failed on fallback, using original HTML: {e}")
            
            temp_html = tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8')
            temp_html.write(html_content)
            temp_html.close()
            return temp_html.name
        
        raise ValueError("Could not find HTML content in custom archive file")
        
    except Exception as e:
        logger.error(f"Error processing custom archive: {e}")
        raise


def extract_html_from_custom_archive(file_path: str) -> str:
    """
    Legacy function name - redirects to new implementation
    """
    return process_custom_archive_to_html(file_path)


def process_mhtml_to_html(mhtml_path: str) -> str:
    """
    Convert MHTML file to HTML using the mhtml-to-html-py Python package
    Returns the path to the temporary HTML file
    """
    # Check if it's a custom archive format first
    if is_custom_archive_format(mhtml_path):
        logger.info(f"Using custom archive extraction for: {mhtml_path}")
        return extract_html_from_custom_archive(mhtml_path)
    
    # Use the mhtml-to-html-py package
    from mhtml_converter import convert_mhtml
    
    logger.info(f"Converting MHTML to HTML using mhtml-to-html-py package: {mhtml_path}")
    
    # Convert MHTML to HTML string with verbose encoding detection
    html_content = convert_mhtml(mhtml_path, verbose=True)
    
    if not html_content or len(html_content) < 100:
        logger.warning(f"MHTML conversion produced suspiciously small HTML: {len(html_content) if html_content else 0} bytes")
        raise ValueError(f"MHTML conversion failed or produced insufficient content")
    
    logger.debug(f"MHTML conversion completed, HTML size: {len(html_content)} bytes")
    
    # Create a temporary file with the HTML content
    temp_html = tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8')
    temp_html.write(html_content)
    temp_html.close()
    
    logger.info(f"Successfully converted MHTML to HTML using mhtml-to-html-py: {temp_html.name}")
    return temp_html.name


def process_mhtml_to_html_python(mhtml_path: str) -> str:
    """
    Fallback Python implementation for MHTML to HTML conversion
    """
    
    logger.info(f"Converting MHTML file to HTML with encoding detection: {mhtml_path}")
    
    # Import required libraries
    import chardet
    
    # Import the mhtml-to-html script
    mhtml_converter_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mhtml-to-html.py")
    
    # Verify that the converter script exists
    if not os.path.exists(mhtml_converter_path):
        logger.error(f"MHTML converter script not found at: {mhtml_converter_path}")
        raise FileNotFoundError(f"MHTML converter script not found at: {mhtml_converter_path}")
    
    logger.debug(f"Using MHTML converter at: {mhtml_converter_path}")
    
    spec = importlib.util.spec_from_file_location("mhtml_converter_fallback", mhtml_converter_path)
    mhtml_converter = importlib.util.module_from_spec(spec)
    sys.modules["mhtml_converter_fallback"] = mhtml_converter
    spec.loader.exec_module(mhtml_converter)
    
    # Convert the MHTML file to HTML
    try:
        # Make sure the file exists and is readable
        if not os.path.isfile(mhtml_path):
            raise FileNotFoundError(f"MHTML file not found: {mhtml_path}")
            
        # Read file and detect encoding
        with open(mhtml_path, 'rb') as f:
            content = f.read()
            result = chardet.detect(content)
            encoding = result['encoding']
            confidence = result['confidence']
            logger.debug(f"Detected encoding for {mhtml_path}: {encoding} (confidence: {confidence})")
            
            # Check file is an MHTML file by examining its contents
            header = content[:2048].decode(encoding or 'utf-8', errors='ignore')
            if "MIME-Version:" not in header and "Content-Type: multipart/" not in header:
                logger.warning(f"File does not appear to be a valid MHTML file: {mhtml_path}")
                # Continue anyway, as converter will handle errors
        
        # Actual conversion
        logger.debug(f"Starting MHTML conversion with encoding {encoding} for: {mhtml_path}")
        
        # If the encoding is particularly challenging, we can pass it to the converter
        # This requires modifying the converter to accept an encoding parameter
        html_content = mhtml_converter.convert(mhtml_path)
        
        # Check if the conversion was successful
        if not html_content or len(html_content) < 100:
            logger.warning(f"MHTML conversion produced suspiciously small HTML: {len(html_content)} bytes")
        else:
            logger.debug(f"MHTML conversion completed, HTML size: {len(html_content)} bytes")
        
        # Create a temporary file for the HTML content with UTF-8 encoding
        with tempfile.NamedTemporaryFile(suffix='.html', delete=False) as temp_file:
            temp_file.write(html_content.encode('utf-8'))
            logger.info(f"Created temporary HTML file (UTF-8): {temp_file.name}")
            return temp_file.name
    except Exception as e:
        logger.error(f"Error converting MHTML to HTML: {str(e)}")
        logger.debug(f"MHTML conversion failed", exc_info=True)
        raise


def extract_with_chrome_headless(file_path: str) -> tuple[str, str]:
    """
    Extract text content from an MHTML file using Chrome Headless directly
    This is particularly effective for Chinese content
    
    Returns a tuple of (content, title)
    """
    try:
        # Create a temporary script to extract text
        with tempfile.NamedTemporaryFile(suffix='.js', mode='w', delete=False) as js_file:
            js_file.write('''
            async function extractText() {
                const text = document.body.innerText;
                const title = document.title;
                return JSON.stringify({
                    title: title,
                    content: text
                });
            }
            
            // Run the extraction after page load
            setTimeout(async () => {
                const result = await extractText();
                console.log(result);
            }, 1000);
            ''')
            js_script = js_file.name
        
        # Define chrome binary possibilities
        chrome_paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # Mac
            "/usr/bin/google-chrome",  # Linux
            "chrome",  # In PATH
            "google-chrome",  # In PATH
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",  # Windows
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"  # Windows 32-bit
        ]
        
        chrome_binary = None
        for path in chrome_paths:
            try:
                # Check if the binary exists and is executable
                if os.path.exists(path) and (os.access(path, os.X_OK) or path in ["chrome", "google-chrome"]):
                    chrome_binary = path
                    break
            except Exception:
                continue
        
        if not chrome_binary:
            # Try to find Chrome using `which` command on UNIX systems
            try:
                chrome_which = subprocess.check_output(['which', 'google-chrome'], text=True).strip()
                if chrome_which:
                    chrome_binary = chrome_which
            except:
                try:
                    chrome_which = subprocess.check_output(['which', 'chrome'], text=True).strip()
                    if chrome_which:
                        chrome_binary = chrome_which
                except:
                    pass
        
        if not chrome_binary:
            logger.warning("Chrome binary not found. Please install Chrome or add it to PATH.")
            return "", ""
            
        # Log found Chrome path
        logger.info(f"Using Chrome binary at: {chrome_binary}")
        
        # Direct chrome headless command - simple version first
        try:
            absolute_path = os.path.abspath(file_path)
            file_url = f"file://{absolute_path}"
            
            # Simple approach first - just dump the DOM
            simple_cmd = [
                chrome_binary,
                "--headless",
                "--disable-gpu",
                file_url,
                "--dump-dom"
            ]
            
            logger.debug(f"Running simple Chrome headless command: {' '.join(simple_cmd)}")
            
            try:
                html_output = subprocess.check_output(simple_cmd, stderr=subprocess.PIPE, text=True, timeout=20)
                
                # Parse the HTML with BeautifulSoup
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html_output, 'html.parser')
                
                # Get title
                title = ""
                if soup.title:
                    title = soup.title.string
                
                # Get content
                content = soup.get_text(separator='\n', strip=True)
                
                if content and len(content) > 100:
                    logger.info(f"Successfully extracted {len(content)} characters using simple Chrome approach")
                    return content, title
                
            except subprocess.TimeoutExpired:
                logger.warning("Chrome headless command timed out")
            except Exception as e:
                logger.debug(f"Simple Chrome approach failed: {e}")
        
        except Exception as e:
            logger.error(f"Error in Chrome headless extraction: {e}")
            
    except Exception as e:
        logger.error(f"Fatal error in Chrome headless extraction: {e}")
        
    return "", ""