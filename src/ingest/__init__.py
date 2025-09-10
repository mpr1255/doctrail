"""
Doctrail Document Ingestion Module

This module handles document ingestion into SQLite databases, with support for
various file formats, text extraction, and content processing.
"""

from .core import process_ingest
from .database import insert_document, check_db_schema, setup_fts, clean_metadata
from .document_processor import process_document, SkippedFileException

__all__ = [
    'process_ingest',
    'process_document',
    'insert_document',
    'check_db_schema',
    'setup_fts',
    'clean_metadata',
    'SkippedFileException'
]