"""Repository pattern for database operations."""

from .base_repository import BaseRepository
from .enrichment_repository import EnrichmentRepository
from .document_repository import DocumentRepository

__all__ = ['BaseRepository', 'EnrichmentRepository', 'DocumentRepository']