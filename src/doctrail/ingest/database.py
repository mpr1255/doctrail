"""
Database operations for document ingestion.

This module contains functions for creating tables, inserting documents,
and managing database schema for the ingestion process.
"""

import os
import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional, List
import sqlite_utils
from loguru import logger

from .text_processing import sanitize_text_for_storage

logger = logging.getLogger(__name__)

DEFAULT_BUSY_TIMEOUT_MS = 1_000
INSERT_LOCK_RETRY_DELAY_SECONDS = 0.5
INSERT_LOCK_LOG_INTERVAL_SECONDS = 30.0


def _simple_tokenizer_path() -> Path:
    configured = os.environ.get("DOCTRAIL_SQLITE_SIMPLE_EXTENSION")
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())

    module_dir = Path(__file__).resolve().parent
    candidates.extend(
        [
            module_dir / "libsimple.dylib",
            module_dir / "libsimple.so",
            Path.home() / ".claude/skills/sqlite-search/libsimple.dylib",
            Path.home() / ".claude/skills/sqlite-search/libsimple.so",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise RuntimeError(
        "This database contains an FTS5 index using the custom simple tokenizer, "
        "but its SQLite extension is unavailable. Set "
        "DOCTRAIL_SQLITE_SIMPLE_EXTENSION to libsimple.dylib or libsimple.so."
    )


def _load_required_fts_extensions(connection) -> None:
    """Load connection-local extensions required by existing FTS tables."""
    fts_definitions = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND sql LIKE '%USING fts5%'"
    ).fetchall()
    simple_pattern = re.compile(
        r"\btokenize\s*=\s*(['\"]?)simple(?:\s|['\"]|\))",
        re.IGNORECASE,
    )
    if not any(simple_pattern.search(row[0] or "") for row in fts_definitions):
        return

    extension_path = _simple_tokenizer_path()
    try:
        connection.enable_load_extension(True)
        connection.load_extension(str(extension_path))
    except (AttributeError, sqlite3.Error) as exc:
        raise RuntimeError(
            f"Could not load the SQLite simple tokenizer from {extension_path}: {exc}"
        ) from exc
    finally:
        try:
            connection.enable_load_extension(False)
        except AttributeError:
            pass


def _sanitize_storage_value(value):
    if isinstance(value, str):
        return sanitize_text_for_storage(value)
    if isinstance(value, dict):
        return {
            sanitize_text_for_storage(str(key)): _sanitize_storage_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_storage_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_storage_value(item) for item in value)
    return value


def _busy_timeout_ms() -> int:
    raw = os.environ.get("DOCTRAIL_SQLITE_BUSY_TIMEOUT_MS", str(DEFAULT_BUSY_TIMEOUT_MS))
    try:
        timeout_ms = int(raw)
    except ValueError as exc:
        raise ValueError("DOCTRAIL_SQLITE_BUSY_TIMEOUT_MS must be an integer") from exc
    if timeout_ms < 1:
        raise ValueError("DOCTRAIL_SQLITE_BUSY_TIMEOUT_MS must be positive")
    return timeout_ms


def configure_ingest_database(db):
    """Configure one sqlite-utils connection for one writer and many readers."""
    _load_required_fts_extensions(db.conn)
    timeout_ms = _busy_timeout_ms()
    db.execute(f"PRAGMA busy_timeout = {timeout_ms}")
    current_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    if str(current_mode).lower() != "wal":
        current_mode = db.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(current_mode).lower() != "wal":
        raise RuntimeError(f"SQLite refused WAL mode; active mode is {current_mode!r}")
    db.execute("PRAGMA synchronous = NORMAL")
    db.execute("PRAGMA wal_autocheckpoint = 1000")
    return db


def ensure_ingest_timestamps(db, table_name: str) -> None:
    """Add timestamp columns and preserve the best known time for legacy rows."""
    if table_name not in db.table_names():
        return
    columns = {column.name for column in db[table_name].columns}
    if "updated_at" not in columns:
        db[table_name].add_column("updated_at", str)
    if "added_at" not in columns:
        db[table_name].add_column("added_at", str)
    quoted_table = '"' + table_name.replace('"', '""') + '"'
    now = datetime.now().isoformat()
    with db.conn:
        db.execute(
            f"UPDATE {quoted_table} "
            "SET added_at = COALESCE(updated_at, ?) "
            "WHERE added_at IS NULL OR added_at = ''",
            [now],
        )


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
    extra_fields: Optional[Dict[str, Any]] = None,
    overwrite: bool = False,
    file_stat_path: Optional[str] = None,
):
    """Insert or replace one ingested document.

    WAL busy waits and interruptible lock retries protect the single writer
    from transient or long-running contention. A writer lock pauses this
    insert until SQLite becomes available; Ctrl-C still interrupts the wait.
    The `sha1` primary key gives replace semantics when overwrite is enabled.

    labels: optional list of labels to store as JSON array in 'labels' column
    json_metadata: optional dict to store as JSON in 'json_metadata' column
    extra_fields: optional flat dict of additional top-level columns to set (e.g., url, archive_url)
    """
    stat_source = Path(file_stat_path) if file_stat_path else Path(file_path)
    content = sanitize_text_for_storage(content)
    important_fields = {
        'original_url', 'source_url', 'url',
        'extraction_method', 'processing_method',
        'author', 'title', 'language',
    }
    stored_metadata = {
        sanitize_text_for_storage(str(key)): (
            sanitize_text_for_storage(str(value)) if value is not None else None
        )
        for key, value in metadata.items()
    }
    top_level_metadata = {
        key: value
        for key, value in stored_metadata.items()
        if key in important_fields and value is not None
    }

    lock_wait_started = None
    next_lock_log = None
    while True:
        try:
            with db.conn:
                existing = (
                    list(db[table_name].rows_where("sha1 = ?", [sha1]))
                    if table_name in db.table_names()
                    else []
                )
                if existing and not overwrite:
                    logger.debug(
                        f"Document with SHA1 {sha1} already exists in "
                        f"{table_name} (overwrite=False)"
                    )
                    return

                now = datetime.now().isoformat()
                existing_row = existing[0] if existing else {}
                added_at = (
                    existing_row.get("added_at")
                    or existing_row.get("updated_at")
                    or now
                )
                document = {
                    "sha1": sha1,
                    "filename": sanitize_text_for_storage(os.path.basename(file_path)),
                    "filepath": os.path.abspath(file_path),
                    "raw_content": content,
                    "file_created": datetime.fromtimestamp(stat_source.stat().st_ctime).isoformat(),
                    "file_modified": datetime.fromtimestamp(stat_source.stat().st_mtime).isoformat(),
                    "added_at": added_at,
                    "updated_at": now,
                    "metadata": json.dumps(stored_metadata) if stored_metadata else None,
                    **top_level_metadata,
                }
                if labels:
                    document["labels"] = json.dumps(_sanitize_storage_value(list(labels)))
                if json_metadata is not None:
                    document["json_metadata"] = json.dumps(_sanitize_storage_value(json_metadata))
                if extra_fields:
                    document.update(_sanitize_storage_value(extra_fields))

                db[table_name].insert(document, alter=True, pk="sha1", replace=True)
                logger.debug(f"Successfully inserted document {sha1} into {table_name}")
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            is_lock_error = "locked" in message or "busy" in message
            if not is_lock_error:
                logger.error(f"Error inserting document {sha1}: {exc}")
                raise
            try:
                db.conn.rollback()
            except sqlite3.Error:
                pass
            now = time.monotonic()
            if lock_wait_started is None:
                lock_wait_started = now
                next_lock_log = now + INSERT_LOCK_LOG_INTERVAL_SECONDS
                logger.warning(
                    f"SQLite is locked while inserting {sha1}; pausing until "
                    "the database writer is available (Ctrl-C to stop)"
                )
            elif next_lock_log is not None and now >= next_lock_log:
                waited = now - lock_wait_started
                logger.warning(
                    f"Still waiting for SQLite writer lock while inserting "
                    f"{sha1} ({waited:.0f}s elapsed)"
                )
                next_lock_log = now + INSERT_LOCK_LOG_INTERVAL_SECONDS
            time.sleep(INSERT_LOCK_RETRY_DELAY_SECONDS)
        except Exception as exc:
            logger.error(f"Error inserting document {sha1}: {exc}")
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

        # A wrong primary key is not an additive schema difference. Check it
        # before missing columns, which sqlite-utils can safely add later.
        pkey_cols = [col.name for col in table.columns if col.is_pk]
        if pkey_cols and 'sha1' not in pkey_cols:
            logger.error(f"Table '{table_name}' has wrong primary key: {pkey_cols}")
            logger.error("Expected 'sha1' as primary key")
            return False
        
        # Required columns (updated schema - no more 'content', only 'raw_content')
        required_columns = {
            'sha1', 'filename', 'filepath', 'raw_content', 'file_created',
            'file_modified', 'added_at', 'updated_at',
        }
        
        # Check if all required columns exist
        missing_columns = required_columns - columns
        if missing_columns:
            logger.warning(f"Missing required columns in table '{table_name}': {missing_columns}")
            logger.info("The table will be automatically updated with missing columns")
            return True  # sqlite-utils can handle adding columns with alter=True
        
        return True
        
    except Exception as e:
        logger.error(f"Error checking database schema: {str(e)}")
        raise


def setup_fts(db_path: str, table_name: str):
    """Set up full-text search for the given table if requested"""
    try:
        db = configure_ingest_database(sqlite_utils.Database(db_path))
        
        # Check if table exists
        if table_name not in db.table_names():
            logger.warning(f"Table '{table_name}' does not exist, cannot create FTS")
            return

        columns = {column.name for column in db[table_name].columns}
        if 'filepath' not in columns:
            db[table_name].add_column('filepath', str)
        
        # Check if FTS already exists
        fts_table_name = f"{table_name}_fts"
        if fts_table_name in db.table_names():
            indexed_columns = {column.name for column in db[fts_table_name].columns}
            required_fts_columns = {'raw_content', 'filename', 'filepath'}
            missing_columns = required_fts_columns - indexed_columns
            if missing_columns:
                raise RuntimeError(
                    f"Existing FTS index '{fts_table_name}' does not index "
                    f"{sorted(missing_columns)}. Rebuild that derived FTS index "
                    "before relying on filepath search."
                )
            logger.info(f"FTS table '{fts_table_name}' already exists")
            return
        
        logger.info(f"Creating full-text search index for table '{table_name}'...")

        # File paths are evidence too: agents need to find documents by folder
        # and path components even when those words are absent from the content.
        db[table_name].enable_fts(
            ['raw_content', 'filename', 'filepath'],
            create_triggers=True,
        )
        
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
