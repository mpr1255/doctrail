"""
Base classes and exceptions for the ingestion module.
"""

class IngestionError(Exception):
    """Base exception for document ingestion errors."""
    pass

class SkippedFileException(Exception):
    """Exception raised when a file is intentionally skipped during ingestion."""
    pass