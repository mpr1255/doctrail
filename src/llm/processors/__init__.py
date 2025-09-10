"""Processors for different types of LLM operations."""

from .base import BaseProcessor
from .enrichment import EnrichmentProcessor

__all__ = ['BaseProcessor', 'EnrichmentProcessor']