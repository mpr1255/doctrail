"""
Database operations for document ingestion.

This module contains functions for creating tables, inserting documents,
and managing database schema for the ingestion process.
"""

import os
import json
import logging
import sqlite3
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, List
import sqlite_utils
from loguru import logger

logger = logging.getLogger(__name__)


def insert_document(
    db,
    table_name: str,
    sha1: str,
    file_path: str,
    content: str,
    metadata: dict,
    *,
    labels: Optional[List[str]] = None,
    json_metadata: Optional[dict] = None,
    extra_fields: Optional[Dict[str, str]] = None,
    overwrite: bool = False,
    file_stat_path: Optional[str] = None,
):
    """Insert or replace one ingested document.

    Concurrency is left to SQLite locking through sqlite-utils. The `sha1`
    primary key gives replace semantics when overwrite is enabled.

    labels: optional list of labels to store as JSON array in 'labels' column
    json_metadata: optional dict to store as JSON in 'json_metadata' column
    extra_fields: optional flat dict of additional top-level columns to set (e.g., url, archive_url)
    """
    try:
        # Use a transaction for the insert
        with db.conn:
            # Check if document already exists
            existing = list(db[table_name].rows_where("sha1 = ?", [sha1])) if table_name in db.table_names() else []
            if existing and not overwrite:
                logger.debug(f"Document with SHA1 {sha1} already exists in {table_name} (overwrite=False)")
                return
            
            # Prepare document for insertion
            stat_source = Path(file_stat_path) if file_stat_path else Path(file_path)

            # Extract important metadata fields as top-level columns
            # These are commonly queried and should be easily accessible
            important_fields = {
                'original_url', 'source_url', 'url',  # URLs
                'extraction_method', 'processing_method',  # How it was extracted
                'author', 'title',  # Document metadata
                'language',  # Language
            }

            top_level_metadata = {}
            stored_metadata = {}

            for key, value in metadata.items():
                # Convert to string to handle BeautifulSoup NavigableString and other non-JSON types
                str_value = str(value) if value is not None else None
                stored_metadata[key] = str_value

                if key in important_fields and str_value is not None:
                    top_level_metadata[key] = str_value

            document = {
                "sha1": sha1,
                "filename": os.path.basename(file_path),
                "filepath": os.path.abspath(file_path),  # Store absolute path
                "raw_content": content,  # Single content field
                "file_created": datetime.fromtimestamp(stat_source.stat().st_ctime).isoformat(),
                "file_modified": datetime.fromtimestamp(stat_source.stat().st_mtime).isoformat(),
                "updated_at": datetime.now().isoformat(),
                "metadata": json.dumps(stored_metadata) if stored_metadata else None,  # Full metadata record
                **top_level_metadata,  # Spread important fields as columns
            }

            # Add first-class fields if provided
            if labels:
                # Store as JSON array
                document["labels"] = json.dumps(list(labels))
            if json_metadata is not None:
                # Store full JSON sidecar
                document["json_metadata"] = json.dumps(json_metadata)
            if extra_fields:
                for k, v in extra_fields.items():
                    document[k] = v
            
            # Use upsert with alter=True to handle schema changes
            db[table_name].insert(document, alter=True, pk="sha1", replace=True)
            logger.debug(f"Successfully inserted document {sha1} into {table_name}")
    except Exception as e:
        logger.error(f"Error inserting document {sha1}: {str(e)}")
        raise


def check_db_schema(db_path: str, table_name: str) -> bool:
    """
    Check if the database schema matches expected schema.
    Returns True if schema is compatible, False if the schema itself conflicts.
    Raises the underlying exception when the database cannot be opened.
    """
    try:
        db = sqlite_utils.Database(db_path)
        
        # Check if table exists
        if table_name not in db.table_names():
            logger.info(f"Table '{table_name}' does not exist yet - will be created")
            return True
        
        # Get existing columns
        table = db[table_name]
        columns = {col.name for col in table.columns}
        
        # Required columns (updated schema - no more 'content', only 'raw_content')
        required_columns = {'sha1', 'filename', 'filepath', 'raw_content', 'file_created', 'file_modified'}
        
        # Check if all required columns exist
        missing_columns = required_columns - columns
        if missing_columns:
            logger.warning(f"Missing required columns in table '{table_name}': {missing_columns}")
            logger.info("The table will be automatically updated with missing columns")
            return True  # sqlite-utils can handle adding columns with alter=True
        
        # Check primary key
        pkey_cols = [col.name for col in table.columns if col.is_pk]
        if pkey_cols and 'sha1' not in pkey_cols:
            logger.error(f"Table '{table_name}' has wrong primary key: {pkey_cols}")
            logger.error("Expected 'sha1' as primary key")
            return False
        
        return True
        
    except Exception as e:
        logger.error(f"Error checking database schema: {str(e)}")
        raise


def setup_fts(db_path: str, table_name: str):
    """Set up full-text search for the given table if requested"""
    try:
        db = sqlite_utils.Database(db_path)
        
        # Check if table exists
        if table_name not in db.table_names():
            logger.warning(f"Table '{table_name}' does not exist, cannot create FTS")
            return
        
        # Check if FTS already exists
        fts_table_name = f"{table_name}_fts"
        if fts_table_name in db.table_names():
            logger.info(f"FTS table '{fts_table_name}' already exists")
            return
        
        logger.info(f"Creating full-text search index for table '{table_name}'...")

        # Enable FTS5 on raw_content and filename columns
        db[table_name].enable_fts(['raw_content', 'filename'], create_triggers=True)
        
        logger.info(f"Successfully created FTS index '{fts_table_name}'")
        
    except Exception as e:
        logger.error(f"Error setting up FTS: {str(e)}")
        raise


def clean_metadata(metadata: dict) -> dict:
    """
    Clean up the metadata to include only useful information
    """
    # List of metadata keys to keep
    important_keys = {
        # Standard document metadata
        'title', 'author', 'dc:title', 'dc:creator', 'creator', 'keywords', 'subject', 'dc:subject',
        # Date metadata
        'created', 'modified', 'date', 'dcterms:created', 'dcterms:modified', 'Creation-Date', 'Last-Modified',
        # Content metadata
        'Content-Type', 'Content-Length', 'language', 'resourceName', 'original_file_path', 'original_file_type',
        # URL metadata
        'url', 'source', 'Source', 'Message:Raw-Header:Snapshot-Content-Location', 'X-Parsed-By',
        # Custom metadata
        'original_file_path', 'original_file_type',
        'spreadsheet_sheet_count',
        # MHTML-specific metadata
        'original_url', 'source_url', 'save_date', 'mhtml_date', 'mhtml_subject', 'mhtml_subject_decoded',
        'mhtml_from', 'mime_version', 'content_type', 'mhtml_boundary', 'file_type', 'extraction_method',
        'processing_method',
        # OCR-specific metadata
        'ocr_applied', 'ocr_file_path', 'ocr_languages', 'text_quality_issue', 'ocr_attempted', 'ocr_failed'
    }
    
    # Keep only important keys
    cleaned_metadata = {}
    for key, value in metadata.items():
        # Keep exact matches for important keys
        if key in important_keys:
            cleaned_metadata[key] = value
        # Keep keys that contain important substrings (including mhtml_ and ocr_ prefixed keys)
        elif any(important in key.lower() for important in ['date', 'title', 'author', 'creator', 'source', 'url', 'content', 'mhtml_', 'ocr_', 'spreadsheet_']):
            cleaned_metadata[key] = value
    
    logger.debug(f"Cleaned metadata from {len(metadata)} to {len(cleaned_metadata)} fields")
    return cleaned_metadata
