#!/usr/bin/env python3
"""
Legacy ingester module - redirects to new ingest package.

This module is kept for backward compatibility. All functionality has been
moved to the src/ingest/ package.
"""

# Import everything from the new location for backward compatibility
from .ingest import (
    process_ingest,
    process_document,
    insert_document,
    check_db_schema,
    setup_fts,
    clean_metadata,
    SkippedFileException
)

# Re-export for backward compatibility
__all__ = [
    'process_ingest',
    'process_document', 
    'insert_document',
    'check_db_schema',
    'setup_fts',
    'clean_metadata',
    'SkippedFileException'
]

# For backward compatibility
if __name__ == "__main__":
    # This allows the module to be run directly
    import asyncio
    asyncio.run(process_ingest(
        db_path="test.db",
        input_dir=".",
        table="documents",
        verbose=True
    ))