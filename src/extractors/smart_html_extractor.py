#!/usr/bin/env -S uv run
"""Smart HTML text extraction that preserves paragraph structure."""

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from typing import List
import re
import logging

logger = logging.getLogger(__name__)


def fix_mixed_encoding(text: str) -> str:
    """Fix text with mixed UTF-8 and Windows-1252 encoding.
    
    This handles:
    1. Double-encoded UTF-8 (â€™ → ')
    2. Raw Windows-1252 bytes in UTF-8 text
    3. UTF-8 BOM at start of content
    """
    # Remove BOM if present
    if text.startswith('\ufeff') or text.startswith('ï»¿'):
        text = text[1:] if text.startswith('\ufeff') else text[3:]
    
    # First fix double-encoded UTF-8 sequences
    # These appear as 3 UTF-8 characters that were originally one character
    # Note: \x80 in a string literal is different from the actual Unicode character U+0080
    replacements = [
        # Smart quotes and punctuation - using actual Unicode characters
        ('â\u0080\u0099', "'"),   # right single quote (C3 A2 C2 80 C2 99)
        ('â\u0080\u0098', "'"),   # left single quote  
        ('â\u0080\u009c', '"'),   # left double quote
        ('â\u0080\u009d', '"'),   # right double quote
        ('â\u0080\u0094', '—'),   # em dash
        ('â\u0080\u0093', '–'),   # en dash
        ('â\u0080¦', '…'),        # ellipsis
        
        # Less common but found in some files
        ('â\x80\x9a', 'š'),   # s with caron
        ('â\x80\x9b', '›'),   # single right angle quote
        
        # Also try with explicit sequences (different representations)
        ('â€™', "'"),   # right single quote
        ('â€˜', "'"),   # left single quote  
        ('â€œ', '"'),   # left double quote
        ('â€"', '—'),   # em dash
        ('â€"', '–'),   # en dash
        
        # Accented characters
        ('Ã¢', 'â'),   # circumflex a
        ('Ã©', 'é'),   # acute e
        ('Ã¨', 'è'),   # grave e
        ('Ã´', 'ô'),   # circumflex o
        ('Ã§', 'ç'),   # cedilla c
        ('Ã±', 'ñ'),   # tilde n
        ('Ã¼', 'ü'),   # umlaut u
        ('Ã¶', 'ö'),   # umlaut o
        ('Ã¤', 'ä'),   # umlaut a
        
        # Common sequences
        ('Â ', ' '),    # non-breaking space
        ('Â´', '´'),    # acute accent
        ('Â°', '°'),    # degree symbol
    ]
    
    fixed = text
    for bad, good in replacements:
        fixed = fixed.replace(bad, good)
    
    # Also handle raw Windows-1252 bytes that might appear as Unicode replacement char
    # These often show up as � or as literal byte values in the text
    # Common patterns: \x91-\x97 are smart quotes and dashes
    win1252_fixes = {
        '\x91': "'",  # left single quote
        '\x92': "'",  # right single quote
        '\x93': '"',  # left double quote
        '\x94': '"',  # right double quote
        '\x95': "•",  # bullet
        '\x96': "–",  # en dash
        '\x97': "—",  # em dash
        '\x85': "…",  # ellipsis
    }
    
    for bad, good in win1252_fixes.items():
        fixed = fixed.replace(bad, good)
    
    return fixed


def extract_html_text_smart(html_content: str) -> str:
    """
    Extract text from HTML with intelligent paragraph preservation.
    
    This extractor:
    - Only adds newlines for block-level elements (p, div, h1-h6, etc.)
    - Removes HTML comments (like <!-- BODY GOES HERE -->)
    - Preserves paragraph structure with proper spacing
    - Handles inline elements (span, em, strong) without extra newlines
    - Fixes double-encoded UTF-8 (smart quotes showing as â€™)
    
    Args:
        html_content: Raw HTML string
        
    Returns:
        Extracted text with proper paragraph breaks and fixed encoding
    """
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Remove all comments (like "BODY GOES HERE")
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()
        
        # Remove script and style elements
        for element in soup(['script', 'style', 'noscript']):
            element.decompose()
        
        # Block elements that should trigger newlines
        BLOCK_ELEMENTS = {
            'p', 'div', 'section', 'article', 'header', 'footer',
            'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
            'ul', 'ol', 'li', 'dl', 'dt', 'dd',
            'blockquote', 'pre', 'hr', 'br',
            'table', 'tr', 'td', 'th',
            'form', 'fieldset', 'legend',
            'nav', 'aside', 'main', 'figure', 'figcaption'
        }
        
        # Elements that should have blank line after
        PARAGRAPH_ELEMENTS = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
        
        def process_element(element, lines: List[str], current_line: List[str] = None) -> List[str]:
            """Recursively process elements maintaining proper line breaks."""
            if current_line is None:
                current_line = []
            
            for item in element.children:
                if isinstance(item, NavigableString):
                    # Text node
                    text = str(item)
                    # Collapse whitespace (including newlines) but preserve single spaces
                    # This handles HTML with hard line breaks in the middle of paragraphs
                    text = re.sub(r'\s+', ' ', text)
                    text = text.strip()
                    if text:
                        current_line.append(text)
                        
                elif isinstance(item, Tag):
                    if item.name in BLOCK_ELEMENTS:
                        # Finish current line if it has content
                        if current_line:
                            lines.append(' '.join(current_line))
                            current_line = []
                        
                        # Process children
                        child_lines = []
                        child_current = []
                        process_element(item, child_lines, child_current)
                        
                        # Add accumulated child content
                        if child_current:
                            child_lines.append(' '.join(child_current))
                        lines.extend(child_lines)
                        
                        # Add blank line after paragraph elements
                        if item.name in PARAGRAPH_ELEMENTS:
                            if lines and lines[-1]:  # Don't add multiple blank lines
                                lines.append('')
                    else:
                        # Inline element - continue on same line
                        process_element(item, lines, current_line)
            
            return current_line
        
        lines = []
        remaining = process_element(soup, lines)
        if remaining:
            lines.append(' '.join(remaining))
        
        # Clean up excessive blank lines (max 1 blank line between paragraphs)
        cleaned_lines = []
        prev_blank = False
        for line in lines:
            if not line:  # Blank line
                if not prev_blank:
                    cleaned_lines.append(line)
                prev_blank = True
            else:
                cleaned_lines.append(line)
                prev_blank = False
        
        result = '\n'.join(cleaned_lines).strip()
        
        # Fix mixed encoding issues (double-encoded UTF-8, Windows-1252, BOM)
        result = fix_mixed_encoding(result)
        
        logger.debug(f"Smart HTML extraction: {len(result)} chars, {result.count(chr(10))} newlines")
        return result
        
    except Exception as e:
        logger.error(f"Smart HTML extraction failed: {e}")
        # Fallback to simple extraction
        soup = BeautifulSoup(html_content, 'html.parser')
        fallback_text = soup.get_text(separator=' ', strip=True)
        # Still fix encoding in fallback
        return fix_mixed_encoding(fallback_text)