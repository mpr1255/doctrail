"""
Text processing utilities for document ingestion.

This module contains functions for cleaning, normalizing, and processing
extracted text content.
"""

import re
import logging
import unicodedata
from typing import Dict

logger = logging.getLogger(__name__)

ANSI_ESCAPE_RE = re.compile(
    r"""
    \x1b
    (?:
        \[[0-?]*[\x20-\x2f]*[@-~]
        |\][^\x07\x1b]*(?:\x07|\x1b\\)
        |[PX^_][^\x1b]*(?:\x1b\\)
        |[@-_]
    )
    """,
    re.VERBOSE | re.DOTALL,
)

BIDI_CONTROL_CODEPOINTS = {
    0x061C,
    0x200E,
    0x200F,
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
}


def sanitize_text_for_storage(text: str) -> str:
    """Remove unsafe controls without damaging multilingual text."""
    if not isinstance(text, str):
        raise TypeError(f"sanitize_text_for_storage expected str, got {type(text).__name__}")
    if not text:
        return ""

    text = ANSI_ESCAPE_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\f", "\n").replace("\v", " ")

    cleaned = []
    for character in text:
        if character in "\n\t":
            cleaned.append(character)
            continue
        codepoint = ord(character)
        if codepoint == 0xFEFF or codepoint in BIDI_CONTROL_CODEPOINTS:
            continue
        if unicodedata.category(character) in {"Cc", "Cs"}:
            continue
        cleaned.append(character)
    return "".join(cleaned)


def add_page_markers(text: str) -> str:
    """
    Add page break markers to text for better readability.
    
    Args:
        text: Raw text content
        
    Returns:
        Text with page markers added
    """
    # Split on form feed characters and add markers
    pages = text.split('\f')
    if len(pages) > 1:
        marked_text = '\n\n--- PAGE BREAK ---\n\n'.join(pages)
        return marked_text
    return text


def clean_extracted_text(text: str) -> str:
    """
    Clean extracted text by removing extra whitespace and normalizing line breaks.
    
    Args:
        text: Raw extracted text
        
    Returns:
        Cleaned text
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        raise TypeError(f"clean_extracted_text expected str, got {type(text).__name__}")
    if not text:
        return ""
    
    # Split into lines for processing
    lines = text.split('\n')
    cleaned_lines = []
    
    for line in lines:
        # Strip whitespace from each line
        line = line.strip()
        
        # Skip empty lines and lines with only whitespace/punctuation
        if not line or re.match(r'^[\s\-_=*+.]+$', line):
            continue
            
        # Remove excessive repeated characters (like ===== or -----)
        line = re.sub(r'([^\w\s])\1{4,}', r'\1\1', line)
        
        cleaned_lines.append(line)
    
    # Join with single newlines and remove excessive newlines (more than 2)
    text = '\n'.join(cleaned_lines)
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()


def is_text_garbage(text: str, min_length: int = 100) -> bool:
    """
    Detect if extracted text is garbage/corrupted.
    
    Args:
        text: Text to analyze
        min_length: Minimum length threshold
        
    Returns:
        True if text appears to be garbage
    """
    if not text or len(text) < min_length:
        return True

    # Check for excessive repeated characters — but measure proportion, not existence.
    # A single decorative line (e.g. 25 underscores as section divider, or 253 dots
    # from a table of contents) shouldn't invalidate 100k chars of clean text.
    repeated_chars = sum(len(m.group(0)) for m in re.finditer(r'(.)\1{20,}', text))
    if repeated_chars / len(text) > 0.3:
        return True

    # Check ratio of alphanumeric to total characters
    # Threshold 0.15 accommodates formatting-heavy, non-Latin, and CJK text
    alphanumeric_count = sum(1 for c in text if c.isalnum())
    if len(text) > 0 and alphanumeric_count / len(text) < 0.15:
        return True

    return False


def is_content_garbage(content: str) -> bool:
    """
    Check if extracted content appears to be garbage.
    
    Args:
        content: Content to check
        
    Returns:
        True if content appears to be garbage
    """
    if not content or len(content.strip()) < 50:
        return True
    
    # Check for excessive repetition
    if re.search(r'(.{1,10})\1{10,}', content):
        return True
    
    # Check for binary-like content
    non_printable = sum(1 for c in content if ord(c) < 32 and c not in '\n\r\t')
    if len(content) > 0 and non_printable / len(content) > 0.1:
        return True
    
    return False


def clean_ocr_text(text: str) -> str:
    """
    Clean OCR artifacts from extracted text.
    
    Args:
        text: OCR text to clean
        
    Returns:
        Cleaned text
    """
    if not text:
        return ""

    # Normalize newline variants early so downstream consumers keep layout
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\f', '\n')

    # Replace non-breaking spaces and collapse runs of spaces/tabs but keep newlines intact
    text = text.replace('\u00a0', ' ')
    text = re.sub(r'[ \t\x0b]+', ' ', text)

    # Remove common OCR artifacts while preserving structural whitespace
    text = re.sub(r'[|]{2,}', '', text)
    text = re.sub(r'[_]{3,}', '', text)
    text = re.sub(r'\.{4,}', '...', text)

    # Trim stray spaces around line breaks and limit excessive blank lines
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def clean_metadata(metadata: dict) -> dict:
    """
    Clean and normalize metadata extracted from documents.
    
    Args:
        metadata: Raw metadata dictionary
        
    Returns:
        Cleaned metadata dictionary
    """
    if not isinstance(metadata, dict):
        return {}
    
    cleaned = {}
    
    for key, value in metadata.items():
        # Skip None values
        if value is None:
            continue
        
        # Convert to string if not already
        if not isinstance(value, str):
            value = str(value)
        
        # Clean the value
        value = value.strip()
        if not value:
            continue
        
        # Normalize key names
        clean_key = key.lower().replace('-', '_').replace(' ', '_')
        
        # Store cleaned value
        cleaned[clean_key] = value
    
    return cleaned
