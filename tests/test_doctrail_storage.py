#!/usr/bin/env python3
"""Split doctrail tests."""

import pytest
import shutil
import yaml
import sqlite3
import asyncio
import csv
import json
from datetime import datetime
from click.testing import CliRunner
from pathlib import Path
import logging
import sys
import os
from types import SimpleNamespace
from typing import Optional, get_args

sys.path.insert(0, str(Path(__file__).parent.parent))

from doctrail.main import cli
import sqlite_utils
from doctrail.llm_providers.anthropic_provider import AnthropicProvider
from doctrail.llm_providers.gemini_provider import GeminiProvider
from doctrail.llm_providers.openai_provider import OpenAIProvider
from doctrail.utils.model_pricing import get_openai_batch_model_info
from tests.doctrail_support import *


def test_output_db_separation(temp_env, caplog):
    """
    Test that --output-db writes enrichments to a separate database
    while reading source data from the original database.

    Verifies:
    1. Source db is never modified (no enrichments/audit tables added)
    2. Output db gets enrichments + enrichment_audit tables
    3. Cross-db views work via ATTACH
    4. _doctrail_meta stores the source path
    """
    runner = CliRunner()

    source_db = temp_env["db_path"]
    output_db = temp_env["temp_dir"] / "output.db"

    seed_conn = sqlite3.connect(source_db)
    seed_conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    existing = seed_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    if existing == 0:
        seed_conn.executemany(
            "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
            [
                ("output_seed_1", "doc1.txt", "x" * 300),
                ("output_seed_2", "doc2.txt", "y" * 300),
            ],
        )
        seed_conn.commit()
    seed_conn.close()

    # Build a minimal enrichment config matching existing test patterns
    config = {
        "database": str(source_db),
        "default_table": "documents",
        "enrichments": [
            {
                "name": "test_validate",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["raw_content"],
                },
                "prompt": "Validate this document content.",
                "model": "gpt-4o-mini",
                "output_column": "content_valid",
                "schema": {"content_valid": {"type": "boolean"}},
            }
        ],
        "sql_queries": {
            "all_docs": "SELECT rowid, sha1 FROM documents",
        },
    }

    config_path = temp_env["temp_dir"] / "output_db_test.yml"
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    # Run enrichment with --output-db
    result = runner.invoke(cli, [
        "--skip-requirements",
        "enrich",
        "--config", str(config_path),
        "--enrichments", "test_validate",
        "--output-db", str(output_db),
        "--overwrite",
    ])

    if result.exit_code != 0:
        print(f"Output:\n{result.output}")
        if result.exception:
            import traceback
            traceback.print_exception(type(result.exception), result.exception, result.exception.__traceback__)
    assert result.exit_code == 0, f"Enrichment failed:\n{result.output}"

    # 1. Source db should NOT have enrichments or enrichment_audit tables
    source_conn = sqlite3.connect(source_db)
    source_tables = [
        r[0] for r in source_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    source_conn.close()
    assert "_enrichments" not in source_tables, "Source db should not have enrichments table"
    assert "_enrichment_audit" not in source_tables, "Source db should not have enrichment_audit table"

    # 2. Output db SHOULD have enrichments + enrichment_audit
    out_conn = sqlite3.connect(output_db)
    out_tables = [
        r[0] for r in out_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    assert "_enrichments" in out_tables, f"Output db missing enrichments table. Tables: {out_tables}"
    assert "_enrichment_audit" in out_tables, f"Output db missing enrichment_audit table. Tables: {out_tables}"

    # Check enrichments were actually written
    row_count = out_conn.execute("SELECT COUNT(*) FROM _enrichments").fetchone()[0]
    assert row_count > 0, "No enrichment rows in output db"

    # 3. _doctrail_meta should store the source path
    assert "_doctrail_meta" in out_tables, "Output db missing _doctrail_meta table"
    stored_source = out_conn.execute(
        "SELECT value FROM _doctrail_meta WHERE key = 'source_db_path'"
    ).fetchone()
    assert stored_source is not None, "No source_db_path in _doctrail_meta"
    assert os.path.basename(stored_source[0]) == "test.db"

    # 4. Cross-db view should exist and be queryable with ATTACH
    out_views = [
        r[0] for r in out_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()
    ]
    has_enriched_view = any("enriched" in v for v in out_views)

    if has_enriched_view:
        # View uses a local _source_documents copy — no ATTACH needed
        view_name = [v for v in out_views if "enriched" in v][0]
        view_rows = out_conn.execute(f"SELECT * FROM {view_name} LIMIT 1").fetchall()
        assert len(view_rows) > 0, f"View {view_name} returned no rows"

    out_conn.close()

def test_output_db_views_queryable(temp_env):
    """
    Verify that views created with --output-db can be queried
    after manually ATTACHing the source database.
    """
    from doctrail.db_operations import (
        ensure_enrichments_table,
        ensure_enrichment_audit_table,
        write_enrichment,
        create_enrichments_views,
        store_source_db_path,
        get_source_db_path,
        attach_source_db,
    )

    source_db = str(temp_env["db_path"])
    output_db = str(temp_env["temp_dir"] / "views_test.db")

    seed_conn = sqlite3.connect(source_db)
    seed_conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    existing = seed_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    if existing == 0:
        seed_conn.execute(
            "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
            ("test_sha1_1", "seed.txt", "x" * 200),
        )
        seed_conn.commit()
    seed_conn.close()

    # Set up output db with enrichment data
    ensure_enrichments_table(output_db)
    ensure_enrichment_audit_table(output_db)
    store_source_db_path(output_db, source_db)

    # Write a fake enrichment referencing a key_value that exists in source
    write_enrichment(
        db_path=output_db,
        key_value="test_sha1_1",
        enrichment_name="test_lang",
        field_name="language",
        value="Chinese",
        model="gpt-4o-mini",
    )

    # Create the cross-db view
    create_enrichments_views(
        db_path=output_db,
        source_table="documents",
        source_db_path=source_db,
    )

    # Verify stored source path
    retrieved = get_source_db_path(output_db)
    assert retrieved is not None
    assert retrieved.endswith("test.db")

    # Query the view with ATTACH
    conn = sqlite3.connect(output_db)
    attach_source_db(conn, source_db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("SELECT * FROM v_documents_enriched LIMIT 5").fetchall()
    assert len(rows) > 0, "View returned no rows"

    # Check that the enrichment column is present
    col_names = rows[0].keys()
    assert "language" in col_names, f"Expected 'language' column in view. Got: {list(col_names)}"

    # Check value
    row = dict(rows[0])
    assert row["language"] == "Chinese"

    conn.close()

def test_rebuild_enrichments_from_audit_exact(temp_env):
    """Audit projection payloads should rebuild enrichments without reparsing raw_json."""
    from doctrail.db_operations import (
        ENRICHMENT_PROJECTION_VERSION,
        build_enrichment_projection,
        ensure_enrichment_audit_table,
        ensure_enrichments_table,
        rebuild_enrichments_from_audit,
        serialize_enrichment_projection,
        store_raw_enrichment_response,
    )

    db_path = str(temp_env["db_path"])
    ensure_enrichment_audit_table(db_path)
    ensure_enrichments_table(db_path)

    projection_rows = build_enrichment_projection({
        "category": "science",
        "confidence": 0.92,
    })
    projection_json = serialize_enrichment_projection(projection_rows)
    created_at = "2026-03-12T10:11:12"

    store_raw_enrichment_response(
        db_path=db_path,
        key_value="test_sha1_1",
        enrichment_name="topic_extract",
        raw_json=json.dumps({"category": "science", "confidence": 0.92}),
        model_used="gpt-4o-mini",
        enrichment_id="eid-001",
        prompt_id="prompt-001",
        run_id="run-001",
        query_hash="query-001",
        projection_json=projection_json,
        projection_version=ENRICHMENT_PROJECTION_VERSION,
        project="demo_project",
        created_at=created_at,
    )

    summary = rebuild_enrichments_from_audit(db_path)
    assert summary["audit_rows"] == 1
    assert summary["written_rows"] == 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT field_name, value, value_type, timestamp, model, prompt_hash, run_id, query_hash, project
        FROM _enrichments
        WHERE enrichment_name = 'topic_extract'
        ORDER BY field_name
    """).fetchall()
    conn.close()

    assert len(rows) == 2
    assert dict(rows[0]) == {
        "field_name": "category",
        "value": "science",
        "value_type": "string",
        "timestamp": created_at,
        "model": "gpt-4o-mini",
        "prompt_hash": "prompt-001",
        "run_id": "run-001",
        "query_hash": "query-001",
        "project": "demo_project",
    }
    assert dict(rows[1]) == {
        "field_name": "confidence",
        "value": "0.92",
        "value_type": "number",
        "timestamp": created_at,
        "model": "gpt-4o-mini",
        "prompt_hash": "prompt-001",
        "run_id": "run-001",
        "query_hash": "query-001",
        "project": "demo_project",
    }

def test_run_enrichment_persists_projection_json_and_rebuilds(temp_env, monkeypatch):
    """The live enrichment path should write audit and enrichments from the same projection payload."""
    from doctrail.core import run_enrichment
    from doctrail.db_operations import rebuild_enrichments_from_audit

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "projection_rebuild.yml"

    seed_conn = sqlite3.connect(db_path)
    seed_conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    existing = seed_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    if existing == 0:
        seed_conn.execute(
            "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
            ("test_sha1_1", "seed.txt", "x" * 300),
        )
        seed_conn.commit()
    seed_conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {
            "all_docs": "SELECT rowid, sha1 FROM documents",
        },
        "enrichments": [
            {
                "name": "doc_summary",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["raw_content"],
                },
                "prompt": "Summarize the document in one line.",
                "model": "gpt-4o-mini",
                "output_column": "summary",
            }
        ],
    }

    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    async def mock_llm(*args, **kwargs):
        return "Short summary"

    monkeypatch.setattr('doctrail.llm_operations.call_llm', mock_llm)
    result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["doc_summary"],
        overwrite=True,
        skip_cost_check=True,
    ))
    assert result["status"] == "success"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    audit_row = conn.execute("""
        SELECT key_value, raw_json, projection_json, projection_version, prompt_id, run_id, query_hash, created_at
        FROM _enrichment_audit
        WHERE enrichment_name = 'doc_summary'
        ORDER BY created_at
        LIMIT 1
    """).fetchone()
    enrichment_row = conn.execute("""
        SELECT key_value, field_name, value, value_type, prompt_hash, run_id, query_hash, timestamp
        FROM _enrichments
        WHERE enrichment_name = 'doc_summary'
        ORDER BY timestamp
        LIMIT 1
    """).fetchone()
    assert audit_row is not None
    assert enrichment_row is not None
    raw_payload = json.loads(audit_row["raw_json"])
    projection_payload = json.loads(audit_row["projection_json"])
    expected_value = projection_payload[0]["value"]
    assert audit_row["projection_version"] == "v1"
    assert projection_payload == [
        {
            "field_name": "summary",
            "value": expected_value,
            "value_type": "string",
            "metadata": None,
        }
    ]
    if "result" in raw_payload:
        assert raw_payload["result"] == expected_value
    else:
        assert raw_payload["summary"] == expected_value
    assert enrichment_row["value"] == expected_value
    assert enrichment_row["value_type"] == "string"
    assert enrichment_row["prompt_hash"] == audit_row["prompt_id"]
    assert enrichment_row["run_id"] == audit_row["run_id"]
    assert enrichment_row["query_hash"] == audit_row["query_hash"]
    assert enrichment_row["timestamp"] == audit_row["created_at"]

    conn.execute("DELETE FROM _enrichments WHERE enrichment_name = 'doc_summary'")
    conn.commit()
    conn.close()

    summary = rebuild_enrichments_from_audit(str(db_path), run_id=audit_row["run_id"])
    assert summary["audit_rows"] >= 1
    assert summary["written_rows"] >= 1

    conn = sqlite3.connect(db_path)
    rebuilt = conn.execute("""
        SELECT value, timestamp
        FROM _enrichments
        WHERE enrichment_name = 'doc_summary'
        ORDER BY timestamp
        LIMIT 1
    """).fetchone()
    conn.close()
    assert rebuilt == (expected_value, audit_row["created_at"])


def test_run_enrichment_key_only_query_fetches_input_columns(temp_env, monkeypatch):
    """Scope queries should select row keys while input_columns fetch prompt payload."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "rowid_explicit.yml"

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    conn.execute("DELETE FROM documents")
    conn.executemany(
        "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
        [
            ("doc1", "one.txt", "short"),
            ("doc2", "two.txt", "other"),
        ],
    )
    selected_rowid = conn.execute(
        "SELECT rowid FROM documents WHERE sha1 = 'doc1'"
    ).fetchone()[0]
    conn.commit()
    conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {
            "explicit_docs": "SELECT sha1 FROM documents",
        },
        "enrichments": [
            {
                "name": "short_summary",
                "input": {
                    "query": "explicit_docs",
                    "input_columns": ["raw_content"],
                },
                "prompt": "Summarize this: {raw_content}",
                "model": "gpt-4o-mini",
                "output_column": "summary",
            }
        ],
    }

    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    async def mock_llm(*args, **kwargs):
        return "processed short input"

    monkeypatch.setattr("doctrail.llm_operations.call_llm", mock_llm)

    result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["short_summary"],
        rowid=selected_rowid,
        overwrite=True,
        skip_cost_check=True,
    ))

    assert result["status"] == "success"
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT key_value, value
        FROM _enrichments
        WHERE enrichment_name = 'short_summary'
        ORDER BY key_value
    """).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "doc1"


def test_run_enrichment_cost_threshold_aborts_without_tty(temp_env, monkeypatch):
    """High estimated costs should abort in noninteractive execution unless explicitly skipped."""
    from doctrail.core import run_enrichment, EnrichmentError

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "cost_gate.yml"

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    conn.execute("DELETE FROM documents")
    conn.execute(
        "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
        ("doc1", "one.txt", "short"),
    )
    conn.commit()
    conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {
            "all_docs": "SELECT rowid, sha1 FROM documents",
        },
        "enrichments": [
            {
                "name": "costly_summary",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["raw_content"],
                },
                "prompt": "Summarize this: {raw_content}",
                "model": "gpt-4o-mini",
                "output_column": "summary",
            }
        ],
    }

    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    monkeypatch.setattr(
        "doctrail.core_runtime.enrichment._estimate_enrichment_cost",
        lambda *args, **kwargs: {"total_cost": 10.0},
    )
    monkeypatch.setattr(
        "doctrail.core_runtime.enrichment.sys.stdin.isatty",
        lambda: False,
    )

    with pytest.raises(EnrichmentError) as exc_info:
        asyncio.run(run_enrichment(
            config_path=str(config_path),
            enrichments=["costly_summary"],
            cost_threshold=1.0,
        ))

    assert "--skip-cost-check" in str(exc_info.value)


def test_query_scope_cost_estimate_counts_only_unprocessed_rows(temp_env, monkeypatch):
    """Append-mode cost checks should ignore query-scope rows already answered."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "incremental_cost.yml"

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    conn.execute("DELETE FROM documents")
    conn.executemany(
        "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
        [
            ("doc1", "one.txt", "x" * 300),
            ("doc2", "two.txt", "y" * 300),
        ],
    )
    conn.commit()
    conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {
            "all_docs": "SELECT rowid, sha1 FROM documents ORDER BY sha1",
        },
        "enrichments": [
            {
                "name": "incremental_summary",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["raw_content"],
                },
                "prompt": "Summarize this: {raw_content}",
                "model": "gpt-4o-mini",
                "output_column": "summary",
            }
        ],
    }

    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    first = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["incremental_summary"],
        overwrite=False,
        skip_cost_check=True,
    ))
    assert first["success_count"] == 2

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
        ("doc3", "three.txt", "z" * 300),
    )
    conn.commit()
    conn.close()

    observed = {}

    def fake_estimate(_enrichment_config, rows, _model, actual_total, **_kwargs):
        observed["row_keys"] = [row["sha1"] for row in rows]
        observed["actual_total"] = actual_total
        return {"total_cost": 0.0}

    monkeypatch.setattr(
        "doctrail.core_runtime.enrichment._estimate_enrichment_cost",
        fake_estimate,
    )

    second = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["incremental_summary"],
        overwrite=False,
        skip_cost_check=False,
        cost_threshold=1.0,
    ))

    assert second["success_count"] == 1
    assert observed == {"row_keys": ["doc3"], "actual_total": 1}


def test_structured_persist_failure_does_not_call_legacy_fallback(temp_env, monkeypatch):
    """A DB write failure after a structured response must not trigger a second LLM call."""
    from doctrail.core import EnrichmentError, run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "persist_failure.yml"

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    conn.execute("DELETE FROM documents")
    conn.execute(
        "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
        ("doc1", "one.txt", "x" * 300),
    )
    conn.commit()
    conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {
            "all_docs": "SELECT rowid, sha1 FROM documents",
        },
        "enrichments": [
            {
                "name": "structured_summary",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["raw_content"],
                },
                "prompt": "Summarize this: {raw_content}",
                "model": "gpt-4o-mini",
                "output_column": "summary",
                "schema": {"summary": {"type": "string"}},
            }
        ],
    }

    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    calls = {"structured": 0, "legacy": 0}

    async def fake_structured(**kwargs):
        calls["structured"] += 1
        return {
            "enrichment_id": "persist-failure-1",
            "rowid": kwargs["row"].get("rowid"),
            "key_value": kwargs["row"]["sha1"],
            "original": {},
            "updated": {"summary": "structured answer"},
            "raw_json": '{"summary": "structured answer"}',
            "full_prompt": "prompt",
            "usage": {},
        }

    async def fake_legacy(*args, **kwargs):
        calls["legacy"] += 1
        return {
            "key_value": "doc1",
            "updated": "legacy answer",
        }

    def fail_persist(*args, **kwargs):
        raise sqlite3.OperationalError("simulated write failure")

    monkeypatch.setattr("doctrail.llm_operations.process_row_structured", fake_structured)
    monkeypatch.setattr("doctrail.llm_operations.process_row", fake_legacy)
    monkeypatch.setattr("doctrail.llm_operations.persist_enrichment_result", fail_persist)

    with pytest.raises(EnrichmentError) as exc_info:
        asyncio.run(run_enrichment(
            config_path=str(config_path),
            enrichments=["structured_summary"],
            overwrite=False,
            skip_cost_check=True,
        ))

    assert "simulated write failure" in str(exc_info.value)
    assert calls == {"structured": 1, "legacy": 0}


def test_run_enrichment_pack_selected_indexes_unpacks_row_level_results(temp_env, monkeypatch):
    """Packed boolean screening should persist one ordinary row result per source row."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "packed_selected.yml"

    seed_conn = sqlite3.connect(db_path)
    seed_conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    seed_conn.execute("DELETE FROM documents")
    seed_conn.executemany(
        "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
        [
            ("test_sha1_1", "doc1.txt", "这是一个普通说明文档。" * 20),
            ("test_sha2_2", "doc2.txt", "红十字会向器官捐献者家庭发放了5万元慰问金。" * 12),
        ],
    )
    seed_conn.commit()
    seed_conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {
            "all_docs": "SELECT rowid, sha1 FROM documents ORDER BY sha1",
        },
        "enrichments": [
            {
                "name": "packed_relevance",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["raw_content:240"],
                },
                "prompt": "Return the items that mention cash payments or condolence money for donor families.",
                "model": "gpt-4o-mini",
                "output_column": "is_relevant",
                "schema": {"type": "boolean"},
                "pack_size": 2,
                "pack_response_mode": "selected_indexes",
            }
        ],
    }

    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    async def mock_packed_llm(*args, **kwargs):
        packed_model = None
        for arg in args:
            if isinstance(arg, type) and hasattr(arg, "model_fields"):
                packed_model = arg
                break
        if packed_model is None:
            packed_model = kwargs["pydantic_model"]
        result = packed_model(selected_item_indexes=[1])
        if kwargs.get("return_usage"):
            return result, {"input_tokens": 30, "output_tokens": 3, "estimated_cost": 0.03}
        return result

    monkeypatch.setattr("doctrail.llm_operations.call_llm_structured", mock_packed_llm)

    result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["packed_relevance"],
        overwrite=True,
        skip_cost_check=True,
    ))
    assert result["status"] == "success"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    enrichments = conn.execute("""
        SELECT key_value, value
        FROM _enrichments
        WHERE enrichment_name = 'packed_relevance'
        ORDER BY key_value
    """).fetchall()
    audits = conn.execute("""
        SELECT key_value, raw_json
        FROM _enrichment_audit
        WHERE enrichment_name = 'packed_relevance'
        ORDER BY key_value
    """).fetchall()
    statuses = {
        row["status"]: row["count"]
        for row in conn.execute("""
            SELECT status, COUNT(*) AS count
            FROM _enrichment_run_items
            GROUP BY status
        """).fetchall()
    }
    conn.close()

    assert [(row["key_value"], row["value"]) for row in enrichments] == [
        ("test_sha1_1", "false"),
        ("test_sha2_2", "true"),
    ]
    assert len(audits) == 2
    assert json.loads(audits[0]["raw_json"]) == {"selected_item_indexes": [1]}
    assert json.loads(audits[1]["raw_json"]) == {"selected_item_indexes": [1]}
    assert statuses["processed"] == 2

def test_run_enrichment_pack_exhaustive_unpacks_nested_results(temp_env, monkeypatch):
    """Packed exhaustive responses should explode back into normal field rows."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "packed_exhaustive.yml"

    seed_conn = sqlite3.connect(db_path)
    seed_conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    seed_conn.execute("DELETE FROM documents")
    seed_conn.executemany(
        "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
        [
            ("test_sha1_1", "doc1.txt", "这是一个普通说明文档。" * 20),
            ("test_sha2_2", "doc2.txt", "红十字会向器官捐献者家庭发放了5万元慰问金。" * 12),
        ],
    )
    seed_conn.commit()
    seed_conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {
            "all_docs": "SELECT rowid, sha1 FROM documents ORDER BY sha1",
        },
        "enrichments": [
            {
                "name": "packed_analysis",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["raw_content:240"],
                },
                "prompt": "For each item, decide if it is relevant and give a short reason.",
                "model": "gpt-4o-mini",
                "schema": {
                    "is_relevant": {"type": "boolean"},
                    "reason": {"type": "string", "maxLength": 40},
                },
                "pack_size": 2,
                "pack_response_mode": "exhaustive",
            }
        ],
    }

    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    async def mock_packed_llm(*args, **kwargs):
        packed_model = None
        for arg in args:
            if isinstance(arg, type) and hasattr(arg, "model_fields"):
                packed_model = arg
                break
        if packed_model is None:
            packed_model = kwargs["pydantic_model"]

        item_model = get_args(packed_model.model_fields["items"].annotation)[0]
        nested_model = item_model.model_fields["result"].annotation
        result = packed_model(
            items=[
                item_model(
                    item_index=0,
                    result=nested_model(is_relevant=False, reason="No payment language"),
                ),
                item_model(
                    item_index=1,
                    result=nested_model(is_relevant=True, reason="Mentions condolence money"),
                ),
            ]
        )
        if kwargs.get("return_usage"):
            return result, {"input_tokens": 40, "output_tokens": 8, "estimated_cost": 0.04}
        return result

    monkeypatch.setattr("doctrail.llm_operations.call_llm_structured", mock_packed_llm)

    result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["packed_analysis"],
        overwrite=True,
        skip_cost_check=True,
    ))
    assert result["status"] == "success"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    enrichments = conn.execute("""
        SELECT key_value, field_name, value
        FROM _enrichments
        WHERE enrichment_name = 'packed_analysis'
        ORDER BY key_value, field_name
    """).fetchall()
    audits = conn.execute("""
        SELECT key_value, projection_json, raw_json
        FROM _enrichment_audit
        WHERE enrichment_name = 'packed_analysis'
        ORDER BY key_value
    """).fetchall()
    conn.close()

    assert [(row["key_value"], row["field_name"], row["value"]) for row in enrichments] == [
        ("test_sha1_1", "is_relevant", "false"),
        ("test_sha1_1", "reason", "No payment language"),
        ("test_sha2_2", "is_relevant", "true"),
        ("test_sha2_2", "reason", "Mentions condolence money"),
    ]
    assert len(audits) == 2
    assert json.loads(audits[0]["projection_json"]) == [
        {"field_name": "is_relevant", "value": "false", "value_type": "boolean", "metadata": None},
        {"field_name": "reason", "value": "No payment language", "value_type": "string", "metadata": None},
    ]
    assert json.loads(audits[1]["projection_json"]) == [
        {"field_name": "is_relevant", "value": "true", "value_type": "boolean", "metadata": None},
        {"field_name": "reason", "value": "Mentions condolence money", "value_type": "string", "metadata": None},
    ]
    raw_payload = json.loads(audits[0]["raw_json"])
    assert raw_payload["items"][0]["item_index"] == 0
    assert raw_payload["items"][1]["item_index"] == 1

def test_run_enrichment_rejects_pack_mode_with_batch(temp_env):
    """Packed prompt shaping is distinct from provider-side batch submission."""
    from doctrail.core import run_enrichment, EnrichmentError

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "packed_batch_guard.yml"

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {
            "all_docs": "SELECT rowid, sha1 FROM documents ORDER BY sha1",
        },
        "enrichments": [
            {
                "name": "packed_relevance",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["raw_content:240"],
                },
                "prompt": "Return the items that mention cash payments.",
                "model": "gpt-4o-mini",
                "output_column": "is_relevant",
                "schema": {"type": "boolean"},
                "pack_size": 2,
                "pack_response_mode": "selected_indexes",
            }
        ],
    }

    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    with pytest.raises(EnrichmentError, match="pack_size is currently supported only with execution_mode=sync"):
        asyncio.run(run_enrichment(
            config_path=str(config_path),
            enrichments=["packed_relevance"],
            overwrite=True,
            skip_cost_check=True,
            execution_mode="batch",
        ))

def test_rebuild_enrichments_cli_rejects_legacy_audit_rows(temp_env):
    """Exact rebuild should fail loudly when audit rows predate projection persistence."""
    from doctrail.db_operations import ensure_enrichment_audit_table

    db_path = str(temp_env["db_path"])
    ensure_enrichment_audit_table(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO _enrichment_audit (
            enrichment_id, key_value, enrichment_name, raw_json, model_used, prompt_id
        ) VALUES (?, ?, ?, ?, ?, ?)
    """, (
        "legacy-001",
        "test_sha1_1",
        "legacy_task",
        json.dumps({"result": "legacy"}),
        "gpt-4o-mini",
        "prompt-legacy",
    ))
    conn.commit()
    conn.close()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "rebuild-enrichments",
        "--db-path", db_path,
        "--yes",
    ])

    assert result.exit_code != 0
    assert "missing projection_json" in result.output

def test_run_tracking_and_override_workflow(temp_env, monkeypatch):
    """Run metadata, diffs, and manual overrides should work end to end."""
    from doctrail.core import run_enrichment

    runner = CliRunner()
    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "run_workflow.yml"

    seed_conn = sqlite3.connect(db_path)
    seed_conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    existing = seed_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    if existing == 0:
        seed_conn.executemany(
            "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
            [
                ("seed_sha1_1", "doc1.txt", "x" * 300),
                ("seed_sha1_2", "doc2.txt", "y" * 300),
            ],
        )
        seed_conn.commit()
    seed_conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {
            "all_docs": "SELECT rowid, sha1 FROM documents",
        },
        "enrichments": [
            {
                "name": "language_review",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["raw_content"],
                },
                "prompt": "Classify the language of this document.",
                "model": "gpt-4o-mini",
                "output_column": "detected_language",
                "schema": {
                    "detected_language": {
                        "type": "string",
                    }
                },
            }
        ],
    }

    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    async def mock_llm_v1(*args, **kwargs):
        return "english"

    async def mock_llm_v2(*args, **kwargs):
        return "mixed"

    monkeypatch.setattr('doctrail.llm_operations.call_llm', mock_llm_v1)
    first_run = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["language_review"],
        overwrite=True,
        skip_cost_check=True,
    ))
    assert first_run["status"] == "success"

    monkeypatch.setattr('doctrail.llm_operations.call_llm', mock_llm_v2)
    second_run = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["language_review"],
        overwrite=True,
        skip_cost_check=True,
        override_query="SELECT rowid, sha1 FROM documents WHERE filename IS NOT NULL",
    ))
    assert second_run["status"] == "success"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    runs = conn.execute("""
        SELECT run_id, query_hash
        FROM _enrichment_runs
        WHERE enrichment_name = 'language_review'
        ORDER BY command_started_at
    """).fetchall()
    assert len(runs) == 2
    run_a = runs[0]["run_id"]
    run_b = runs[1]["run_id"]
    assert runs[0]["query_hash"] != runs[1]["query_hash"]

    run_a_count = conn.execute(
        "SELECT COUNT(*) FROM _enrichments WHERE run_id = ?",
        (run_a,),
    ).fetchone()[0]
    if run_a_count == 0:
        seed_keys = [
            row["key_value"]
            for row in conn.execute(
                "SELECT key_value FROM _enrichment_run_items WHERE run_id = ? ORDER BY row_order",
                (run_a,),
            ).fetchall()
        ]
        conn.executemany(
            """
            INSERT INTO _enrichments (
                key_value, enrichment_name, field_name, value, value_type,
                timestamp, model, prompt_hash, run_id, query_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    key_value,
                    "language_review",
                    "detected_language",
                    "english",
                    "string",
                    datetime.now().isoformat(),
                    "gpt-4o-mini",
                    None,
                    run_a,
                    runs[0]["query_hash"],
                )
                for key_value in seed_keys
            ],
        )
    conn.execute(
        "UPDATE _enrichments SET value = 'mixed' WHERE run_id = ? AND field_name = 'detected_language'",
        (run_b,),
    )
    conn.commit()

    runs_result = runner.invoke(cli, [
        "runs",
        "--db-path", str(db_path),
        "--enrichment", "language_review",
    ])
    assert runs_result.exit_code == 0
    assert run_a[:8] in runs_result.output
    assert run_b[:8] in runs_result.output

    diff_result = runner.invoke(cli, [
        "diff-runs",
        "--db-path", str(db_path),
        "--run-a", run_a,
        "--run-b", run_b,
        "--limit", "5",
    ])
    assert diff_result.exit_code == 0
    assert "Disagreement cells" in diff_result.output
    assert "english" in diff_result.output
    assert "mixed" in diff_result.output

    export_path = temp_env["temp_dir"] / "language_overrides.csv"
    export_result = runner.invoke(cli, [
        "overrides-export",
        "--db-path", str(db_path),
        "--run-id", run_b,
        "--output", str(export_path),
    ])
    assert export_result.exit_code == 0
    assert export_path.exists()

    with export_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    assert "override__detected_language" in fieldnames
    assert rows

    rows[0]["override__detected_language"] = "other"
    rows[0]["note__detected_language"] = "Human review"
    with export_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    import_result = runner.invoke(cli, [
        "overrides-import",
        "--db-path", str(db_path),
        "--run-id", run_b,
        "--input", str(export_path),
        "--reviewer", "tester",
    ])
    assert import_result.exit_code == 0
    assert "Final view refreshed" in import_result.output

    override_row = conn.execute("""
        SELECT override_value, reviewer, note
        FROM _enrichment_overrides
        WHERE run_id = ?
        LIMIT 1
    """, (run_b,)).fetchone()
    assert override_row is not None
    assert override_row["override_value"] == "other"
    assert override_row["reviewer"] == "tester"
    assert override_row["note"] == "Human review"

    final_view_name = conn.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'view'
          AND name LIKE ?
        LIMIT 1
    """, (f"v_final_language_review_%_{run_b[:8]}",)).fetchone()["name"]
    final_row = conn.execute(
        f"SELECT detected_language, _has_override FROM {final_view_name} ORDER BY _row_order LIMIT 1"
    ).fetchone()
    assert final_row["detected_language"] == "other"
    assert final_row["_has_override"] == 1
    conn.close()

def test_where_clause_filters_existing_enrichment_query(temp_env, monkeypatch):
    """A where-clause selector should narrow the base enrichment query and create a distinct query hash."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "where_clause_filter.yml"

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    conn.execute("DELETE FROM documents")
    conn.executemany(
        "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
        [
            ("keep_sha1_1", "keep_alpha.txt", "alpha" * 80),
            ("drop_sha1_2", "drop_beta.txt", "beta" * 80),
        ],
    )
    conn.commit()
    conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {
            "all_docs": "SELECT rowid, sha1 FROM documents",
        },
        "enrichments": [
            {
                "name": "language_review",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["raw_content"],
                },
                "prompt": "Classify the language of this document.",
                "model": "gpt-4o-mini",
                "output_column": "detected_language",
                "schema": {
                    "detected_language": {
                        "type": "string",
                    }
                },
            }
        ],
    }

    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    async def mock_llm_v1(*args, **kwargs):
        return "english"

    async def mock_llm_v2(*args, **kwargs):
        return "filtered"

    monkeypatch.setattr('doctrail.llm_operations.call_llm', mock_llm_v1)
    first_run = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["language_review"],
        overwrite=True,
        skip_cost_check=True,
    ))
    assert first_run["status"] == "success"

    monkeypatch.setattr('doctrail.llm_operations.call_llm', mock_llm_v2)
    second_run = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["language_review"],
        overwrite=True,
        skip_cost_check=True,
        where_clause="filename LIKE 'keep%'",
    ))
    assert second_run["status"] == "success"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    runs = conn.execute("""
        SELECT run_id, query_hash, processed_rows
        FROM _enrichment_runs
        WHERE enrichment_name = 'language_review'
        ORDER BY command_started_at
    """).fetchall()
    assert len(runs) == 2
    run_b = runs[1]["run_id"]
    assert runs[0]["query_hash"] != runs[1]["query_hash"]
    assert runs[1]["processed_rows"] == 1

    run_b_keys = [
        row["key_value"]
        for row in conn.execute(
            "SELECT key_value FROM _enrichment_run_items WHERE run_id = ? ORDER BY row_order",
            (run_b,),
        ).fetchall()
    ]
    assert run_b_keys == ["keep_sha1_1"]

    run_b_value_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM _enrichments
        WHERE run_id = ? AND field_name = 'detected_language'
        """,
        (run_b,),
    ).fetchone()[0]
    assert run_b_value_count == 1
    conn.close()

def test_multi_model_run_returns_default_view_collapse_notice(temp_env, monkeypatch):
    """Multi-model sync enrichments should advertise the latest-wins default view."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "multi_model_notice.yml"

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    conn.execute(
        "INSERT OR REPLACE INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
        ("multi_sha1_1", "doc1.txt", "content " * 80),
    )
    conn.commit()
    conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {
            "all_docs": "SELECT rowid, sha1 FROM documents",
        },
        "enrichments": [
            {
                "name": "language_review",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["raw_content"],
                },
                "prompt": "Classify the language.",
                "model": ["gpt-4o-mini", "gpt-4o"],
                "output_column": "detected_language",
                "schema": {
                    "detected_language": {
                        "type": "string",
                    }
                },
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    async def mock_llm(*args, **kwargs):
        return "english"

    monkeypatch.setattr('doctrail.core_runtime.enrichment.validate_model', lambda *args, **kwargs: True)
    monkeypatch.setattr('doctrail.llm_operations.call_llm', mock_llm)

    result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["language_review"],
        overwrite=True,
        skip_cost_check=True,
    ))

    assert result["status"] == "success"
    assert len(result["run_artifacts"]) == 2
    assert "latest-write-wins" in result["multi_model_view_notice"]
    assert "doctrail view pivot <name> -e <enrichment> --by-model" in result["multi_model_view_notice"]

def test_integer_key_column_skip_logic_handles_non_string_keys(temp_env):
    """Re-running an enrichment with an integer key column should skip before creating a run."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "integer_key.yml"

    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS articles")
    conn.execute("""
        CREATE TABLE articles (
            article_id INTEGER PRIMARY KEY,
            title TEXT,
            raw_content TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO articles (article_id, title, raw_content) VALUES (?, ?, ?)",
        [
            (101, "First", "x" * 300),
            (102, "Second", "y" * 300),
        ],
    )
    conn.commit()
    conn.close()

    config = {
        "database": str(db_path),
        "default_table": "articles",
        "key_column": "article_id",
        "sql_queries": {
            "all_articles": "SELECT rowid, article_id FROM articles ORDER BY article_id",
        },
        "enrichments": [
            {
                "name": "article_summary",
                "input": {
                    "query": "all_articles",
                    "input_columns": ["raw_content"],
                },
                "prompt": "Summarize {raw_content}",
                "model": "gpt-4o-mini",
                "output_column": "summary",
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    first_run = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["article_summary"],
        overwrite=False,
        skip_cost_check=True,
    ))
    second_run = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["article_summary"],
        overwrite=False,
        skip_cost_check=True,
    ))

    assert first_run["status"] == "success"
    assert second_run["status"] == "success"
    assert second_run["total_processed"] == 0
    assert "run_artifacts" not in second_run

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM _enrichment_runs
        WHERE enrichment_name = 'article_summary'
        """,
    ).fetchone()
    conn.close()

    assert run_count[0] == 1


def test_review_items_respect_configured_key_column(temp_env):
    """Review sampling should join enrichments through the configured source key."""
    from doctrail.db_operations import ensure_enrichments_table
    from doctrail.review_server import get_review_items

    db_path = temp_env["db_path"]
    ensure_enrichments_table(str(db_path))

    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS articles")
    conn.execute("""
        CREATE TABLE articles (
            article_id INTEGER PRIMARY KEY,
            title TEXT,
            filename TEXT,
            raw_content TEXT
        )
    """)
    conn.execute(
        "INSERT INTO articles (article_id, title, filename, raw_content) VALUES (?, ?, ?, ?)",
        (101, "Article title", "article.txt", "review content"),
    )
    conn.execute(
        """
        INSERT INTO _enrichments (
            key_value, enrichment_name, field_name, value, value_type,
            timestamp, model, prompt_hash, enrichment_id
        ) VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)
        """,
        ("101", "stance_review", "stance", "supportive", "string", "gpt-4o-mini", "prompt", "enrich-101"),
    )
    conn.commit()
    conn.close()

    items, _truncate = get_review_items(
        str(db_path),
        "stance",
        sample_per_class=10,
        table_name="articles",
        key_column="article_id",
    )

    assert len(items) == 1
    assert items[0]["sha1"] == "101"
    assert items[0]["title"] == "Article title"


def test_export_output_names_are_sanitized_under_output_dir(tmp_path):
    """User-configurable export naming should not create paths outside output_dir."""
    from doctrail.export_operations import get_output_filename, safe_output_path

    output_dir = tmp_path / "exports"
    output_dir.mkdir()

    basename = get_output_filename(
        {"sha1": "abc123", "title": "../../outside:report"},
        "{title}",
        "doc_{sha1}",
    )
    output_path = safe_output_path(output_dir, basename, "md")

    assert "/" not in basename
    assert "\\" not in basename
    assert ".." not in basename
    assert output_path.parent == output_dir.resolve()
    assert output_path.name.endswith(".md")


def test_execute_query_optimized_uses_explicit_default_table(tmp_path):
    """Optimized fetches should use the caller-provided default table instead of guessing from SQL text."""
    from doctrail.db_operations import execute_query_optimized

    db_path = tmp_path / "optimized_default_table.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE literature (
            sha1 TEXT PRIMARY KEY,
            title TEXT,
            raw_content TEXT
        )
    """)
    conn.execute(
        "INSERT INTO literature (sha1, title, raw_content) VALUES (?, ?, ?)",
        ("lit-1", "Paper", "full text"),
    )
    conn.commit()
    conn.close()

    rows = execute_query_optimized(
        str(db_path),
        "SELECT sha1 FROM literature",
        ["raw_content"],
        key_column="sha1",
        default_table="literature",
    )

    assert rows == [{"sha1": "lit-1", "rowid": 1, "raw_content": "full text"}]


def test_execute_query_optimized_raises_on_sql_error(tmp_path):
    """A failed optimized query should raise instead of silently running with partial inputs."""
    from doctrail.db_operations import execute_query_optimized

    db_path = tmp_path / "optimized_error.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE documents (sha1 TEXT PRIMARY KEY, raw_content TEXT)")
    conn.execute("INSERT INTO documents (sha1, raw_content) VALUES (?, ?)", ("doc-1", "hello"))
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.Error):
        execute_query_optimized(
            str(db_path),
            "SELECT sha1, missing_column FROM documents",
            ["raw_content"],
            key_column="sha1",
            default_table="documents",
        )


def test_migrate_columns_to_enrichments_uses_detected_key_column(tmp_path):
    """Column migration should use the real source key and avoid migrating the key itself as an enrichment."""
    from doctrail.db_operations import migrate_columns_to_enrichments

    db_path = tmp_path / "migrate_detect_key.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE docs (attachment_sha1 TEXT PRIMARY KEY, sentiment TEXT)")
    conn.execute("INSERT INTO docs (attachment_sha1, sentiment) VALUES (?, ?)", ("abc", "positive"))
    conn.commit()
    conn.close()

    migrated = migrate_columns_to_enrichments(str(db_path), source_table="docs")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT key_value, enrichment_name, field_name, value
        FROM _enrichments
        ORDER BY enrichment_name, field_name
        """
    ).fetchall()
    conn.close()

    assert migrated == 1
    assert [dict(row) for row in rows] == [
        {
            "key_value": "abc",
            "enrichment_name": "sentiment",
            "field_name": "sentiment",
            "value": "positive",
        }
    ]
