#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest"]
# ///
"""Tests for database connection lifecycle behavior."""

import sqlite3

import pytest

from doctrail.db_operations import (
    CURRENT_SCHEMA_VERSION,
    ENRICHMENT_AUDIT_TABLE,
    ENRICHMENT_RUNS_TABLE,
    ENRICHMENT_RUN_ITEMS_TABLE,
    ENRICHMENTS_TABLE,
    PROMPTS_TABLE,
    create_run_summary_view,
    ensure_enrichment_audit_table,
    ensure_enrichments_table,
    execute_query,
    get_db_connection,
    start_enrichment_run,
)
from doctrail.db_ops.audit_runs import _ensure_enrichment_audit_schema
from doctrail.db_ops.enrichments import _ensure_enrichments_table_schema, _ensure_prompts_schema
from doctrail.db_ops.migrations import run_pending_migrations


def test_connection_context_does_not_retry_body_operational_errors(tmp_path):
    """Operational errors raised by caller SQL should propagate unchanged."""
    db_path = tmp_path / "connection.db"

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        with get_db_connection(str(db_path), retries=2):
            raise sqlite3.OperationalError("database is locked")


def test_execute_query_count_preflight_handles_limit_inside_string(tmp_path):
    """A debug row-count query should not break valid SQL containing LIMIT text."""
    db_path = tmp_path / "query.db"
    with get_db_connection(str(db_path)) as conn:
        conn.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO docs (note) VALUES (?)", ("literal LIMIT token",))
        conn.commit()

    rows = execute_query(
        str(db_path),
        "SELECT id, note FROM docs WHERE note = 'literal LIMIT token' LIMIT 1",
    )

    assert rows == [{"id": 1, "note": "literal LIMIT token"}]


def test_run_summary_view_quotes_run_id_and_view_name(tmp_path):
    """Run summary views should quote caller-supplied view names and run IDs."""
    db_path = tmp_path / "summary.db"
    run_id = "run'quoted"
    started_at = "2026-04-23T10:00:00"

    start_enrichment_run(
        str(db_path),
        run_id=run_id,
        command_started_at=started_at,
        enrichment_name="language",
        model="gpt-4o-mini",
        prompt_id="prompt",
        query_sql="SELECT 1",
        query_hash="hash",
        key_column="sha1",
        source_name="documents",
    )

    view_name = create_run_summary_view(str(db_path), run_id, view_name="summary view")

    with get_db_connection(str(db_path)) as conn:
        row = conn.execute('SELECT run_id FROM "v_summary view"').fetchone()

    assert view_name == "v_summary view"
    assert row == (run_id,)


def test_ensure_enrichments_table_preserves_orphaned_migration_table(tmp_path):
    """Failed migration leftovers should be renamed rather than dropped."""
    db_path = tmp_path / "orphaned.db"
    with get_db_connection(str(db_path)) as conn:
        conn.execute("CREATE TABLE _enrichments_new (value TEXT)")
        conn.execute("INSERT INTO _enrichments_new (value) VALUES ('recoverable')")
        conn.commit()

    ensure_enrichments_table(str(db_path))

    with get_db_connection(str(db_path)) as conn:
        active = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_enrichments_new'"
        ).fetchone()
        archived = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE '_enrichments_new_orphaned_%'"
        ).fetchone()
        value = None
        if archived:
            value = conn.execute(f'SELECT value FROM "{archived[0]}"').fetchone()

    assert active is None
    assert archived is not None
    assert value == ("recoverable",)


def test_schema_migration_stamps_fresh_database(tmp_path):
    """A fresh Doctrail storage database should reach the current schema version."""
    db_path = tmp_path / "fresh_schema.db"

    ensure_enrichments_table(str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    assert version == CURRENT_SCHEMA_VERSION
    assert {ENRICHMENTS_TABLE, ENRICHMENT_AUDIT_TABLE, PROMPTS_TABLE, ENRICHMENT_RUNS_TABLE} <= tables


def test_schema_migration_renames_bookkeeping_tables_and_views(tmp_path):
    """Migration 2 should rename old bookkeeping tables and rebuild managed views."""
    db_path = tmp_path / "v1_schema.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript("""
            CREATE TABLE documents (sha1 TEXT PRIMARY KEY, filename TEXT);
            INSERT INTO documents VALUES ('k1', 'doc.txt');

            CREATE TABLE enrichments (
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
            );
            INSERT INTO enrichments (
                key_value, enrichment_name, field_name, value, value_type,
                timestamp, model, prompt_hash, run_id, query_hash
            ) VALUES (
                'k1', 'task', 'score', '7', 'integer',
                '2026-01-01T00:00:00', 'm1', 'p1', 'run-1', 'q1'
            );

            CREATE TABLE enrichment_audit (
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
            );
            INSERT INTO enrichment_audit (
                key_value, enrichment_name, raw_json, projection_json,
                model_used, prompt_id, run_id, query_hash
            ) VALUES (
                'k1', 'task', '{"score":7}',
                '[{"field_name":"score","value":"7","value_type":"integer","metadata":null}]',
                'm1', 'p1', 'run-1', 'q1'
            );

            CREATE TABLE enrichment_runs (
                run_id TEXT PRIMARY KEY,
                command_started_at TEXT NOT NULL,
                started_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                enrichment_name TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_id TEXT,
                prompt_hash TEXT,
                query_sql TEXT,
                query_hash TEXT,
                key_column TEXT DEFAULT 'sha1'
            );
            INSERT INTO enrichment_runs (
                run_id, command_started_at, started_at, status,
                enrichment_name, model, prompt_id, prompt_hash, query_sql, query_hash
            ) VALUES (
                'run-1', '2026-01-01T00:00:00', '2026-01-01T00:00:00',
                'completed', 'task', 'm1', 'p1', 'p1',
                'SELECT sha1 FROM documents', 'q1'
            );

            CREATE TABLE enrichment_run_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                row_order INTEGER NOT NULL,
                key_value TEXT NOT NULL,
                row_json TEXT,
                status TEXT DEFAULT 'candidate'
            );
            INSERT INTO enrichment_run_items (
                run_id, row_order, key_value, row_json, status
            ) VALUES (
                'run-1', 0, 'k1', '{"sha1":"k1","filename":"doc.txt"}', 'processed'
            );

            CREATE VIEW documents_enriched AS
            SELECT d.sha1, e.value AS score
            FROM documents d
            JOIN enrichments e ON e.key_value = d.sha1;

            CREATE VIEW run_task_summary AS
            SELECT run_id, status FROM enrichment_runs;

            CREATE VIEW custom_lookup AS
            SELECT key_value, value FROM enrichments;

            PRAGMA user_version = 1;
        """)

    with sqlite3.connect(str(db_path)) as conn:
        version = run_pending_migrations(conn)

    with sqlite3.connect(str(db_path)) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        views = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
        }
        enrichment_count = conn.execute(f"SELECT COUNT(*) FROM {ENRICHMENTS_TABLE}").fetchone()[0]
        audit_count = conn.execute(f"SELECT COUNT(*) FROM {ENRICHMENT_AUDIT_TABLE}").fetchone()[0]
        item_count = conn.execute(f"SELECT COUNT(*) FROM {ENRICHMENT_RUN_ITEMS_TABLE}").fetchone()[0]
        enriched_row = conn.execute("SELECT score FROM v_documents_enriched").fetchone()
        run_row = conn.execute("SELECT status FROM v_run_task_summary").fetchone()
        custom_row = conn.execute("SELECT value FROM custom_lookup").fetchone()

    assert version == CURRENT_SCHEMA_VERSION
    assert {"enrichments", "enrichment_audit", "enrichment_runs", "enrichment_run_items"}.isdisjoint(tables)
    assert {ENRICHMENTS_TABLE, ENRICHMENT_AUDIT_TABLE, ENRICHMENT_RUNS_TABLE, ENRICHMENT_RUN_ITEMS_TABLE} <= tables
    assert {"v_documents_enriched", "v_run_task_summary", "custom_lookup"} <= views
    assert (enrichment_count, audit_count, item_count) == (1, 1, 1)
    assert enriched_row == ("7",)
    assert run_row == ("completed",)
    assert custom_row == ("7",)

    with sqlite3.connect(str(db_path)) as conn:
        assert run_pending_migrations(conn) == CURRENT_SCHEMA_VERSION


def test_schema_migration_second_run_is_noop(tmp_path):
    """Running the ordered migration twice should not duplicate existing rows."""
    db_path = tmp_path / "repeat_schema.db"
    ensure_enrichments_table(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO _prompts (prompt_id, enrichment_name, prompt_text, prompt_hash) "
            "VALUES ('p1', 'task', 'Prompt', 'p1')"
        )
        conn.commit()

    ensure_enrichment_audit_table(str(db_path))
    ensure_enrichment_audit_table(str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        prompt_count = conn.execute("SELECT COUNT(*) FROM _prompts").fetchone()[0]

    assert version == CURRENT_SCHEMA_VERSION
    assert prompt_count == 1


def test_schema_migration_stamps_pre_runner_database(tmp_path):
    """A database with current ad hoc tables but user_version=0 should migrate."""
    db_path = tmp_path / "pre_runner.db"
    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.cursor()
        _ensure_enrichment_audit_schema(cursor)
        _ensure_enrichments_table_schema(cursor)
        _ensure_prompts_schema(cursor)
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0

    ensure_enrichment_audit_table(str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        identity_index = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_enrichments_identity'"
        ).fetchone()

    assert version == CURRENT_SCHEMA_VERSION
    assert identity_index is not None
