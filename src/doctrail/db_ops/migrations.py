"""Ordered SQLite schema migrations for Doctrail-managed databases."""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable

from .common import (
    BOOKKEEPING_TABLE_RENAMES,
    DOCTRAIL_FINAL_TABLES_TABLE,
    DOCTRAIL_VIEW_PREFIX,
    ENRICHMENT_AUDIT_TABLE,
    ENRICHMENT_BATCH_JOBS_TABLE,
    ENRICHMENT_OVERRIDES_TABLE,
    ENRICHMENT_RUN_ITEMS_TABLE,
    ENRICHMENT_RUNS_TABLE,
    ICR_SAMPLES_TABLE,
    _doctrail_view_name,
    _quote_identifier,
)

CURRENT_SCHEMA_VERSION = 3

Migration = tuple[int, Callable[[sqlite3.Connection], None]]


def _object_exists(cursor: sqlite3.Cursor, object_type: str, name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
        (object_type, name),
    )
    return cursor.fetchone() is not None


def _table_ref(table_name: str) -> str:
    return _quote_identifier(table_name, "table name")


def _rename_bookkeeping_tables(cursor: sqlite3.Cursor) -> None:
    renames = dict(BOOKKEEPING_TABLE_RENAMES)
    renames["enrichment_audit_new"] = "_enrichment_audit_new"

    for old_name, new_name in renames.items():
        if not _object_exists(cursor, "table", old_name):
            continue
        if _object_exists(cursor, "table", new_name):
            archive_name = f"_legacy_{old_name}_pre_migration"
            suffix = 1
            while _object_exists(cursor, "table", archive_name):
                suffix += 1
                archive_name = f"_legacy_{old_name}_pre_migration_{suffix}"
            cursor.execute(
                f"ALTER TABLE {_table_ref(old_name)} RENAME TO {_table_ref(archive_name)}"
            )
            logging.warning(
                "Found both %s and %s; renamed %s to %s for manual recovery",
                old_name,
                new_name,
                old_name,
                archive_name,
            )
            continue

        cursor.execute(f"ALTER TABLE {_table_ref(old_name)} RENAME TO {_table_ref(new_name)}")
        logging.info("Renamed Doctrail table %s to %s", old_name, new_name)


def _sql_references_table(sql: str, table_name: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(table_name)}(?![A-Za-z0-9_])", sql))


def _rewrite_sql_table_names(sql: str) -> str:
    rewritten = sql
    replacements = dict(BOOKKEEPING_TABLE_RENAMES)
    replacements["enrichment_audit_new"] = "_enrichment_audit_new"
    for old_name, new_name in replacements.items():
        rewritten = re.sub(rf'"{re.escape(old_name)}"', f'"{new_name}"', rewritten)
        rewritten = re.sub(rf"`{re.escape(old_name)}`", f"`{new_name}`", rewritten)
        rewritten = re.sub(rf"\[{re.escape(old_name)}\]", f"[{new_name}]", rewritten)
        rewritten = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(old_name)}(?![A-Za-z0-9_])",
            new_name,
            rewritten,
        )
    return rewritten


def _known_doctrail_view_name(name: str) -> bool:
    if name.startswith(DOCTRAIL_VIEW_PREFIX):
        return True
    return (
        name.startswith("run_")
        or name.startswith("final_")
        or name.startswith("enrichments_")
        or name.endswith("_enriched")
    )


def _extract_view_body(sql: str) -> str:
    match = re.match(
        r"""(?is)^\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?VIEW\s+"""
        r"""(?:IF\s+NOT\s+EXISTS\s+)?"""
        r"""(?:"(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\]|[^\s]+)\s+AS\s+""",
        sql,
    )
    if not match:
        raise ValueError("could not parse CREATE VIEW statement")
    return sql[match.end():]


def _migrate_doctrail_views(cursor: sqlite3.Cursor) -> None:
    cursor.execute("SELECT name, sql FROM sqlite_master WHERE type = 'view' AND sql IS NOT NULL")
    view_rows = cursor.fetchall()
    views_to_recreate = []
    for name, sql in view_rows:
        references_renamed_table = any(
            _sql_references_table(sql, old_name) or _sql_references_table(sql, new_name)
            for old_name, new_name in BOOKKEEPING_TABLE_RENAMES.items()
        )
        if _known_doctrail_view_name(name) or references_renamed_table:
            target_name = _doctrail_view_name(name) if _known_doctrail_view_name(name) else name
            views_to_recreate.append((name, target_name, sql))

    for old_name, _, _ in views_to_recreate:
        cursor.execute(f"DROP VIEW IF EXISTS {_quote_identifier(old_name, 'view name')}")

    for old_name, target_name, sql in views_to_recreate:
        try:
            body = _extract_view_body(_rewrite_sql_table_names(sql))
            cursor.execute(
                f"CREATE VIEW {_quote_identifier(target_name, 'view name')} AS {body}"
            )
            if target_name != old_name:
                logging.info("Renamed Doctrail view %s to %s", old_name, target_name)
        except Exception as exc:
            cursor.execute(f"DROP VIEW IF EXISTS {_quote_identifier(target_name, 'view name')}")
            logging.warning(
                "Dropped view %s during Doctrail table-name migration because it could "
                "not be recreated as %s: %s. Rebuild it with `doctrail view pivot`, "
                "`doctrail view spec`, or `doctrail view create`.",
                old_name,
                target_name,
                exc,
            )


def _ensure_enrichment_runs_schema(cursor: sqlite3.Cursor) -> None:
    table_ref = _table_ref(ENRICHMENT_RUNS_TABLE)
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_ref} (
            run_id TEXT PRIMARY KEY,
            command_started_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            enrichment_name TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_id TEXT,
            prompt_hash TEXT,
            query_sql TEXT,
            query_hash TEXT,
            dedupe_scope TEXT DEFAULT 'query',
            key_column TEXT DEFAULT 'sha1',
            source_name TEXT,
            materialized_inputs INTEGER DEFAULT 1,
            total_rows INTEGER DEFAULT 0,
            processed_rows INTEGER DEFAULT 0,
            skipped_rows INTEGER DEFAULT 0,
            insufficient_rows INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            cached_input_tokens INTEGER DEFAULT 0,
            cache_creation_input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            estimated_cost REAL DEFAULT 0,
            project TEXT,
            metadata TEXT
        )
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_runs_started
        ON {table_ref}(command_started_at DESC)
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_runs_enrichment
        ON {table_ref}(enrichment_name, command_started_at DESC)
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_runs_query_hash
        ON {table_ref}(query_hash)
    """)
    cursor.execute(f"PRAGMA table_info({table_ref})")
    columns = [row[1] for row in cursor.fetchall()]
    if "cached_input_tokens" not in columns:
        cursor.execute(f"ALTER TABLE {table_ref} ADD COLUMN cached_input_tokens INTEGER DEFAULT 0")
    if "cache_creation_input_tokens" not in columns:
        cursor.execute(f"ALTER TABLE {table_ref} ADD COLUMN cache_creation_input_tokens INTEGER DEFAULT 0")


def _ensure_enrichment_run_items_schema(cursor: sqlite3.Cursor) -> None:
    table_ref = _table_ref(ENRICHMENT_RUN_ITEMS_TABLE)
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_ref} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            row_order INTEGER NOT NULL,
            key_value TEXT NOT NULL,
            row_json TEXT,
            status TEXT DEFAULT 'candidate',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute(f"PRAGMA table_info({table_ref})")
    columns = [row[1] for row in cursor.fetchall()]
    if "status" not in columns:
        cursor.execute(f"ALTER TABLE {table_ref} ADD COLUMN status TEXT DEFAULT 'candidate'")
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_run_items_run
        ON {table_ref}(run_id, row_order)
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_run_items_key
        ON {table_ref}(run_id, key_value)
    """)


def _ensure_enrichment_overrides_schema(cursor: sqlite3.Cursor) -> None:
    table_ref = _table_ref(ENRICHMENT_OVERRIDES_TABLE)
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_ref} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            key_value TEXT NOT NULL,
            enrichment_name TEXT NOT NULL,
            field_name TEXT NOT NULL,
            override_value TEXT,
            reviewer TEXT,
            note TEXT,
            source TEXT DEFAULT 'manual',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, key_value, enrichment_name, field_name)
        )
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_overrides_run
        ON {table_ref}(run_id)
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_overrides_key
        ON {table_ref}(run_id, key_value)
    """)


def _ensure_enrichment_batch_jobs_schema(cursor: sqlite3.Cursor) -> None:
    table_ref = _table_ref(ENRICHMENT_BATCH_JOBS_TABLE)
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_ref} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            model TEXT NOT NULL,
            provider_batch_id TEXT UNIQUE,
            input_file_id TEXT NOT NULL,
            output_file_id TEXT,
            error_file_id TEXT,
            status TEXT NOT NULL DEFAULT 'submitted',
            request_count INTEGER DEFAULT 0,
            completed_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            input_file_bytes INTEGER DEFAULT 0,
            metadata TEXT,
            submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_polled_at TEXT,
            completed_at TEXT,
            reconciled_at TEXT
        )
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_batch_jobs_run
        ON {table_ref}(run_id, submitted_at)
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_batch_jobs_status
        ON {table_ref}(status, reconciled_at)
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_enrichment_batch_jobs_provider_batch
        ON {table_ref}(provider_batch_id)
    """)


def _ensure_final_tables_registry_schema(cursor: sqlite3.Cursor) -> None:
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {_table_ref(DOCTRAIL_FINAL_TABLES_TABLE)} (
            table_name TEXT PRIMARY KEY,
            source_view TEXT NOT NULL,
            source_run_id TEXT,
            created_at TEXT NOT NULL
        )
    """)


def _ensure_icr_samples_schema(cursor: sqlite3.Cursor) -> None:
    table_ref = _table_ref(ICR_SAMPLES_TABLE)
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (ICR_SAMPLES_TABLE,),
    )
    if cursor.fetchone():
        cursor.execute(f"PRAGMA table_info({table_ref})")
        columns = [row[1] for row in cursor.fetchall()]
        if "sha1" in columns and "key_value" not in columns:
            logging.info("Migrating icr_samples: renaming sha1 -> key_value, adding key_column")
            cursor.execute(f"ALTER TABLE {table_ref} RENAME COLUMN sha1 TO key_value")
            cursor.execute(f"ALTER TABLE {table_ref} ADD COLUMN key_column TEXT DEFAULT 'sha1'")
    else:
        cursor.execute(f"""
            CREATE TABLE {table_ref} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_id TEXT NOT NULL,
                enrichment_name TEXT NOT NULL,
                key_value TEXT NOT NULL,
                key_column TEXT DEFAULT 'sha1',
                stratum TEXT,
                seed INTEGER,
                sample_size INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_icr_samples_sample_id
        ON {table_ref}(sample_id)
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_icr_samples_key_value
        ON {table_ref}(key_value)
    """)
    cursor.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_icr_samples_enrichment
        ON {table_ref}(enrichment_name)
    """)


def _migration_1_baseline(conn: sqlite3.Connection) -> None:
    """Baseline the existing idempotent schema guards under user_version."""
    from .audit_runs import _ensure_enrichment_audit_schema
    from .enrichments import _ensure_enrichments_table_schema, _ensure_prompts_schema

    cursor = conn.cursor()
    _rename_bookkeeping_tables(cursor)
    _ensure_enrichment_audit_schema(cursor)
    _ensure_enrichments_table_schema(cursor)
    _ensure_prompts_schema(cursor)
    _ensure_enrichment_runs_schema(cursor)
    _ensure_enrichment_run_items_schema(cursor)
    _ensure_enrichment_overrides_schema(cursor)
    _ensure_enrichment_batch_jobs_schema(cursor)
    _ensure_final_tables_registry_schema(cursor)
    _ensure_icr_samples_schema(cursor)


def _migration_2_bookkeeping_names(conn: sqlite3.Connection) -> None:
    """Prefix Doctrail tables and views without deleting stored rows."""
    cursor = conn.cursor()
    _rename_bookkeeping_tables(cursor)
    _migrate_doctrail_views(cursor)


def _migration_3_cache_token_counters(conn: sqlite3.Connection) -> None:
    """Add prompt-cache token counters to audit and run records."""
    from .audit_runs import _ensure_enrichment_audit_schema

    cursor = conn.cursor()
    _ensure_enrichment_audit_schema(cursor)
    _ensure_enrichment_runs_schema(cursor)


MIGRATIONS: list[Migration] = [
    (1, _migration_1_baseline),
    (2, _migration_2_bookkeeping_names),
    (3, _migration_3_cache_token_counters),
]


def run_pending_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending schema migrations and return the resulting user_version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than this Doctrail "
            f"build supports ({CURRENT_SCHEMA_VERSION})"
        )

    for version, migrate in MIGRATIONS:
        if version <= current:
            continue
        conn.execute("BEGIN")
        try:
            migrate(conn)
            conn.execute(f"PRAGMA user_version = {version}")
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        current = version

    return current


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MIGRATIONS",
    "run_pending_migrations",
]
