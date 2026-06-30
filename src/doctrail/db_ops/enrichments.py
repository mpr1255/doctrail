import os
import sqlite3
import logging
import click
import time
import threading
import json
import hashlib
import csv
import html
from datetime import datetime
from contextlib import contextmanager
from typing import List, Dict, Optional, Any, Tuple, Iterator, Union, Set

from ..constants import DEFAULT_BUSY_TIMEOUT, MAX_RETRY_ATTEMPTS, DEFAULT_KEY_COLUMN
from ..types import RowDict, RowList, DatabaseUpdate

from .common import *
from .audit_runs import (
    _ensure_enrichment_audit_schema,
    ensure_enrichment_audit_table,
    store_raw_enrichment_response,
)
from .migrations import run_pending_migrations

_BLOB_COLUMNS = {'raw_content', 'content', 'full_text', 'embedding', 'pdf_content'}

# The identity of an enrichment value. One current row per document key, per
# enrichment label, per output field, per model, per prompt version. Enforced
# declaratively by a unique index so writers can use ON CONFLICT upserts and
# concurrent or replayed writes cannot create duplicates.
ENRICHMENT_IDENTITY_COLUMNS = ("key_value", "enrichment_name", "field_name", "model", "prompt_hash")

_ENRICHMENTS_TABLE_DDL = f"""
    CREATE TABLE IF NOT EXISTS {ENRICHMENTS_TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key_value TEXT NOT NULL,
        enrichment_name TEXT NOT NULL,
        field_name TEXT NOT NULL,
        value TEXT,
        value_type TEXT,
        timestamp TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_hash TEXT,
        enrichment_id TEXT,
        run_id TEXT,
        query_hash TEXT,
        metadata TEXT,
        project TEXT
    )
"""

_ENRICHMENT_IDENTITY_INDEX_DDL = f"""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_enrichments_identity
    ON {ENRICHMENTS_TABLE}(key_value, enrichment_name, field_name, model, prompt_hash)
"""


def _create_enrichment_lookup_indexes(cursor) -> None:
    """Create the non-unique lookup indexes used by dedupe planning and views."""
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_enrichments_key_value ON {ENRICHMENTS_TABLE}(key_value)")
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_enrichments_name ON {ENRICHMENTS_TABLE}(enrichment_name)")
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_enrichments_field ON {ENRICHMENTS_TABLE}(field_name)")
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_enrichments_composite ON {ENRICHMENTS_TABLE}(key_value, enrichment_name, field_name)")
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_enrichments_timestamp ON {ENRICHMENTS_TABLE}(timestamp)")
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_enrichments_project ON {ENRICHMENTS_TABLE}(project)")
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_enrichments_run_id ON {ENRICHMENTS_TABLE}(run_id)")
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_enrichments_query_hash ON {ENRICHMENTS_TABLE}(query_hash)")
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_enrichments_prompt_query ON {ENRICHMENTS_TABLE}(enrichment_name, model, prompt_hash, query_hash, key_value)")
    # Serves the pivot/view access pattern: latest value per (enrichment, field, key)
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_enrichments_pivot ON {ENRICHMENTS_TABLE}(enrichment_name, field_name, key_value, timestamp DESC)")


def _migrate_enrichments_identity(cursor) -> None:
    """One-time migration that makes the identity constraint hold on legacy data.

    Guarded by the existence of the identity index: once it exists, this is a
    single sqlite_master probe. On first contact with a pre-constraint database
    it (1) normalizes NULL prompt hashes to '' so identity is total, (2)
    normalizes Python-repr booleans to JSON-canonical 'true'/'false', and (3)
    moves all but the latest row per identity into enrichments_superseded —
    nothing is deleted outright, duplicates are preserved there for recovery.
    """
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_enrichments_identity'"
    )
    if cursor.fetchone():
        return

    cursor.execute(f"UPDATE {ENRICHMENTS_TABLE} SET prompt_hash = '' WHERE prompt_hash IS NULL")
    cursor.execute(f"UPDATE {ENRICHMENTS_TABLE} SET value = 'true' WHERE value_type = 'boolean' AND value = 'True'")
    cursor.execute(f"UPDATE {ENRICHMENTS_TABLE} SET value = 'false' WHERE value_type = 'boolean' AND value = 'False'")

    identity_cols = ", ".join(ENRICHMENT_IDENTITY_COLUMNS)
    cursor.execute(f"""
        SELECT COUNT(*) FROM {ENRICHMENTS_TABLE}
        WHERE id NOT IN (SELECT MAX(id) FROM {ENRICHMENTS_TABLE} GROUP BY {identity_cols})
    """)
    duplicate_count = cursor.fetchone()[0]
    if duplicate_count:
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {ENRICHMENTS_SUPERSEDED_TABLE} AS
            SELECT *, '' AS superseded_at FROM {ENRICHMENTS_TABLE} WHERE 0
        """)
        cursor.execute(f"""
            INSERT INTO {ENRICHMENTS_SUPERSEDED_TABLE}
            SELECT *, datetime('now') FROM {ENRICHMENTS_TABLE}
            WHERE id NOT IN (SELECT MAX(id) FROM {ENRICHMENTS_TABLE} GROUP BY {identity_cols})
        """)
        cursor.execute(f"""
            DELETE FROM {ENRICHMENTS_TABLE}
            WHERE id NOT IN (SELECT MAX(id) FROM {ENRICHMENTS_TABLE} GROUP BY {identity_cols})
        """)
        logging.warning(
            f"Identity migration: moved {duplicate_count} duplicate enrichment row(s) "
            f"to {ENRICHMENTS_SUPERSEDED_TABLE} (latest row per identity kept)"
        )

    cursor.execute(_ENRICHMENT_IDENTITY_INDEX_DDL)


def _ensure_enrichments_schema(cursor) -> None:
    """Create the enrichments table and all its indexes on this connection.

    Single source of truth for the enrichments DDL. Used by
    ensure_enrichments_table for file-backed databases and directly by
    in-memory connections, which cannot rely on a prior ensure call because
    every sqlite3.connect(':memory:') is a brand-new database.
    """
    cursor.execute(_ENRICHMENTS_TABLE_DDL)
    _migrate_enrichments_identity(cursor)
    _create_enrichment_lookup_indexes(cursor)


def _upsert_enrichment_row(
    cursor,
    *,
    key_value: str,
    enrichment_name: str,
    field_name: str,
    value: Any,
    value_type: str,
    timestamp: str,
    model: str,
    prompt_hash: str,
    enrichment_id: Optional[str],
    run_id: Optional[str],
    query_hash: Optional[str],
    metadata_json: Optional[str],
    project: Optional[str],
    overwrite: bool,
) -> bool:
    """Write one enrichment row, with identity enforced by the unique index.

    Append mode inserts only when the identity is new (ON CONFLICT DO NOTHING);
    overwrite mode updates the existing row in place. Returns True when a row
    was inserted or updated.
    """
    params = (
        key_value, enrichment_name, field_name, value, value_type, timestamp,
        model, prompt_hash, enrichment_id, run_id, query_hash, metadata_json, project,
    )
    conflict_action = (
        """DO UPDATE SET
                value = excluded.value,
                value_type = excluded.value_type,
                timestamp = excluded.timestamp,
                enrichment_id = excluded.enrichment_id,
                run_id = excluded.run_id,
                query_hash = excluded.query_hash,
                metadata = excluded.metadata,
                project = excluded.project"""
        if overwrite
        else "DO NOTHING"
    )
    cursor.execute(
        f"""
        INSERT INTO {ENRICHMENTS_TABLE}
        (key_value, enrichment_name, field_name, value, value_type, timestamp,
         model, prompt_hash, enrichment_id, run_id, query_hash, metadata, project)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key_value, enrichment_name, field_name, model, prompt_hash)
        {conflict_action}
        """,
        params,
    )
    return cursor.rowcount > 0


def persist_enrichment_result(
    db_path: str,
    *,
    key_value: str,
    enrichment_name: str,
    updated: Any,
    model: str,
    enrichment_id: Optional[str] = None,
    prompt_id: Optional[str] = None,
    full_prompt: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    query_hash: Optional[str] = None,
    project: Optional[str] = None,
    overwrite: bool = False,
    projection_output_fields: Optional[List[str]] = None,
    raw_json: Optional[str] = None,
    error: Optional[str] = None,
    timestamp: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """Persist one enrichment outcome to both the audit and normalized result ledgers."""
    if conn is None:
        ensure_enrichment_audit_table(db_path)
        ensure_enrichments_table(db_path)

    projection_rows = build_enrichment_projection(
        updated,
        output_fields=projection_output_fields,
    ) if updated is not None or error is None else []
    projection_json = serialize_enrichment_projection(projection_rows) if projection_rows else None
    storage_timestamp = timestamp or datetime.now().isoformat()
    usage = usage or {}

    store_raw_enrichment_response(
        db_path=db_path,
        key_value=key_value,
        enrichment_name=enrichment_name,
        raw_json=_serialize_raw_enrichment_payload(
            updated=updated,
            raw_json=raw_json,
            error=error,
        ),
        model_used=model,
        enrichment_id=enrichment_id,
        prompt_id=prompt_id,
        full_prompt=full_prompt,
        input_tokens=usage.get("input_tokens"),
        cached_input_tokens=usage.get("cached_input_tokens"),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
        output_tokens=usage.get("output_tokens"),
        estimated_cost=usage.get("estimated_cost"),
        run_id=run_id,
        query_hash=query_hash,
        projection_json=projection_json,
        projection_version=ENRICHMENT_PROJECTION_VERSION if projection_rows else None,
        project=project,
        created_at=storage_timestamp,
        conn=conn,
    )

    if projection_rows:
        write_enrichment_projection(
            db_path=db_path,
            key_value=key_value,
            enrichment_name=enrichment_name,
            projection_rows=projection_rows,
            model=model,
            enrichment_id=enrichment_id,
            prompt_hash=prompt_id,
            run_id=run_id,
            query_hash=query_hash,
            overwrite=overwrite,
            project=project,
            timestamp=storage_timestamp,
            conn=conn,
        )

    if conn is not None:
        conn.commit()

    return projection_rows

class EnrichmentRunWriter:
    """Run-scoped enrichment writer backed by one locked SQLite connection."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(
            db_path,
            timeout=DEFAULT_BUSY_TIMEOUT,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={int(DEFAULT_BUSY_TIMEOUT * 1000)}")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-64000")
        with self._lock:
            cursor = self._conn.cursor()
            run_pending_migrations(self._conn)
            self._conn.commit()

    def persist(self, persist_func=None, **kwargs) -> List[Dict[str, Any]]:
        persist_func = persist_func or persist_enrichment_result
        with self._lock:
            if self._closed:
                raise RuntimeError("EnrichmentRunWriter is closed")
            return persist_func(
                self.db_path,
                conn=self._conn,
                **kwargs,
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.commit()
            self._conn.close()
            self._closed = True

def _ensure_prompts_schema(cursor) -> None:
    """Ensure prompts exists using the caller's active connection."""
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {PROMPTS_TABLE} (
            prompt_id TEXT PRIMARY KEY,
            enrichment_name TEXT NOT NULL,
            prompt_text TEXT NOT NULL,
            system_prompt TEXT,
            prompt_hash TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(enrichment_name, prompt_hash)
        )
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_prompts_enrichment
        ON {PROMPTS_TABLE}(enrichment_name)
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_prompts_hash
        ON {PROMPTS_TABLE}(prompt_hash)
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_prompts_created
        ON {PROMPTS_TABLE}(created_at)
    """)


def ensure_prompts_table(db_path: str) -> None:
    """Ensure the prompts table exists for tracking prompt versions."""
    with get_db_connection(db_path) as conn:
        run_pending_migrations(conn)
        conn.commit()
        logging.debug("Ensured prompts table exists")

def compute_prompt_id(enrichment_name: str, prompt_text: str, system_prompt: Optional[str] = None) -> str:
    """Compute the stable prompt id used for prompt-scope deduplication."""
    prompt_content = f"{enrichment_name}|{prompt_text}|{system_prompt or ''}"
    return hashlib.sha256(prompt_content.encode()).hexdigest()

def get_or_create_prompt_id(db_path: str, enrichment_name: str, prompt_text: str,
                           system_prompt: Optional[str] = None, model_used: Optional[str] = None) -> str:
    """Get or create a deterministic prompt_id from prompt content.

    The prompt_id IS the SHA-256 hash of the prompt content. Same prompt text
    always produces the same ID — no random UUIDs. This makes it usable as
    both a deduplication key and a human-readable short identifier (first 8 chars).

    Note: model_used parameter is ignored - prompts are model-agnostic.
    """
    prompt_id = compute_prompt_id(enrichment_name, prompt_text, system_prompt)

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # Run the baseline migration in the same connection so in-memory SQLite
        # paths work correctly during tests and transient workflows.
        run_pending_migrations(conn)

        # Check if this exact prompt already exists
        cursor.execute(f"""
            SELECT prompt_id FROM {PROMPTS_TABLE}
            WHERE enrichment_name = ? AND prompt_hash = ?
        """, (enrichment_name, prompt_id))

        result = cursor.fetchone()
        if result:
            return prompt_id

        # Create new prompt record
        current_time = datetime.now().isoformat()

        cursor.execute(f"""
            INSERT INTO {PROMPTS_TABLE} (prompt_id, enrichment_name, prompt_text, system_prompt,
                               prompt_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (prompt_id, enrichment_name, prompt_text, system_prompt,
              prompt_id, current_time))

        conn.commit()
        logging.debug(f"Created new prompt record: {prompt_id[:8]} for {enrichment_name}")

        return prompt_id

def get_prompt_by_id(db_path: str, prompt_id: str) -> Dict[str, Any]:
    """Retrieve prompt details by prompt_id."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row
        
        cursor.execute(f"SELECT * FROM {PROMPTS_TABLE} WHERE prompt_id = ?", (prompt_id,))
        result = cursor.fetchone()
        
        if result:
            return dict(result)
        return None

def get_enrichment_prompts_history(db_path: str, enrichment_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get history of all prompts used for an enrichment."""
    # Ensure tables exist
    ensure_prompts_table(db_path)
    ensure_enrichment_audit_table(db_path)

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row

        if enrichment_name:
            cursor.execute(f"""
                SELECT p.*, COUNT(er.id) as usage_count
                FROM {PROMPTS_TABLE} p
                LEFT JOIN {ENRICHMENT_AUDIT_TABLE} er ON p.prompt_id = er.prompt_id
                WHERE p.enrichment_name = ?
                GROUP BY p.prompt_id
                ORDER BY p.created_at DESC
            """, (enrichment_name,))
        else:
            cursor.execute(f"""
                SELECT p.*, COUNT(er.id) as usage_count
                FROM {PROMPTS_TABLE} p
                LEFT JOIN {ENRICHMENT_AUDIT_TABLE} er ON p.prompt_id = er.prompt_id
                GROUP BY p.prompt_id
                ORDER BY p.created_at DESC
            """)

        return [dict(row) for row in cursor.fetchall()]

def _ensure_enrichments_table_schema(cursor) -> None:
    """Ensure enrichments exists using the caller's active connection."""
    cursor.execute(f"PRAGMA table_info({ENRICHMENTS_TABLE})")
    _pre_cols = [row[1] for row in cursor.fetchall()]
    if 'sha1' in _pre_cols and 'key_value' not in _pre_cols:
        cursor.execute(f"ALTER TABLE {ENRICHMENTS_TABLE} RENAME COLUMN sha1 TO key_value")
        logging.info("Migrated enrichments.sha1 → key_value")

    cursor.execute(_ENRICHMENTS_TABLE_DDL)

    # Preserve any orphaned migration table for manual recovery.
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='_enrichments_new'")
    if cursor.fetchone():
        archive_name = f"_enrichments_new_orphaned_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        cursor.execute(
            f"ALTER TABLE {_quote_identifier('_enrichments_new')} "
            f"RENAME TO {_quote_identifier(archive_name)}"
        )
        logging.warning(f"Renamed orphaned _enrichments_new table to {archive_name}")

    for missing_column_ddl in (
        f"ALTER TABLE {ENRICHMENTS_TABLE} ADD COLUMN project TEXT",
        f"ALTER TABLE {ENRICHMENTS_TABLE} ADD COLUMN run_id TEXT",
        f"ALTER TABLE {ENRICHMENTS_TABLE} ADD COLUMN query_hash TEXT",
    ):
        try:
            cursor.execute(missing_column_ddl)
        except sqlite3.OperationalError:
            pass

    _migrate_enrichments_identity(cursor)
    _create_enrichment_lookup_indexes(cursor)
    logging.debug("Ensured enrichments table exists")

def ensure_enrichments_table(db_path: str) -> None:
    """
    Create enrichments table if it doesn't exist.

    This table stores ALL enrichment results in a normalized format:
    - One row per enrichment field per document
    - Tracks timestamp, model, and enrichment metadata
    - Allows multiple models to enrich same document
    - Preserves enrichment history
    """
    with get_db_connection(db_path) as conn:
        run_pending_migrations(conn)
        conn.commit()

def get_successfully_enriched_keys(
    db_path: str,
    *,
    key_values: List[str],
    enrichment_name: str,
    model: str,
    prompt_id: Optional[str],
    query_hash: Optional[str] = None,
) -> Set[str]:
    """Return keys that already have a successful enrichment for the given scope.

    Success is defined by normalized output existing in `enrichments`. For
    backward compatibility, audit rows with a stored projection also count as a
    successful prior result.
    """
    if not key_values:
        return set()

    successful_keys: Set[str] = set()
    unique_keys = [str(key) for key in dict.fromkeys(key_values) if key is not None]
    chunk_size = 900

    enrichments_scope_clause = "AND query_hash = ?" if query_hash else ""
    audit_scope_clause = "AND query_hash = ?" if query_hash else ""

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        if db_path == ":memory:":
            _ensure_enrichments_schema(cursor)
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {ENRICHMENT_AUDIT_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    enrichment_id TEXT UNIQUE,
                    key_value TEXT NOT NULL,
                    enrichment_name TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    projection_json TEXT,
                    projection_version TEXT,
                    model_used TEXT NOT NULL,
                    prompt_id TEXT,
                    full_prompt TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    estimated_cost REAL,
                    run_id TEXT,
                    query_hash TEXT,
                    project TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
        else:
            ensure_enrichments_table(db_path)
            ensure_enrichment_audit_table(db_path)

        for start in range(0, len(unique_keys), chunk_size):
            chunk = unique_keys[start:start + chunk_size]
            placeholders = ",".join("?" * len(chunk))

            enrichment_params: List[Any] = [
                enrichment_name,
                model,
                prompt_id or '',
            ]
            if query_hash:
                enrichment_params.append(query_hash)
            enrichment_params.extend(chunk)

            cursor.execute(
                f"""
                SELECT DISTINCT key_value
                FROM {ENRICHMENTS_TABLE}
                WHERE enrichment_name = ?
                  AND model = ?
                  AND prompt_hash = ?
                  {enrichments_scope_clause}
                  AND key_value IN ({placeholders})
                """,
                enrichment_params,
            )
            successful_keys.update(str(row[0]) for row in cursor.fetchall())

            audit_params: List[Any] = [
                enrichment_name,
                model,
                prompt_id,
            ]
            if query_hash:
                audit_params.append(query_hash)
            audit_params.extend(chunk)

            cursor.execute(
                f"""
                SELECT DISTINCT key_value
                FROM {ENRICHMENT_AUDIT_TABLE}
                WHERE enrichment_name = ?
                  AND model_used = ?
                  AND prompt_id = ?
                  AND projection_json IS NOT NULL
                  {audit_scope_clause}
                  AND key_value IN ({placeholders})
                """,
                audit_params,
            )
            successful_keys.update(str(row[0]) for row in cursor.fetchall())

    return successful_keys


def get_keys_with_complete_enrichment_fields(
    db_path: str,
    *,
    key_values: List[str],
    enrichment_name: str,
    field_names: List[str],
) -> Set[str]:
    """Return keys with every requested field for an enrichment name.

    This scope deliberately ignores model, prompt_hash, and query_hash so edited
    prompts, legacy NULL prompt hashes, and model aliases still count as the
    same enrichment identity.
    """
    if not key_values or not field_names:
        return set()

    unique_keys = [str(key) for key in dict.fromkeys(key_values) if key is not None]
    unique_fields = [str(field) for field in dict.fromkeys(field_names) if field]
    if not unique_keys or not unique_fields:
        return set()

    complete_keys: Set[str] = set()
    chunk_size = 450

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        if db_path == ":memory:":
            _ensure_enrichments_schema(cursor)
        else:
            ensure_enrichments_table(db_path)

        field_placeholders = ",".join("?" for _ in unique_fields)
        required_field_count = len(unique_fields)

        for start in range(0, len(unique_keys), chunk_size):
            chunk = unique_keys[start:start + chunk_size]
            key_placeholders = ",".join("?" for _ in chunk)
            cursor.execute(
                f"""
                SELECT key_value
                FROM {ENRICHMENTS_TABLE}
                WHERE enrichment_name = ?
                  AND field_name IN ({field_placeholders})
                  AND key_value IN ({key_placeholders})
                GROUP BY key_value
                HAVING COUNT(DISTINCT field_name) = ?
                """,
                [
                    enrichment_name,
                    *unique_fields,
                    *chunk,
                    required_field_count,
                ],
            )
            complete_keys.update(str(row[0]) for row in cursor.fetchall())

    return complete_keys


def plan_existing_enrichment_skips(
    db_path: str,
    *,
    rows: List[Dict[str, Any]],
    enrichment_name: str,
    model: str,
    prompt_id: str,
    key_column: str,
    dedupe_scope: str,
    query_hash: Optional[str],
    output_table: Optional[str],
    output_cols: Optional[List[str]],
    separate_output_db: bool,
    source_table: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return rows that append mode should skip before calling the provider."""
    if dedupe_scope == "name":
        dedupe_scope = "enrichment"
    use_query_scope = dedupe_scope == "query" and bool(query_hash)
    candidate_keys = [
        str(row.get(key_column))
        for row in rows
        if row.get(key_column) not in (None, "NO_KEY")
    ]
    if dedupe_scope == "enrichment":
        successful_keys = get_keys_with_complete_enrichment_fields(
            db_path,
            key_values=candidate_keys,
            enrichment_name=enrichment_name,
            field_names=output_cols or [],
        )
    else:
        successful_keys = get_successfully_enriched_keys(
            db_path,
            key_values=candidate_keys,
            enrichment_name=enrichment_name,
            model=model,
            prompt_id=prompt_id,
            query_hash=query_hash if use_query_scope else None,
        )

    logging.debug(
        f"Checking skip logic for enrichment '{enrichment_name}' with model '{model}' "
        f"prompt_id={prompt_id[:8]} found {len(successful_keys)} successful prior result(s)"
    )

    skipped_rows: List[Dict[str, Any]] = []
    for row in rows:
        key_value = row.get(key_column, "NO_KEY")
        key_text = str(key_value) if key_value != "NO_KEY" else "NO_KEY"
        kv_prefix = _key_prefix(key_value)
        if key_text in successful_keys:
            logging.debug(f"Found successful existing enrichment for {kv_prefix}")
            skipped_rows.append({
                "rowid": row.get("rowid", "NO_ROWID"),
                "key_value": key_value,
                "original": "already processed (successful enrichment)",
                "updated": None,
            })
        else:
            logging.debug(f"No successful existing enrichment for {kv_prefix}")

    if dedupe_scope != "prompt":
        return skipped_rows

    # Prompt scope also honours legacy direct-column outputs: when an
    # enrichment writes into a source-table column, a non-empty value there
    # means the row was already processed.
    output_col = output_cols[0] if output_cols else None
    if not output_col or output_table:
        return skipped_rows

    existing_direct_values: Dict[str, Any] = {}
    if source_table and not separate_output_db:
        keys_to_check = [
            row.get(key_column)
            for row in rows
            if row.get(key_column) not in (None, "NO_KEY")
        ]
        if keys_to_check:
            with get_db_connection(db_path) as conn:
                cursor = conn.cursor()
                table_ref = _quote_identifier(source_table, "source table")
                key_column_ref = _quote_identifier(key_column, "key column")
                output_col_ref = _quote_identifier(output_col, "output column")
                for start in range(0, len(keys_to_check), 900):
                    chunk = keys_to_check[start:start + 900]
                    placeholders = ",".join("?" for _ in chunk)
                    try:
                        cursor.execute(
                            f"SELECT {key_column_ref}, {output_col_ref} "
                            f"FROM {table_ref} WHERE {key_column_ref} IN ({placeholders})",
                            chunk,
                        )
                    except sqlite3.Error:
                        logging.debug(
                            f"Could not inspect direct-column output {source_table}.{output_col}",
                            exc_info=True,
                        )
                        existing_direct_values = {}
                        break
                    existing_direct_values.update({
                        str(row[0]): row[1]
                        for row in cursor.fetchall()
                    })

    skipped_keys = {row["key_value"] for row in skipped_rows}
    for row in rows:
        key_value = row.get(key_column, "NO_KEY")
        if key_value in skipped_keys:
            continue
        existing_value = existing_direct_values.get(str(key_value), row.get(output_col))
        if existing_value is not None and existing_value != "":
            kv_prefix = _key_prefix(key_value)
            logging.debug(f"Row {kv_prefix} already has data in {output_col}")
            skipped_rows.append({
                "rowid": row.get("rowid", "NO_ROWID"),
                "key_value": key_value,
                "original": f"already has {output_col}",
                "updated": None,
            })

    return skipped_rows

def filter_unskipped_input_rows(
    rows: List[Dict[str, Any]],
    skipped_rows: List[Dict[str, Any]],
    *,
    key_column: str,
) -> List[Dict[str, Any]]:
    """Filter already-skipped rows out of an input rowset."""
    skipped_ids = {
        (row.get("rowid", "NO_ROWID"), row.get("key_value", "NO_KEY"))
        for row in skipped_rows
    }
    return [
        row
        for row in rows
        if (row.get("rowid", "NO_ROWID"), row.get(key_column, "NO_KEY")) not in skipped_ids
    ]

def store_source_db_path(output_db_path: str, source_db_path: str) -> None:
    """Store the source database path in the output db's metadata table.

    This allows future connections to auto-ATTACH the source for cross-db views.
    Stores the absolute path so it works regardless of cwd.
    """
    abs_source = os.path.abspath(source_db_path)
    with get_db_connection(output_db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS _doctrail_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cursor.execute("""
            INSERT OR REPLACE INTO _doctrail_meta (key, value) VALUES ('source_db_path', ?)
        """, (abs_source,))
        conn.commit()

def get_source_db_path(db_path: str) -> Optional[str]:
    """Retrieve the source database path from the metadata table, if it exists."""
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='_doctrail_meta'")
            if not cursor.fetchone():
                return None
            cursor.execute("SELECT value FROM _doctrail_meta WHERE key = 'source_db_path'")
            row = cursor.fetchone()
            return row[0] if row else None
    except Exception:
        return None

def attach_source_db(conn: 'sqlite3.Connection', source_db_path: str, alias: str = '_source') -> None:
    """ATTACH a source database to an existing connection.

    After this call, tables in source_db_path are accessible as {alias}.{table}.
    """
    abs_path = os.path.abspath(source_db_path)
    conn.execute(f"ATTACH DATABASE ? AS {_quote_identifier(alias, 'database alias')}", (abs_path,))

def copy_source_metadata(
    output_db_path: str,
    source_db_path: str,
    source_table: str = 'documents',
) -> str:
    """Copy lightweight document metadata from source db into output db.

    Creates a _source_{source_table} table in the output db with all columns
    EXCEPT large blob columns (raw_content, content, embedding, etc.).
    This allows views in the output db to work standalone without ATTACH.

    Returns the name of the created metadata table.
    """
    meta_table = f"_source_{source_table}"
    source_table_ref = _quote_identifier(source_table, "source table")
    meta_table_ref = _quote_identifier(meta_table, "metadata table")

    with get_db_connection(source_db_path) as src_conn:
        src_cursor = src_conn.cursor()

        # Get source table columns
        src_cursor.execute(f"PRAGMA table_info({source_table_ref})")
        all_cols = [(row[1], row[2]) for row in src_cursor.fetchall()]  # (name, type)

        if not all_cols:
            logging.warning(f"Source table '{source_table}' has no columns")
            return meta_table

        # Filter out blob columns
        keep_cols = [(name, typ) for name, typ in all_cols if name not in _BLOB_COLUMNS]
        col_names = [name for name, _ in keep_cols]

        # Read source data (metadata only, no blobs)
        col_list = _quote_identifier_list(col_names, "source column")
        rows = src_cursor.execute(f"SELECT {col_list} FROM {source_table_ref}").fetchall()

    # Write into output db
    with get_db_connection(output_db_path) as out_conn:
        out_cursor = out_conn.cursor()

        # Drop and recreate
        out_cursor.execute(f"DROP TABLE IF EXISTS {meta_table_ref}")

        col_defs = ', '.join(f"{_quote_identifier(name, 'metadata column')} {typ}" for name, typ in keep_cols)
        out_cursor.execute(f"CREATE TABLE {meta_table_ref} ({col_defs})")

        if rows:
            placeholders = ', '.join(['?'] * len(col_names))
            out_cursor.executemany(
                f"INSERT INTO {meta_table_ref} ({col_list}) VALUES ({placeholders})",
                rows
            )

        out_conn.commit()
        logging.info(f"Copied {len(rows)} rows of metadata into {meta_table} ({len(col_names)} columns, excluded blobs)")

    return meta_table

def write_enrichment(
    db_path: str,
    key_value: str,
    enrichment_name: str,
    field_name: str,
    value: Any,
    model: str,
    value_type: Optional[str] = None,
    enrichment_id: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    run_id: Optional[str] = None,
    query_hash: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    overwrite: bool = False,
    project: Optional[str] = None,
    timestamp: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """
    Write a single enrichment result to the enrichments table.

    Args:
        db_path: Path to database
        key_value: Document key value (e.g. sha1 hash, or whatever key_column is)
        enrichment_name: Name of the enrichment task
        field_name: Name of the field being enriched
        value: The enrichment value (will be JSON-encoded if complex)
        model: Model name used (e.g., "gpt-4o-mini")
        value_type: Type of value ('string', 'integer', 'boolean', 'json', 'enum')
        enrichment_id: UUID for this enrichment run
        prompt_hash: Hash of the prompt used
        run_id: Run identifier for this enrichment invocation
        query_hash: Hash of the rendered SQL query used for the run
        metadata: Additional metadata (JSON)
        overwrite: If True, replace existing enrichment for same key_value/name/field/model
        project: Optional project name for filtering (e.g., 'mock_compliance')
    """
    owns_connection = conn is None
    if owns_connection and db_path != ":memory:":
        ensure_enrichments_table(db_path)

    serialized_value, final_value_type = _prepare_enrichment_storage_value(value, value_type=value_type)

    # Serialize metadata if provided
    metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata else None

    current_time = timestamp or datetime.now().isoformat()

    if owns_connection:
        conn_context = get_db_connection(db_path)
        conn = conn_context.__enter__()
    else:
        conn_context = None

    try:
        cursor = conn.cursor()
        if db_path == ":memory:":
            _ensure_enrichments_schema(cursor)

        wrote = _upsert_enrichment_row(
            cursor,
            key_value=key_value,
            enrichment_name=enrichment_name,
            field_name=field_name,
            value=serialized_value,
            value_type=final_value_type,
            timestamp=current_time,
            model=model,
            prompt_hash=prompt_hash or '',
            enrichment_id=enrichment_id,
            run_id=run_id,
            query_hash=query_hash,
            metadata_json=metadata_json,
            project=project,
            overwrite=overwrite,
        )

        key_prefix = _key_prefix(key_value)
        if wrote:
            logging.debug(f"Wrote enrichment for {key_prefix}/{field_name}")
        else:
            logging.debug(f"Enrichment already exists for {key_prefix}/{field_name}, skipping")

        if owns_connection:
            conn.commit()
    finally:
        if conn_context is not None:
            conn_context.__exit__(None, None, None)

def write_enrichment_projection(
    db_path: str,
    key_value: str,
    enrichment_name: str,
    projection_rows: List[Dict[str, Any]],
    model: str,
    enrichment_id: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    run_id: Optional[str] = None,
    query_hash: Optional[str] = None,
    overwrite: bool = False,
    project: Optional[str] = None,
    timestamp: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Write a normalized projection payload into the enrichments table."""
    owns_connection = conn is None
    if owns_connection and db_path != ":memory:":
        ensure_enrichments_table(db_path)
    current_time = timestamp or datetime.now().isoformat()
    written_count = 0

    if owns_connection:
        conn_context = get_db_connection(db_path)
        conn = conn_context.__enter__()
    else:
        conn_context = None

    try:
        cursor = conn.cursor()
        if db_path == ":memory:":
            _ensure_enrichments_schema(cursor)

        key_prefix = _key_prefix(key_value)

        for projection_row in projection_rows:
            field_name = projection_row["field_name"]
            metadata = projection_row.get("metadata")
            metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
            final_value_type = projection_row.get("value_type") or "string"

            wrote = _upsert_enrichment_row(
                cursor,
                key_value=key_value,
                enrichment_name=enrichment_name,
                field_name=field_name,
                value=projection_row.get("value"),
                value_type=final_value_type,
                timestamp=current_time,
                model=model,
                prompt_hash=prompt_hash or '',
                enrichment_id=enrichment_id,
                run_id=run_id,
                query_hash=query_hash,
                metadata_json=metadata_json,
                project=project,
                overwrite=overwrite,
            )

            if wrote:
                written_count += 1
            else:
                logging.debug(f"Projected enrichment already exists for {key_prefix}/{field_name}, skipping")

        if owns_connection:
            conn.commit()
    finally:
        if conn_context is not None:
            conn_context.__exit__(None, None, None)

    return written_count

def rebuild_enrichments_from_audit(
    db_path: str,
    run_id: Optional[str] = None,
    enrichment_name: Optional[str] = None,
    key_value: Optional[str] = None,
    clear_existing: bool = True,
) -> Dict[str, int]:
    """
    Rebuild enrichments from the exact projection payload persisted in enrichment_audit.

    This is intentionally strict: if any matching audit rows predate projection
    persistence, rebuild aborts rather than guessing from raw_json.
    """
    ensure_enrichment_audit_table(db_path)
    ensure_enrichments_table(db_path)

    filters: List[str] = []
    params: List[Any] = []

    if run_id:
        filters.append("run_id = ?")
        params.append(run_id)
    if enrichment_name:
        filters.append("enrichment_name = ?")
        params.append(enrichment_name)
    if key_value is not None:
        filters.append("key_value = ?")
        params.append(key_value)

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    missing_where = f"{where_clause} {'AND' if where_clause else 'WHERE'} COALESCE(projection_json, '') = ''"

    with get_db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        missing_projection_count = cursor.execute(
            f"SELECT COUNT(*) FROM {ENRICHMENT_AUDIT_TABLE} {missing_where}",
            params,
        ).fetchone()[0]

        if missing_projection_count:
            raise ValueError(
                f"Cannot rebuild exactly: {missing_projection_count} matching audit rows are missing projection_json. "
                "These rows predate the guaranteed projection contract."
            )

        audit_rows = cursor.execute(
            f"""
            SELECT key_value, enrichment_name, model_used, prompt_id, enrichment_id,
                   run_id, query_hash, project, created_at, projection_json
            FROM {ENRICHMENT_AUDIT_TABLE}
            {where_clause}
            ORDER BY created_at, id
            """,
            params,
        ).fetchall()

        if clear_existing:
            if where_clause:
                cursor.execute(f"DELETE FROM {ENRICHMENTS_TABLE} {where_clause}", params)
            else:
                cursor.execute(f"DELETE FROM {ENRICHMENTS_TABLE}")

        written_count = 0
        for audit_row in audit_rows:
            projection_rows = parse_enrichment_projection(audit_row["projection_json"])
            for projection_row in projection_rows:
                metadata = projection_row.get("metadata")
                metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
                cursor.execute(f"""
                    INSERT INTO {ENRICHMENTS_TABLE}
                    (key_value, enrichment_name, field_name, value, value_type, timestamp,
                     model, prompt_hash, enrichment_id, run_id, query_hash, metadata, project)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    audit_row["key_value"],
                    audit_row["enrichment_name"],
                    projection_row["field_name"],
                    projection_row.get("value"),
                    projection_row.get("value_type") or "string",
                    audit_row["created_at"],
                    audit_row["model_used"],
                    audit_row["prompt_id"],
                    audit_row["enrichment_id"],
                    audit_row["run_id"],
                    audit_row["query_hash"],
                    metadata_json,
                    audit_row["project"],
                ))
                written_count += 1

        conn.commit()

    return {
        "audit_rows": len(audit_rows),
        "written_rows": written_count,
        "missing_projection_rows": 0,
    }

def get_enrichments(
    db_path: str,
    key_value: Optional[str] = None,
    enrichment_name: Optional[str] = None,
    field_name: Optional[str] = None,
    model: Optional[str] = None,
    latest_only: bool = True
) -> List[Dict[str, Any]]:
    """
    Retrieve enrichments from the enrichments table.

    Args:
        db_path: Path to database
        key_value: Filter by document key value
        enrichment_name: Filter by enrichment name
        field_name: Filter by field name
        model: Filter by model
        latest_only: If True, return only the most recent enrichment for each key_value/field combo

    Returns:
        List of enrichment records as dictionaries
    """
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row

        # Build query
        if latest_only:
            query = f"""
                SELECT e.* FROM {ENRICHMENTS_TABLE} e
                INNER JOIN (
                    SELECT key_value, field_name, MAX(timestamp) as max_timestamp
                    FROM {ENRICHMENTS_TABLE}
                    WHERE 1=1
            """
            params = []

            if key_value is not None:
                query += " AND key_value = ?"
                params.append(key_value)
            if enrichment_name:
                query += " AND enrichment_name = ?"
                params.append(enrichment_name)
            if field_name:
                query += " AND field_name = ?"
                params.append(field_name)
            if model:
                query += " AND model = ?"
                params.append(model)

            query += """
                    GROUP BY key_value, field_name
                ) latest ON e.key_value = latest.key_value
                       AND e.field_name = latest.field_name
                       AND e.timestamp = latest.max_timestamp
                ORDER BY e.timestamp DESC
            """
        else:
            query = f"SELECT * FROM {ENRICHMENTS_TABLE} WHERE 1=1"
            params = []

            if key_value is not None:
                query += " AND key_value = ?"
                params.append(key_value)
            if enrichment_name:
                query += " AND enrichment_name = ?"
                params.append(enrichment_name)
            if field_name:
                query += " AND field_name = ?"
                params.append(field_name)
            if model:
                query += " AND model = ?"
                params.append(model)

            query += " ORDER BY timestamp DESC"

        cursor.execute(query, params)
        results = [dict(row) for row in cursor.fetchall()]

        # Parse JSON values and metadata
        for result in results:
            if result.get('value_type') == 'json' and result.get('value'):
                try:
                    result['parsed_value'] = json.loads(result['value'])
                except json.JSONDecodeError:
                    result['parsed_value'] = result['value']
            else:
                result['parsed_value'] = result['value']

            if result.get('metadata'):
                try:
                    result['parsed_metadata'] = json.loads(result['metadata'])
                except json.JSONDecodeError:
                    result['parsed_metadata'] = None

        return results

def create_enrichments_views(
    db_path: str,
    source_table: str = "documents",
    priority_columns: Optional[List[str]] = None,
    source_db_path: Optional[str] = None,
    key_column: str = 'sha1',
) -> None:
    """
    Create helper views for easier querying of enrichments.

    Creates:
    - {source_table}_enriched: Source table with latest enrichment values as columns

    Args:
        db_path: Path to database (where enrichments live and view is created)
        source_table: Table to create view from (default: 'documents')
        priority_columns: List of column names to show first (optional, has sensible defaults)
        source_db_path: If enrichments are in a separate db from source documents,
            pass the path to the source db here. The source will be ATTACHed as _source.
        key_column: Column in source table that links to enrichments.sha1 (default: 'sha1').
    """
    # Default priority columns if not specified
    if priority_columns is None:
        priority_columns = [
            'bibtex_key', 'bibtex', 'title', 'authors', 'year',
            'publication_title', 'doi', 'abstract'
        ]
    ensure_enrichments_table(db_path)

    # When source db differs, copy metadata and use the local copy for views
    # (SQLite views cannot reference ATTACHed databases)
    cross_db = source_db_path and os.path.abspath(source_db_path) != os.path.abspath(db_path)
    if cross_db:
        meta_table = copy_source_metadata(db_path, source_db_path, source_table)
        table_ref = meta_table
    else:
        table_ref = source_table
    table_ref_quoted = _quote_identifier(table_ref, "source table")

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # Check if source object exists (table or view)
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
            (table_ref,),
        )
        if not cursor.fetchone():
            logging.warning(f"Source '{table_ref}' not found, skipping view creation")
            return

        # Detect which sha1 column the table uses
        cursor.execute(f"PRAGMA table_info({table_ref_quoted})")
        columns = [row[1] for row in cursor.fetchall()]

        if key_column in columns:
            sha1_col = key_column
        elif 'sha1' in columns:
            sha1_col = 'sha1'
        elif 'attachment_sha1' in columns:
            sha1_col = 'attachment_sha1'
        else:
            logging.warning(f"Table '{source_table}' has no key column (tried {key_column}, sha1, attachment_sha1), skipping enriched view creation")
            return
        sha1_col_ref = _quote_identifier(sha1_col, "key column")

        # Each (enrichment, field) pair gets its own column so two enrichments
        # that share a field name (e.g. both output "summary") never bleed
        # into each other's view column.
        cursor.execute(f"""
            SELECT DISTINCT enrichment_name, field_name
            FROM {ENRICHMENTS_TABLE}
            ORDER BY enrichment_name, field_name
        """)
        enrichment_fields = [(row[0], row[1]) for row in cursor.fetchall()]

        if not enrichment_fields:
            logging.debug("No enrichments yet, skipping view creation")
            return

        # Alias is the bare field name when globally unique, otherwise
        # prefixed with the enrichment name to disambiguate.
        field_counts: Dict[str, int] = {}
        for _, field in enrichment_fields:
            field_counts[field] = field_counts.get(field, 0) + 1

        subqueries = []
        aliases = []
        for enrichment, field in enrichment_fields:
            alias = field if field_counts[field] == 1 else f"{enrichment}_{field}"
            safe_alias = _sanitize_sql_name(alias)
            aliases.append(safe_alias)
            subqueries.append(f"""
                (SELECT value FROM {ENRICHMENTS_TABLE}
                 WHERE key_value = d.{sha1_col_ref}
                   AND enrichment_name = {_sql_literal(enrichment)}
                   AND field_name = {_sql_literal(field)}
                 ORDER BY timestamp DESC, id DESC LIMIT 1) as {_quote_identifier(safe_alias, 'column alias')}
            """)

        view_name = _doctrail_view_name(f"{source_table}_enriched")
        view_ref = _quote_identifier(view_name, "view name")

        # Build column list: priority cols first, then enrichments, then remaining cols
        safe_field_names = set(aliases)
        priority_select = []
        remaining_cols = []

        for col in columns:
            safe_col = _sanitize_sql_name(col)
            col_ref = _quote_identifier(col, "source column")
            collides = safe_col in safe_field_names and col != sha1_col
            if col in priority_columns:
                if collides:
                    priority_select.append(
                        f'd.{col_ref} as {_quote_identifier(f"{safe_col}_input", "column alias")}'
                    )
                else:
                    priority_select.append(f'd.{col_ref}')
            else:
                if collides:
                    remaining_cols.append(
                        f'd.{col_ref} as {_quote_identifier(f"{safe_col}_input", "column alias")}'
                    )
                else:
                    remaining_cols.append(f'd.{col_ref}')

        # Sort priority columns in the defined order
        def _priority_sort_key(expr):
            # Extract column name from 'd.col' or 'd.col as col_input'
            col_part = expr.split(' as ')[0][2:]  # strip 'd.' prefix
            return priority_columns.index(col_part) if col_part in priority_columns else 999
        priority_select.sort(key=_priority_sort_key)

        # Combine: priority cols + enrichments + remaining cols
        all_selects = priority_select + subqueries + remaining_cols

        # Create or replace the view
        view_sql = f"""
            CREATE VIEW IF NOT EXISTS {view_ref} AS
            SELECT
                {','.join(all_selects)}
            FROM {table_ref_quoted} d
        """

        # Drop existing view if it exists
        cursor.execute(f"DROP VIEW IF EXISTS {view_ref}")
        cursor.execute(view_sql)

        conn.commit()
        logging.info(f"Created {view_name} view with {len(enrichment_fields)} enrichment fields (using {sha1_col})")

def migrate_columns_to_enrichments(
    db_path: str,
    source_table: str = "documents",
    exclude_columns: Optional[List[str]] = None,
    key_column: Optional[str] = None,
) -> int:
    """
    Migrate enrichment columns from a table to the enrichments table.

    This is useful for migrating from the old column-per-enrichment pattern
    to the new centralized enrichments table.

    Args:
        db_path: Path to database
        source_table: Table to migrate from (default: documents)
        exclude_columns: Columns to skip (core document fields)
        key_column: Source-table key column. If omitted, detect sha1, attachment_sha1,
            or the table primary key.

    Returns:
        Number of enrichments migrated
    """
    if exclude_columns is None:
        # Default core columns to exclude
        exclude_columns = {
            'sha1', 'filename', 'filepath', 'content', 'raw_content',
            'file_created', 'file_modified', 'updated_at', 'created_at',
            'rowid', 'id'
        }
        # Also exclude metadata columns
        exclude_columns.update({
            col for col in []
            if col.startswith('metadata_')
        })
    else:
        exclude_columns = set(exclude_columns)

    ensure_enrichments_table(db_path)

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # Get all columns from source table
        source_table_ref = _quote_identifier(source_table, "source table")
        cursor.execute(f"PRAGMA table_info({source_table_ref})")
        table_info = cursor.fetchall()
        all_columns = [row[1] for row in table_info]

        resolved_key_column = None
        if key_column and key_column in all_columns:
            resolved_key_column = key_column
        elif 'sha1' in all_columns:
            resolved_key_column = 'sha1'
        elif 'attachment_sha1' in all_columns:
            resolved_key_column = 'attachment_sha1'
        else:
            pk_columns = [row[1] for row in table_info if row[5]]
            if pk_columns:
                resolved_key_column = pk_columns[0]

        if not resolved_key_column:
            raise ValueError(
                f"Table '{source_table}' has no detectable key column; "
                "pass key_column explicitly to migrate_columns_to_enrichments"
            )

        # Filter to enrichment columns only
        enrichment_columns = [col for col in all_columns
                             if col not in exclude_columns
                             and col != resolved_key_column
                             and not col.startswith('metadata_')]

        if not enrichment_columns:
            logging.info("No enrichment columns found to migrate")
            return 0

        logging.info(f"Migrating {len(enrichment_columns)} enrichment columns: {enrichment_columns}")

        migrated_count = 0

        for column in enrichment_columns:
            column_ref = _quote_identifier(column, "source column")
            key_column_ref = _quote_identifier(resolved_key_column, "key column")
            # Get all non-null values for this column
            cursor.execute(f"""
                SELECT {key_column_ref}, {column_ref}
                FROM {source_table_ref}
                WHERE {column_ref} IS NOT NULL AND {column_ref} != ''
            """)

            rows = cursor.fetchall()

            for key_val, value in rows:
                # Write to enrichments table
                write_enrichment(
                    db_path=db_path,
                    key_value=key_val,
                    enrichment_name=column,  # Use column name as enrichment name
                    field_name=column,
                    value=value,
                    model='unknown',  # We don't know which model was used
                    value_type='string',
                    overwrite=False  # Don't overwrite if already migrated
                )
                migrated_count += 1

        conn.commit()
        logging.info(f"Migrated {migrated_count} enrichments to enrichments table")

        return migrated_count

def create_project_view(
    db_path: str,
    project: str,
    documents_table: str = 'documents',
    key_column: str = 'sha1',
    priority_columns: Optional[List[str]] = None,
    source_db_path: Optional[str] = None
) -> str:
    """
    Create or replace a SQLite view for enrichments filtered by project.

    The view pivots enrichment values into columns and joins with document metadata,
    giving you one row per document with key metadata first, then enrichments, then
    remaining columns.

    Args:
        db_path: Path to database (where enrichments live and view is created)
        project: Project name to filter by
        documents_table: Name of the documents table to join (default: 'documents')
        key_column: Column name in documents table to join on (default: 'sha1')
        priority_columns: List of column names to show first (optional, has sensible defaults)
        source_db_path: If source documents are in a different db, pass its path here.

    Returns:
        Name of the created view
    """
    # Default priority columns if not specified
    if priority_columns is None:
        priority_columns = [
            'bibtex_key', 'bibtex', 'title', 'authors', 'year',
            'publication_title', 'doi', 'abstract'
        ]
    # Sanitize project name for use in view name (alphanumeric + underscore only)
    safe_project = _sanitize_sql_name(project)
    view_name = _doctrail_view_name(f"enrichments_{safe_project}")
    view_ref = _quote_identifier(view_name, "view name")

    # When source db differs, copy metadata locally
    cross_db = source_db_path and os.path.abspath(source_db_path) != os.path.abspath(db_path)
    if cross_db:
        meta_table = copy_source_metadata(db_path, source_db_path, documents_table)
        table_ref = meta_table
    else:
        table_ref = documents_table
    table_ref_quoted = _quote_identifier(table_ref, "documents table")

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # Get unique key_values for this project
        cursor.execute(f"""
            SELECT DISTINCT key_value FROM {ENRICHMENTS_TABLE} WHERE project = ?
        """, (project,))
        project_keys = [row[0] for row in cursor.fetchall()]

        if not project_keys:
            logging.warning(f"No enrichments found for project '{project}'")
            # Create empty view
            cursor.execute(f"DROP VIEW IF EXISTS {view_ref}")
            cursor.execute(f"""
                CREATE VIEW {view_ref} AS
                SELECT * FROM {ENRICHMENTS_TABLE} WHERE 1=0
            """)
            conn.commit()
            return view_name

        # Get unique field_names for this project (these become columns)
        cursor.execute(f"""
            SELECT DISTINCT field_name FROM {ENRICHMENTS_TABLE}
            WHERE project = ?
            ORDER BY field_name
        """, (project,))
        field_names = [row[0] for row in cursor.fetchall()]

        # Get document table columns (from local table — either original or metadata copy)
        cursor.execute(f"PRAGMA table_info({table_ref_quoted})")
        doc_columns = cursor.fetchall()
        doc_col_names = [col[1] for col in doc_columns] if doc_columns else []

        # Detect key column name in documents table
        if key_column in doc_col_names:
            doc_sha1_col = key_column
        elif 'sha1' in doc_col_names:
            doc_sha1_col = 'sha1'
        elif 'attachment_sha1' in doc_col_names:
            doc_sha1_col = 'attachment_sha1'
        else:
            doc_sha1_col = key_column

        # Build priority and remaining column lists (blobs already excluded in metadata copy)
        doc_sha1_ref = _quote_identifier(doc_sha1_col, "key column")
        safe_field_names = {_sanitize_sql_name(f) for f in field_names}
        exclude_cols = {'raw_content', 'content', 'embedding'}
        priority_select = []
        remaining_select = []

        for col in doc_col_names:
            if col in exclude_cols:
                continue
            col_ref = _quote_identifier(col, "source column")
            safe_col = _sanitize_sql_name(col)
            collides = safe_col in safe_field_names and col != doc_sha1_col
            if col in priority_columns:
                if collides:
                    priority_select.append(
                        f'd.{col_ref} as {_quote_identifier(f"{safe_col}_input", "column alias")}'
                    )
                else:
                    priority_select.append(f'd.{col_ref}')
            else:
                if collides:
                    remaining_select.append(
                        f'd.{col_ref} as {_quote_identifier(f"{safe_col}_input", "column alias")}'
                    )
                else:
                    remaining_select.append(f'd.{col_ref}')

        # Sort priority columns in defined order
        def _priority_sort_key(expr):
            col_part = expr.split(' as ')[0][2:]
            return priority_columns.index(col_part) if col_part in priority_columns else 999
        priority_select.sort(key=_priority_sort_key)

        # Build enrichment subqueries (pivoting field_names to columns)
        enrichment_subqueries = []
        for field in field_names:
            safe_field = _sanitize_sql_name(field)
            enrichment_subqueries.append(f"""
                (SELECT value FROM {ENRICHMENTS_TABLE}
                 WHERE key_value = d.{doc_sha1_ref}
                   AND field_name = {_sql_literal(field)}
                   AND project = {_sql_literal(project)}
                 ORDER BY timestamp DESC LIMIT 1) as {_quote_identifier(safe_field, 'column alias')}
            """)

        # Combine: priority doc cols + enrichments + remaining doc cols
        all_selects = priority_select + enrichment_subqueries + remaining_select

        # Build key filter - need to quote strings for SQL
        quoted_keys = ', '.join([_sql_literal(k) for k in project_keys])

        # Drop and recreate view
        cursor.execute(f"DROP VIEW IF EXISTS {view_ref}")

        view_sql = f"""
            CREATE VIEW {view_ref} AS
            SELECT
                {','.join(all_selects)}
            FROM {table_ref_quoted} d
            WHERE d.{doc_sha1_ref} IN ({quoted_keys})
        """

        cursor.execute(view_sql)
        conn.commit()
        logging.info(f"Created view '{view_name}' for project '{project}' with {len(field_names)} enrichment columns")

    return view_name

__all__ = [

    'persist_enrichment_result',

    'EnrichmentRunWriter',

    'ensure_prompts_table',

    'compute_prompt_id',

    'get_or_create_prompt_id',

    'get_prompt_by_id',

    'get_enrichment_prompts_history',

    'ensure_enrichments_table',

    'get_successfully_enriched_keys',

    'get_keys_with_complete_enrichment_fields',

    'plan_existing_enrichment_skips',

    'filter_unskipped_input_rows',

    'store_source_db_path',

    'get_source_db_path',

    'attach_source_db',

    'copy_source_metadata',

    'write_enrichment',

    'write_enrichment_projection',

    'rebuild_enrichments_from_audit',

    'get_enrichments',

    'create_enrichments_views',

    'migrate_columns_to_enrichments',

    'create_project_view',

]
