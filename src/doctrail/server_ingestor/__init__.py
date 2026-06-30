"""
Web interface for Doctrail document ingestion.

This module provides a FastAPI-based web server that offers a browser interface
for document ingestion, acting as a pure frontend wrapper around the existing
ingest functionality.
"""

from .app import create_app, start_server

__all__ = ["create_app", "start_server"]