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
from .migrations import run_pending_migrations


def _ensure_enrichment_audit_schema(cursor) -> None:
    """Ensure enrichment_audit exists using the caller's active connection."""
    audit_ref = _quote_identifier(ENRICHMENT_AUDIT_TABLE, "table name")
    audit_new_ref = _quote_identifier("_enrichment_audit_new", "table name")

    cursor.execute(f"PRAGMA table_info({audit_ref})")
    _pre_cols = [row[1] for row in cursor.fetchall()]
    if 'sha1' in _pre_cols and 'key_value' not in _pre_cols:
        cursor.execute(f"ALTER TABLE {audit_ref} RENAME COLUMN sha1 TO key_value")
        logging.info("Migrated enrichment_audit.sha1 → key_value")

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {audit_ref} (
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
            cached_input_tokens INTEGER,
            cache_creation_input_tokens INTEGER,
            output_tokens INTEGER,
            estimated_cost REAL,
            project TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute(f"PRAGMA table_info({audit_ref})")
    columns = [info[1] for info in cursor.fetchall()]
    if "enrichment_id" not in columns:
        cursor.execute(f"ALTER TABLE {audit_ref} ADD COLUMN enrichment_id TEXT")
        logging.info("Added enrichment_id column to existing enrichment_audit table")

    if "prompt_id" not in columns:
        cursor.execute(f"ALTER TABLE {audit_ref} ADD COLUMN prompt_id TEXT")
        logging.info("Added prompt_id column to existing enrichment_audit table")

    if "full_prompt" not in columns:
        cursor.execute(f"ALTER TABLE {audit_ref} ADD COLUMN full_prompt TEXT")
        logging.info("Added full_prompt column to existing enrichment_audit table")

    if "input_tokens" not in columns:
        cursor.execute(f"ALTER TABLE {audit_ref} ADD COLUMN input_tokens INTEGER")
        logging.info("Added input_tokens column to existing enrichment_audit table")

    if "cached_input_tokens" not in columns:
        cursor.execute(f"ALTER TABLE {audit_ref} ADD COLUMN cached_input_tokens INTEGER")
        logging.info("Added cached_input_tokens column to existing enrichment_audit table")

    if "cache_creation_input_tokens" not in columns:
        cursor.execute(f"ALTER TABLE {audit_ref} ADD COLUMN cache_creation_input_tokens INTEGER")
        logging.info("Added cache_creation_input_tokens column to existing enrichment_audit table")

    if "output_tokens" not in columns:
        cursor.execute(f"ALTER TABLE {audit_ref} ADD COLUMN output_tokens INTEGER")
        logging.info("Added output_tokens column to existing enrichment_audit table")

    if "estimated_cost" not in columns:
        cursor.execute(f"ALTER TABLE {audit_ref} ADD COLUMN estimated_cost REAL")
        logging.info("Added estimated_cost column to existing enrichment_audit table")

    if "projection_json" not in columns:
        cursor.execute(f"ALTER TABLE {audit_ref} ADD COLUMN projection_json TEXT")
        logging.info("Added projection_json column to existing enrichment_audit table")

    if "projection_version" not in columns:
        cursor.execute(f"ALTER TABLE {audit_ref} ADD COLUMN projection_version TEXT")
        logging.info("Added projection_version column to existing enrichment_audit table")

    if "run_id" not in columns:
        cursor.execute(f"ALTER TABLE {audit_ref} ADD COLUMN run_id TEXT")
        logging.info("Added run_id column to existing enrichment_audit table")

    if "query_hash" not in columns:
        cursor.execute(f"ALTER TABLE {audit_ref} ADD COLUMN query_hash TEXT")
        logging.info("Added query_hash column to existing enrichment_audit table")

    if "project" not in columns:
        cursor.execute(f"ALTER TABLE {audit_ref} ADD COLUMN project TEXT")
        logging.info("Added project column to existing enrichment_audit table")

    cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (ENRICHMENT_AUDIT_TABLE,),
    )
    table_sql = cursor.fetchone()
    if table_sql and ('UNIQUE(sha1, enrichment_name)' in table_sql[0] or
                     'UNIQUE(sha1, enrichment_name, model_used)' in table_sql[0]):
        logging.info("Migrating enrichment_audit table to remove unique constraints")
        cursor.execute(f"""
            CREATE TABLE {audit_new_ref} (
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
                cached_input_tokens INTEGER,
                cache_creation_input_tokens INTEGER,
                output_tokens INTEGER,
                estimated_cost REAL,
                run_id TEXT,
                query_hash TEXT,
                project TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute(f"""
            INSERT INTO {audit_new_ref} (
                id, enrichment_id, key_value, enrichment_name, raw_json,
                projection_json, projection_version, model_used, prompt_id, full_prompt,
                input_tokens, cached_input_tokens, cache_creation_input_tokens,
                output_tokens, estimated_cost, run_id, query_hash, project, created_at
            )
            SELECT
                id, enrichment_id, key_value, enrichment_name, raw_json,
                projection_json, projection_version, model_used, prompt_id, full_prompt,
                input_tokens, NULL, NULL, output_tokens, estimated_cost, run_id,
                query_hash, project, created_at
            FROM {audit_ref}
        """)
        cursor.execute(f"DROP TABLE {audit_ref}")
        cursor.execute(f"ALTER TABLE {audit_new_ref} RENAME TO {audit_ref}")
        logging.info("Migration completed")

    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_audit_key_value
        ON {audit_ref}(key_value)
    """)

    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_audit_enrichment
        ON {audit_ref}(enrichment_name)
    """)

    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_audit_created
        ON {audit_ref}(created_at)
    """)

    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_audit_enrichment_id
        ON {audit_ref}(enrichment_id)
    """)

    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_audit_composite
        ON {audit_ref}(key_value, enrichment_name, model_used)
    """)

    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_audit_run_id
        ON {audit_ref}(run_id)
    """)

    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_audit_query_hash
        ON {audit_ref}(query_hash)
    """)

    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_audit_prompt_query
        ON {audit_ref}(enrichment_name, model_used, prompt_id, query_hash, key_value)
    """)

    logging.debug("Ensured enrichment_audit table exists")

def ensure_enrichment_audit_table(db_path: str) -> None:
    """Ensure the enrichment_audit audit table exists."""
    with get_db_connection(db_path) as conn:
        run_pending_migrations(conn)
        conn.commit()

def ensure_enrichment_runs_table(db_path: str) -> None:
    """Ensure the enrichment_runs table exists."""
    with get_db_connection(db_path) as conn:
        run_pending_migrations(conn)
        conn.commit()

def ensure_enrichment_run_items_table(db_path: str) -> None:
    """Ensure the enrichment_run_items table exists."""
    with get_db_connection(db_path) as conn:
        run_pending_migrations(conn)
        conn.commit()

def ensure_enrichment_overrides_table(db_path: str) -> None:
    """Ensure the enrichment_overrides table exists."""
    with get_db_connection(db_path) as conn:
        run_pending_migrations(conn)
        conn.commit()

def ensure_run_tracking_tables(db_path: str) -> None:
    """Ensure all run-tracking tables exist."""
    ensure_enrichment_runs_table(db_path)
    ensure_enrichment_run_items_table(db_path)
    ensure_enrichment_overrides_table(db_path)

def ensure_enrichment_batch_jobs_table(db_path: str) -> None:
    """Ensure the batch job tracking table exists."""
    with get_db_connection(db_path) as conn:
        run_pending_migrations(conn)
        conn.commit()

def create_enrichment_batch_job(
    db_path: str,
    *,
    run_id: str,
    provider: str,
    endpoint: str,
    model: str,
    input_file_id: str,
    provider_batch_id: Optional[str] = None,
    status: str = "submitted",
    request_count: int = 0,
    input_file_bytes: int = 0,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Insert a batch job record and return it."""
    ensure_enrichment_batch_jobs_table(db_path)
    submitted_at = datetime.now().isoformat()
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            INSERT INTO {ENRICHMENT_BATCH_JOBS_TABLE} (
                run_id, provider, endpoint, model, provider_batch_id, input_file_id,
                status, request_count, input_file_bytes, metadata, submitted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            provider,
            endpoint,
            model,
            provider_batch_id,
            input_file_id,
            status,
            request_count,
            input_file_bytes,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
            submitted_at,
        ))
        job_id = cursor.lastrowid
        conn.commit()
    return get_enrichment_batch_job(db_path, int(job_id))

def get_enrichment_batch_job(db_path: str, job_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single batch job by internal ID."""
    ensure_enrichment_batch_jobs_table(db_path)
    with get_db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT * FROM {ENRICHMENT_BATCH_JOBS_TABLE} WHERE id = ?",
            (job_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

def list_enrichment_batch_jobs(
    db_path: str,
    *,
    run_id: Optional[str] = None,
    statuses: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """List batch jobs, optionally filtered by run or status."""
    ensure_enrichment_batch_jobs_table(db_path)
    with get_db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        query = f"SELECT * FROM {ENRICHMENT_BATCH_JOBS_TABLE} WHERE 1=1"
        params: List[Any] = []
        if run_id:
            query += " AND run_id = ?"
            params.append(run_id)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY submitted_at, id"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

def update_enrichment_batch_job(
    db_path: str,
    job_id: int,
    *,
    provider_batch_id: Optional[str] = None,
    output_file_id: Optional[str] = None,
    error_file_id: Optional[str] = None,
    status: Optional[str] = None,
    completed_count: Optional[int] = None,
    failed_count: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
    completed_at: Optional[str] = None,
    reconciled_at: Optional[str] = None,
) -> None:
    """Update mutable fields for a batch job."""
    ensure_enrichment_batch_jobs_table(db_path)
    set_clauses = ["last_polled_at = ?"]
    params: List[Any] = [datetime.now().isoformat()]

    if provider_batch_id is not None:
        set_clauses.append("provider_batch_id = ?")
        params.append(provider_batch_id)
    if output_file_id is not None:
        set_clauses.append("output_file_id = ?")
        params.append(output_file_id)
    if error_file_id is not None:
        set_clauses.append("error_file_id = ?")
        params.append(error_file_id)
    if status is not None:
        set_clauses.append("status = ?")
        params.append(status)
    if completed_count is not None:
        set_clauses.append("completed_count = ?")
        params.append(completed_count)
    if failed_count is not None:
        set_clauses.append("failed_count = ?")
        params.append(failed_count)
    if metadata is not None:
        set_clauses.append("metadata = ?")
        params.append(json.dumps(metadata, ensure_ascii=False))
    if completed_at is not None:
        set_clauses.append("completed_at = ?")
        params.append(completed_at)
    if reconciled_at is not None:
        set_clauses.append("reconciled_at = ?")
        params.append(reconciled_at)

    params.append(job_id)

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE {ENRICHMENT_BATCH_JOBS_TABLE} SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )
        conn.commit()

def update_enrichment_run_status(
    db_path: str,
    run_id: str,
    status: str,
    *,
    finished_at: Optional[str] = None,
) -> None:
    """Update a run status without touching final counters."""
    ensure_enrichment_runs_table(db_path)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        if finished_at is None:
            cursor.execute(
                f"UPDATE {ENRICHMENT_RUNS_TABLE} SET status = ? WHERE run_id = ?",
                (status, run_id),
            )
        else:
            cursor.execute(
                f"UPDATE {ENRICHMENT_RUNS_TABLE} SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, finished_at, run_id),
            )
        conn.commit()

def create_run_id(
    enrichment_name: str,
    model: str,
    prompt_id: str,
    query_hash: str,
    command_started_at: str,
) -> str:
    """Create a stable run ID for a single enrichment/model invocation."""
    return _hash_text(enrichment_name, model, prompt_id, query_hash, command_started_at)

def create_query_hash(query_sql: str) -> str:
    """Create a stable hash for the fully rendered SQL query."""
    return _hash_text(query_sql)

def start_enrichment_run(
    db_path: str,
    run_id: str,
    command_started_at: str,
    enrichment_name: str,
    model: str,
    prompt_id: str,
    query_sql: str,
    query_hash: str,
    key_column: str,
    source_name: Optional[str],
    dedupe_scope: str = "query",
    project: Optional[str] = None,
    materialized_inputs: bool = True,
    metadata: Optional[Dict[str, Any]] = None,
    status: str = "running",
) -> None:
    """Insert or replace a run record when enrichment processing starts."""
    ensure_enrichment_runs_table(db_path)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute(f"""
            INSERT OR REPLACE INTO {ENRICHMENT_RUNS_TABLE} (
                run_id, command_started_at, started_at, status,
                enrichment_name, model, prompt_id, prompt_hash,
                query_sql, query_hash, dedupe_scope, key_column,
                source_name, materialized_inputs, project, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            command_started_at,
            now,
            status,
            enrichment_name,
            model,
            prompt_id,
            prompt_id,
            query_sql,
            query_hash,
            dedupe_scope,
            key_column,
            source_name,
            1 if materialized_inputs else 0,
            project,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
        ))
        conn.commit()

def finalize_enrichment_run(
    db_path: str,
    run_id: str,
    *,
    status: str,
    total_rows: int,
    processed_rows: int,
    skipped_rows: int,
    insufficient_rows: int,
    success_count: int,
    error_count: int,
    input_tokens: int,
    output_tokens: int,
    estimated_cost: float,
    cached_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> None:
    """Update a run record with final counts and timing."""
    ensure_enrichment_runs_table(db_path)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE {ENRICHMENT_RUNS_TABLE}
            SET finished_at = ?,
                status = ?,
                total_rows = ?,
                processed_rows = ?,
                skipped_rows = ?,
                insufficient_rows = ?,
                success_count = ?,
                error_count = ?,
                input_tokens = ?,
                cached_input_tokens = ?,
                cache_creation_input_tokens = ?,
                output_tokens = ?,
                estimated_cost = ?
            WHERE run_id = ?
        """, (
            datetime.now().isoformat(),
            status,
            total_rows,
            processed_rows,
            skipped_rows,
            insufficient_rows,
            success_count,
            error_count,
            input_tokens,
            cached_input_tokens,
            cache_creation_input_tokens,
            output_tokens,
            estimated_cost,
            run_id,
        ))
        conn.commit()

def materialize_run_inputs(
    db_path: str,
    run_id: str,
    rows: List[Dict[str, Any]],
    key_column: str,
    enabled: bool = True,
) -> None:
    """Persist the exact input rowset used for a run."""
    ensure_enrichment_run_items_table(db_path)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM {ENRICHMENT_RUN_ITEMS_TABLE} WHERE run_id = ?", (run_id,))

        if enabled:
            payload = [
                (
                    run_id,
                    idx,
                    str(row.get(key_column, "")),
                    json.dumps(row, ensure_ascii=False, default=str),
                    "candidate",
                )
                for idx, row in enumerate(rows)
                if row.get(key_column) is not None
            ]
        else:
            payload = [
                (
                    run_id,
                    idx,
                    str(row.get(key_column, "")),
                    None,
                    "candidate",
                )
                for idx, row in enumerate(rows)
                if row.get(key_column) is not None
            ]

        cursor.executemany(f"""
            INSERT INTO {ENRICHMENT_RUN_ITEMS_TABLE} (run_id, row_order, key_value, row_json, status)
            VALUES (?, ?, ?, ?, ?)
        """, payload)
        conn.commit()

def update_run_item_statuses(
    db_path: str,
    run_id: str,
    statuses: Dict[str, str],
) -> None:
    """Update per-row status for a materialized run snapshot."""
    ensure_enrichment_run_items_table(db_path)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.executemany(
            f"""
            UPDATE {ENRICHMENT_RUN_ITEMS_TABLE}
            SET status = ?
            WHERE run_id = ? AND key_value = ?
            """,
            [(status, run_id, key_value) for key_value, status in statuses.items()],
        )
        conn.commit()

def update_run_item_statuses_by_row_order(
    db_path: str,
    run_id: str,
    statuses: Dict[int, str],
) -> None:
    """Update per-row status for a materialized run snapshot by row order."""
    ensure_enrichment_run_items_table(db_path)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.executemany(
            f"""
            UPDATE {ENRICHMENT_RUN_ITEMS_TABLE}
            SET status = ?
            WHERE run_id = ? AND row_order = ?
            """,
            [(status, run_id, row_order) for row_order, status in statuses.items()],
        )
        conn.commit()

def upsert_enrichment_override(
    db_path: str,
    run_id: str,
    key_value: str,
    enrichment_name: str,
    field_name: str,
    override_value: Optional[str],
    reviewer: Optional[str] = None,
    note: Optional[str] = None,
    source: str = "manual",
) -> None:
    """Insert or update a manual override for a specific run output cell."""
    ensure_enrichment_overrides_table(db_path)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute(f"""
            INSERT INTO {ENRICHMENT_OVERRIDES_TABLE} (
                run_id, key_value, enrichment_name, field_name,
                override_value, reviewer, note, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, key_value, enrichment_name, field_name)
            DO UPDATE SET
                override_value = excluded.override_value,
                reviewer = excluded.reviewer,
                note = excluded.note,
                source = excluded.source,
                updated_at = excluded.updated_at
        """, (
            run_id,
            key_value,
            enrichment_name,
            field_name,
            override_value,
            reviewer,
            note,
            source,
            now,
            now,
        ))
        conn.commit()

def delete_enrichment_override(
    db_path: str,
    run_id: str,
    key_value: str,
    enrichment_name: str,
    field_name: str,
) -> None:
    """Delete a manual override for one run output cell."""
    ensure_enrichment_overrides_table(db_path)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            DELETE FROM {ENRICHMENT_OVERRIDES_TABLE}
            WHERE run_id = ? AND key_value = ? AND enrichment_name = ? AND field_name = ?
        """, (run_id, key_value, enrichment_name, field_name))
        conn.commit()

def list_enrichment_runs(
    db_path: str,
    enrichment_name: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """List recent enrichment runs."""
    ensure_enrichment_runs_table(db_path)
    with get_db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        query = f"SELECT * FROM {ENRICHMENT_RUNS_TABLE}"
        params: List[Any] = []
        if enrichment_name:
            query += " WHERE enrichment_name = ?"
            params.append(enrichment_name)
        query += " ORDER BY command_started_at DESC, enrichment_name, model LIMIT ?"
        params.append(limit)
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

def get_enrichment_run(db_path: str, run_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a single run by ID."""
    ensure_enrichment_runs_table(db_path)
    with get_db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {ENRICHMENT_RUNS_TABLE} WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def create_run_summary_view(
    db_path: str,
    run_id: str,
    view_name: Optional[str] = None,
) -> str:
    """Create a one-row view exposing persisted run metadata."""
    ensure_enrichment_runs_table(db_path)
    run = get_enrichment_run(db_path, run_id)
    if not run:
        raise ValueError(f"Run '{run_id}' not found")

    if not view_name:
        started_at = run.get("command_started_at") or run.get("started_at") or datetime.now().isoformat()
        try:
            ts = datetime.fromisoformat(started_at).strftime("%Y%m%d_%H%M")
        except ValueError:
            ts = started_at.replace("-", "").replace(":", "").replace("T", "_")[:13]
        safe_name = ''.join(c if c.isalnum() or c == '_' else '_' for c in run["enrichment_name"])
        view_name = f"run_{safe_name}_{ts}_{run_id[:8]}_summary"
    view_name = _doctrail_view_name(view_name)
    view_ref = _quote_identifier(view_name, "view name")

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"DROP VIEW IF EXISTS {view_ref}")
        cursor.execute(f"""
            CREATE VIEW {view_ref} AS
            SELECT *
            FROM {ENRICHMENT_RUNS_TABLE}
            WHERE run_id = {_sql_literal(run_id)}
        """)
        conn.commit()
    return view_name

def store_raw_enrichment_response(db_path: str, key_value: str, enrichment_name: str,
                                 raw_json: str, model_used: str, enrichment_id: Optional[str] = None,
                                 prompt_id: Optional[str] = None, full_prompt: Optional[str] = None,
                                 input_tokens: Optional[int] = None, output_tokens: Optional[int] = None,
                                 estimated_cost: Optional[float] = None,
                                 cached_input_tokens: Optional[int] = None,
                                 cache_creation_input_tokens: Optional[int] = None,
                                 run_id: Optional[str] = None,
                                 query_hash: Optional[str] = None, projection_json: Optional[str] = None,
                                 projection_version: Optional[str] = None, project: Optional[str] = None,
                                 created_at: Optional[str] = None,
                                 conn: Optional[sqlite3.Connection] = None) -> None:
    """Store raw LLM response in audit table with optional token usage tracking."""
    try:
        owns_connection = conn is None
        if owns_connection:
            conn_context = get_db_connection(db_path)
            conn = conn_context.__enter__()
        else:
            conn_context = None

        try:
            cursor = conn.cursor()
            current_time = created_at or datetime.now().isoformat()
            cursor.execute(f"""
                INSERT OR IGNORE INTO {ENRICHMENT_AUDIT_TABLE}
                (enrichment_id, key_value, enrichment_name, raw_json, projection_json, projection_version,
                 model_used, prompt_id, full_prompt, input_tokens, cached_input_tokens,
                 cache_creation_input_tokens, output_tokens, estimated_cost,
                 run_id, query_hash, project, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                enrichment_id, key_value, enrichment_name, raw_json, projection_json, projection_version,
                model_used, prompt_id, full_prompt, input_tokens, cached_input_tokens,
                cache_creation_input_tokens, output_tokens, estimated_cost, run_id, query_hash,
                project, current_time
            ))

            if owns_connection:
                conn.commit()

            cost_str = f", cost=${estimated_cost:.4f}" if estimated_cost else ""
            tokens_str = f", tokens={input_tokens}+{output_tokens}" if input_tokens else ""
            key_prefix = _key_prefix(key_value)
            logging.debug(f"Stored raw response for {enrichment_name} on {key_prefix}{tokens_str}{cost_str}")
        finally:
            if conn_context is not None:
                conn_context.__exit__(None, None, None)

    except sqlite3.Error as e:
        logging.error(f"Error storing raw enrichment response: {e}")
        raise

def get_enrichment_response_history(db_path: str, key_value: Optional[str] = None,
                                   enrichment_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieve enrichment response history for debugging/audit."""
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()

            # Build query based on filters
            query = f"SELECT * FROM {ENRICHMENT_AUDIT_TABLE} WHERE 1=1"
            params = []

            if key_value is not None:
                query += " AND key_value = ?"
                params.append(key_value)

            if enrichment_name:
                query += " AND enrichment_name = ?"
                params.append(enrichment_name)
                
            query += " ORDER BY created_at DESC"
            
            cursor.execute(query, params)
            
            columns = [desc[0] for desc in cursor.description]
            results = []
            for row in cursor.fetchall():
                result = dict(zip(columns, row))
                # Parse raw_json back to dict for easier inspection
                try:
                    result['parsed_json'] = json.loads(result['raw_json'])
                except json.JSONDecodeError:
                    result['parsed_json'] = None
                try:
                    projection_json = result.get('projection_json')
                    result['parsed_projection'] = parse_enrichment_projection(projection_json) if projection_json else []
                except ValueError:
                    result['parsed_projection'] = None
                results.append(result)
                
            return results
            
    except sqlite3.Error as e:
        logging.error(f"Error retrieving enrichment history: {e}")
        return []

__all__ = [

    'ensure_enrichment_audit_table',

    'ensure_enrichment_runs_table',

    'ensure_enrichment_run_items_table',

    'ensure_enrichment_overrides_table',

    'ensure_run_tracking_tables',

    'ensure_enrichment_batch_jobs_table',

    'create_enrichment_batch_job',

    'get_enrichment_batch_job',

    'list_enrichment_batch_jobs',

    'update_enrichment_batch_job',

    'update_enrichment_run_status',

    'create_run_id',

    'create_query_hash',

    'start_enrichment_run',

    'finalize_enrichment_run',

    'materialize_run_inputs',

    'update_run_item_statuses',

    'update_run_item_statuses_by_row_order',

    'upsert_enrichment_override',

    'delete_enrichment_override',

    'list_enrichment_runs',

    'get_enrichment_run',

    'create_run_summary_view',

    'store_raw_enrichment_response',

    'get_enrichment_response_history',


]
