"""Smart HTML text extraction that preserves paragraph structure."""

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from typing import List
import re
import logging

logger = logging.getLogger(__name__)


def extract_html_text_smart(html_content: str) -> str:
    """
    Extract text from HTML with intelligent paragraph preservation.
    
    Args:
        html_content: Raw HTML string
        
    Returns:
        Extracted text with proper paragraph breaks
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
                    # Collapse whitespace but preserve single spaces
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
        logger.debug(f"Smart extraction: {len(result)} chars, {result.count(chr(10))} newlines")
        return result
        
    except Exception as e:
        logger.error(f"Smart HTML extraction failed: {e}")
        # Fallback to simple extraction
        soup = BeautifulSoup(html_content, 'html.parser')
        return soup.get_text(separator=' ', strip=True)