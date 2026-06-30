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


def test_anthropic_batch_submission_and_poll_success(temp_env, monkeypatch):
    """Direct Anthropic models should submit, poll, and reconcile through the existing batch flow."""
    from doctrail.core import run_enrichment, poll_batch_runs

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "anthropic_batch.yml"

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
        ("anthropic_sha1_1", "anthropic.txt", "alpha"),
    )
    conn.commit()
    conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {"all_docs": "SELECT rowid, sha1 FROM documents ORDER BY sha1"},
        "enrichments": [
            {
                "name": "batch_summary",
                "input": {"query": "all_docs", "input_columns": ["filename"]},
                "prompt": "Summarize {filename}",
                "model": "claude-sonnet-4",
                "output_column": "summary",
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")
    backend = FakeAnthropicBatchBackend(provider)
    monkeypatch.setattr("doctrail.core._get_batch_provider", lambda model: provider)

    submit_result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["batch_summary"],
        overwrite=True,
        skip_cost_check=True,
        execution_mode="batch",
    ))

    assert submit_result["status"] == "submitted"
    artifact = submit_result["run_artifacts"][0]
    run_id = artifact["run_id"]
    batch_job = artifact["batch_jobs"][0]
    batch_id = batch_job["provider_batch_id"]
    assert batch_job["provider"] == "anthropic"
    assert backend.submitted_batches[0]["requests"][0]["custom_id"] == "row_0"
    assert backend.submitted_batches[0]["requests"][0]["params"]["model"] == "claude-sonnet-4"
    assert backend.submitted_batches[0]["requests"][0]["params"]["output_config"]["format"]["type"] == "json_schema"

    backend.set_batch_result(
        batch_id,
        status="ended",
        results=[
            {
                "custom_id": "row_0",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "content": [{"type": "text", "text": json.dumps({"summary": "anthropic summary"})}],
                        "usage": {"input_tokens": 10, "output_tokens": 2},
                        "model": "claude-sonnet-4",
                        "role": "assistant",
                        "type": "message",
                    },
                },
            }
        ],
    )

    poll_result = asyncio.run(poll_batch_runs(
        db_path=str(db_path),
        run_id=run_id,
        watch=False,
    ))
    assert poll_result["status"] == "success"
    assert poll_result["pending_jobs"] == 0
    assert poll_result["success_count"] == 1
    assert poll_result["error_count"] == 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_row = conn.execute(
        "SELECT status, processed_rows, error_count, estimated_cost FROM _enrichment_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    batch_row = conn.execute(
        "SELECT provider, status, metadata FROM _enrichment_batch_jobs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    enrichment_row = conn.execute(
        """
        SELECT value
        FROM _enrichments
        WHERE run_id = ? AND enrichment_name = 'batch_summary' AND field_name = 'summary'
        """,
        (run_id,),
    ).fetchone()
    conn.close()

    metadata = json.loads(batch_row["metadata"])
    assert run_row["status"] == "completed"
    assert run_row["processed_rows"] == 1
    assert run_row["error_count"] == 0
    assert run_row["estimated_cost"] > 0
    assert batch_row["provider"] == "anthropic"
    assert batch_row["status"] == "ended"
    assert metadata["request_counts"]["succeeded"] == 1
    assert enrichment_row["value"] == "anthropic summary"

def test_anthropic_batch_submission_warns_about_integer_bounds(temp_env, monkeypatch, capsys):
    """Anthropic batch runs should surface provider schema compatibility warnings before submission."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "anthropic_batch_schema_warning.yml"

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
        ("anthropic_warn_sha1_1", "anthropic_warn.txt", "alpha"),
    )
    conn.commit()
    conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {"all_docs": "SELECT rowid, sha1 FROM documents ORDER BY sha1"},
        "enrichments": [
            {
                "name": "bounded_integer_batch",
                "input": {"query": "all_docs", "input_columns": ["filename"]},
                "prompt": "Score {filename}",
                "model": "claude-sonnet-4",
                "schema": {
                    "menace_level": {"type": "integer", "minimum": 0, "maximum": 3},
                    "explanation": {"type": "string"},
                },
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")
    backend = FakeAnthropicBatchBackend(provider)
    monkeypatch.setattr("doctrail.core._get_batch_provider", lambda model: provider)

    submit_result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["bounded_integer_batch"],
        overwrite=True,
        skip_cost_check=True,
        execution_mode="batch",
    ))
    captured = capsys.readouterr()

    assert submit_result["status"] == "submitted"
    warning_text = f"{captured.out}\n{captured.err}"
    assert "anthropic batch schema compatibility" in warning_text
    assert "$.properties.menace_level.minimum" in warning_text
    assert "$.properties.menace_level.maximum" in warning_text

    menace_schema = backend.submitted_batches[0]["requests"][0]["params"]["output_config"]["format"]["schema"]["properties"]["menace_level"]
    assert menace_schema["type"] == "integer"
    assert "minimum" not in menace_schema
    assert "maximum" not in menace_schema

def test_anthropic_batch_poll_serializes_error_objects(temp_env, monkeypatch):
    """Anthropic poll should persist SDK-like error objects without crashing JSON serialization."""
    from doctrail.core import run_enrichment, poll_batch_runs

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "anthropic_batch_error_object.yml"

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
        ("anthropic_error_sha1_1", "anthropic_error.txt", "alpha"),
    )
    conn.commit()
    conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {"all_docs": "SELECT rowid, sha1 FROM documents ORDER BY sha1"},
        "enrichments": [
            {
                "name": "batch_summary",
                "input": {"query": "all_docs", "input_columns": ["filename"]},
                "prompt": "Summarize {filename}",
                "model": "claude-sonnet-4",
                "output_column": "summary",
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")
    backend = FakeAnthropicBatchBackend(provider)
    monkeypatch.setattr("doctrail.core._get_batch_provider", lambda model: provider)

    submit_result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["batch_summary"],
        overwrite=True,
        skip_cost_check=True,
        execution_mode="batch",
    ))

    run_id = submit_result["run_artifacts"][0]["run_id"]
    batch_id = submit_result["run_artifacts"][0]["batch_jobs"][0]["provider_batch_id"]
    backend.set_batch_result(
        batch_id,
        status="ended",
        results=[
            {
                "custom_id": "row_0",
                "result": {
                    "type": "errored",
                    "error": SimpleNamespace(
                        type="invalid_request_error",
                        message="bad schema",
                    ),
                },
            }
        ],
    )

    poll_result = asyncio.run(poll_batch_runs(
        db_path=str(db_path),
        run_id=run_id,
        watch=False,
    ))

    assert poll_result["status"] == "error"
    assert poll_result["pending_jobs"] == 0
    assert poll_result["success_count"] == 0
    assert poll_result["error_count"] == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_row = conn.execute(
        "SELECT status, processed_rows, error_count FROM _enrichment_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    audit_row = conn.execute(
        "SELECT raw_json FROM _enrichment_audit WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    conn.close()

    raw_payload = json.loads(audit_row["raw_json"])
    assert run_row["status"] == "failed"
    assert run_row["processed_rows"] == 0
    assert run_row["error_count"] == 1
    assert raw_payload["type"] == "invalid_request_error"
    assert raw_payload["message"] == "bad schema"

def test_cli_batch_cancel_requests_anthropic_provider_cancellation(temp_env, monkeypatch):
    """The batch cancel CLI should call Anthropic's native cancel endpoint for direct Claude models."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "anthropic_batch_cancel.yml"

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
        ("anthropic_cancel_sha1_1", "cancel.txt", "cancel me"),
    )
    conn.commit()
    conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {"all_docs": "SELECT rowid, sha1 FROM documents"},
        "enrichments": [
            {
                "name": "batch_summary",
                "input": {"query": "all_docs", "input_columns": ["filename"]},
                "prompt": "Summarize {filename}",
                "model": "claude-sonnet-4",
                "output_column": "summary",
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")
    backend = FakeAnthropicBatchBackend(provider)
    monkeypatch.setattr("doctrail.core._get_batch_provider", lambda model: provider)

    submit_result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["batch_summary"],
        overwrite=True,
        skip_cost_check=True,
        execution_mode="batch",
    ))
    run_id = submit_result["run_artifacts"][0]["run_id"]
    batch_id = submit_result["run_artifacts"][0]["batch_jobs"][0]["provider_batch_id"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "batch",
        "cancel",
        "--db-path", str(db_path),
        "--run-id", run_id,
    ])

    assert result.exit_code == 0
    assert backend.batch_records[batch_id]["processing_status"] == "canceling"

def test_gemini_batch_submission_and_poll_success(temp_env, monkeypatch):
    """Direct Gemini models should submit, poll, and reconcile through the existing batch flow."""
    from doctrail.core import run_enrichment, poll_batch_runs

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "gemini_batch.yml"

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
        ("gemini_sha1_1", "gemini.txt", "alpha"),
    )
    conn.commit()
    conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {"all_docs": "SELECT rowid, sha1 FROM documents ORDER BY sha1"},
        "enrichments": [
            {
                "name": "batch_summary",
                "input": {"query": "all_docs", "input_columns": ["filename"]},
                "prompt": "Summarize {filename}",
                "model": "gemini-2.5-flash",
                "output_column": "summary",
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    provider = GeminiProvider(api_key="test-key", model="gemini-2.5-flash")
    backend = FakeGeminiBatchBackend(provider)
    monkeypatch.setattr("doctrail.core._get_batch_provider", lambda model: provider)

    submit_result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["batch_summary"],
        overwrite=True,
        skip_cost_check=True,
        execution_mode="batch",
    ))

    assert submit_result["status"] == "submitted"
    artifact = submit_result["run_artifacts"][0]
    run_id = artifact["run_id"]
    batch_job = artifact["batch_jobs"][0]
    batch_id = batch_job["provider_batch_id"]
    assert batch_job["provider"] == "gemini"
    assert batch_job["input_file_id"].startswith("files/input-")
    assert backend.submitted_batches[0]["requests"][0]["key"] == "row_0"
    assert backend.submitted_batches[0]["requests"][0]["request"]["generation_config"]["response_mime_type"] == "application/json"

    backend.set_batch_result(
        batch_id,
        status="BATCH_STATE_SUCCEEDED",
        result_lines=[
            {
                "key": "row_0",
                "response": {
                    "candidates": [{
                        "content": {
                            "parts": [{"text": json.dumps({"summary": "gemini summary"})}]
                        }
                    }],
                    "usageMetadata": {
                        "promptTokenCount": 12,
                        "candidatesTokenCount": 3,
                    },
                },
            }
        ],
    )

    poll_result = asyncio.run(poll_batch_runs(
        db_path=str(db_path),
        run_id=run_id,
        watch=False,
    ))
    assert poll_result["status"] == "success"
    assert poll_result["pending_jobs"] == 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_row = conn.execute(
        "SELECT status, processed_rows, error_count, estimated_cost FROM _enrichment_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    batch_row = conn.execute(
        "SELECT provider, status, metadata FROM _enrichment_batch_jobs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    enrichment_row = conn.execute(
        """
        SELECT value
        FROM _enrichments
        WHERE run_id = ? AND enrichment_name = 'batch_summary' AND field_name = 'summary'
        """,
        (run_id,),
    ).fetchone()
    conn.close()

    metadata = json.loads(batch_row["metadata"])
    assert run_row["status"] == "completed"
    assert run_row["processed_rows"] == 1
    assert run_row["error_count"] == 0
    assert run_row["estimated_cost"] > 0
    assert batch_row["provider"] == "gemini"
    assert batch_row["status"] == "BATCH_STATE_SUCCEEDED"
    assert metadata["request_counts"]["succeeded"] == 1
    assert enrichment_row["value"] == "gemini summary"

def test_cli_batch_cancel_requests_gemini_provider_cancellation(temp_env, monkeypatch):
    """The batch cancel CLI should call Gemini's native cancel endpoint for direct models."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "gemini_batch_cancel.yml"

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
        ("gemini_cancel_sha1_1", "cancel.txt", "cancel me"),
    )
    conn.commit()
    conn.close()

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "sql_queries": {"all_docs": "SELECT rowid, sha1 FROM documents"},
        "enrichments": [
            {
                "name": "batch_summary",
                "input": {"query": "all_docs", "input_columns": ["filename"]},
                "prompt": "Summarize {filename}",
                "model": "gemini-2.5-flash",
                "output_column": "summary",
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    provider = GeminiProvider(api_key="test-key", model="gemini-2.5-flash")
    backend = FakeGeminiBatchBackend(provider)
    monkeypatch.setattr("doctrail.core._get_batch_provider", lambda model: provider)

    submit_result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["batch_summary"],
        overwrite=True,
        skip_cost_check=True,
        execution_mode="batch",
    ))
    run_id = submit_result["run_artifacts"][0]["run_id"]
    batch_id = submit_result["run_artifacts"][0]["batch_jobs"][0]["provider_batch_id"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "batch",
        "cancel",
        "--db-path", str(db_path),
        "--run-id", run_id,
    ])

    conn = sqlite3.connect(db_path)
    metadata = json.loads(conn.execute(
        "SELECT metadata FROM _enrichment_batch_jobs WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0])
    conn.close()

    assert result.exit_code == 0
    assert backend.batch_records[batch_id]["state"] == "BATCH_STATE_CANCELLED"
    assert metadata["request_counts"]["canceled"] == 1
