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


def test_openai_batch_submission_shards_large_run(temp_env, monkeypatch):
    """Batch submission should shard large runs deterministically at the provider contract boundary."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "batch_large.yml"

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    conn.execute("DELETE FROM documents")
    for start in range(0, 100001, 5000):
        batch = [
            (f"sha_{idx:06d}", f"doc_{idx:06d}.txt", "x")
            for idx in range(start, min(start + 5000, 100001))
        ]
        conn.executemany(
            "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
            batch,
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
                "name": "batch_summary",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["filename"],
                },
                "prompt": "Summarize {filename}",
                "model": "gpt-4o-mini",
                "output_column": "summary",
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    provider = OpenAIProvider(api_key="test-key", model="gpt-4o-mini")
    backend = FakeOpenAIBatchBackend(provider)
    monkeypatch.setattr("doctrail.core._get_batch_provider", lambda model: provider)

    result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["batch_summary"],
        overwrite=True,
        skip_cost_check=True,
        execution_mode="batch",
    ))

    assert result["status"] == "submitted"
    assert len(backend.uploaded_files) == 3
    artifact = result["run_artifacts"][0]
    request_counts = [job["request_count"] for job in artifact["batch_jobs"]]
    assert request_counts == [50000, 50000, 1]
    assert sum(request_counts) == 100001
    assert all(len(upload["lines"]) == expected for upload, expected in zip(backend.uploaded_files, request_counts))


def test_openai_batch_submission_failure_finalizes_run(temp_env, monkeypatch):
    """A provider submit failure should mark the run failed instead of leaving it submitted forever."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "batch_submit_failure.yml"

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
        ("submit_fail_sha1", "doc.txt", "alpha beta gamma"),
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
                "name": "batch_summary",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["filename"],
                },
                "prompt": "Summarize {filename}",
                "model": "gpt-4o-mini",
                "output_column": "summary",
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    provider = OpenAIProvider(api_key="test-key", model="gpt-4o-mini")
    backend = FakeOpenAIBatchBackend(provider)

    async def failing_create(*args, **kwargs):
        raise RuntimeError("submit failed")

    provider.client.batches.create = failing_create
    monkeypatch.setattr("doctrail.core._get_batch_provider", lambda model: provider)

    with pytest.raises(Exception, match="submit failed"):
        asyncio.run(run_enrichment(
            config_path=str(config_path),
            enrichments=["batch_summary"],
            overwrite=True,
            skip_cost_check=True,
            execution_mode="batch",
        ))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_row = conn.execute(
        """
        SELECT status, total_rows, processed_rows, error_count, finished_at
        FROM _enrichment_runs
        ORDER BY rowid DESC
        LIMIT 1
        """
    ).fetchone()
    batch_jobs = conn.execute("SELECT COUNT(*) FROM _enrichment_batch_jobs").fetchone()[0]
    item_statuses = conn.execute(
        "SELECT DISTINCT status FROM _enrichment_run_items"
    ).fetchall()
    conn.close()

    assert backend.uploaded_files, "The test should fail after file upload, during provider batch submission"
    assert run_row["status"] == "failed"
    assert run_row["total_rows"] == 1
    assert run_row["processed_rows"] == 0
    assert run_row["error_count"] == 1
    assert run_row["finished_at"] is not None
    assert batch_jobs == 0
    assert {row[0] for row in item_statuses} == {"error"}

def test_openai_batch_poll_recovers_after_partial_failure(temp_env, monkeypatch):
    """Polling a completed batch twice should fill missed rows without duplicating existing results."""
    from doctrail.core import run_enrichment, poll_batch_runs

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "batch_recover.yml"

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
            ("batch_sha1_1", "doc1.txt", "alpha"),
            ("batch_sha1_2", "doc2.txt", "beta"),
            ("batch_sha1_3", "doc3.txt", "gamma"),
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
                "name": "batch_summary",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["filename"],
                },
                "prompt": "Summarize {filename}",
                "model": "gpt-4o-mini",
                "output_column": "summary",
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    provider = OpenAIProvider(api_key="test-key", model="gpt-4o-mini")
    backend = FakeOpenAIBatchBackend(provider)
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
    batch_id = artifact["batch_jobs"][0]["provider_batch_id"]

    def _success_line(row_order: int, text: str) -> dict:
        return {
            "custom_id": f"{run_id}:{row_order}",
            "response": {
                "status_code": 200,
                "body": {
                    "choices": [{"message": {"content": json.dumps({"summary": text})}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                },
            },
        }

    backend.set_batch_result(
        batch_id,
        status="completed",
        output_lines=[
            _success_line(2, "summary-3"),
            _success_line(0, "summary-1"),
        ],
        error_lines=[
            {
                "custom_id": f"{run_id}:1",
                "error": {"message": "bad row"},
            }
        ],
    )
    backend.retrieve_fail_once.add(f"{batch_id}_error")

    first_poll = asyncio.run(poll_batch_runs(
        db_path=str(db_path),
        run_id=run_id,
        watch=False,
    ))
    assert first_poll["status"] == "partial"
    assert first_poll["pending_jobs"] == 1

    conn = sqlite3.connect(db_path)
    first_audit_count = conn.execute(
        "SELECT COUNT(*) FROM _enrichment_audit WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]
    conn.close()
    assert first_audit_count == 2

    second_poll = asyncio.run(poll_batch_runs(
        db_path=str(db_path),
        run_id=run_id,
        watch=False,
    ))
    assert second_poll["status"] == "error"
    assert second_poll["pending_jobs"] == 0
    assert second_poll["success_count"] == 2
    assert second_poll["error_count"] == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    audit_count = conn.execute(
        "SELECT COUNT(*) FROM _enrichment_audit WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]
    enrichment_count = conn.execute(
        "SELECT COUNT(*) FROM _enrichments WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]
    run_status = conn.execute(
        "SELECT status FROM _enrichment_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()["status"]
    item_statuses = {
        row["status"]: row["count"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM _enrichment_run_items WHERE run_id = ? GROUP BY status",
            (run_id,),
        ).fetchall()
    }
    conn.close()

    assert audit_count == 3
    assert enrichment_count == 2
    assert run_status == "completed_with_errors"
    assert item_statuses["processed"] == 2
    assert item_statuses["error"] == 1

def test_openai_batch_submission_defaults_gpt5_reasoning_effort_to_minimal(temp_env, monkeypatch):
    """GPT-5 batch submission should include a minimal reasoning_effort by default."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "batch_reasoning_default.yml"

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
        ("reasoning_sha1_1", "doc1.txt", "x" * 300),
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
                "input": {"query": "all_docs", "input_columns": ["raw_content"]},
                "prompt": "Summarize {raw_content}",
                "model": "gpt-5-mini",
                "output_column": "summary",
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    provider = OpenAIProvider(api_key="test-key", model="gpt-5-mini")
    backend = FakeOpenAIBatchBackend(provider)
    monkeypatch.setattr("doctrail.core._get_batch_provider", lambda model: provider)

    result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["batch_summary"],
        overwrite=True,
        skip_cost_check=True,
        execution_mode="batch",
    ))

    assert result["status"] == "submitted"
    first_line = json.loads(backend.uploaded_files[0]["lines"][0])
    assert first_line["body"]["reasoning_effort"] == "minimal"

def test_openai_batch_reconcile_accepts_null_nested_fields(temp_env, monkeypatch):
    """Nulls inside nested structured batch items should reconcile when the rest of the payload is valid."""
    from doctrail.core import run_enrichment, poll_batch_runs

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "batch_nullable_nested.yml"

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
        ("nullable_sha1_1", "nullable.txt", "alpha"),
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
                "name": "incident_extract",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["filename"],
                },
                "prompt": "Extract incidents from {filename}",
                "model": "gpt-4o-mini",
                "schema": {
                    "incidents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "location_city_en": "string",
                                "location_province_en": {
                                    "enum": ["Beijing", "Shanghai"],
                                },
                                "should_keep": "boolean",
                            },
                        },
                    },
                },
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    provider = OpenAIProvider(api_key="test-key", model="gpt-4o-mini")
    backend = FakeOpenAIBatchBackend(provider)
    monkeypatch.setattr("doctrail.core._get_batch_provider", lambda model: provider)

    submit_result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["incident_extract"],
        overwrite=True,
        skip_cost_check=True,
        execution_mode="batch",
    ))
    run_id = submit_result["run_artifacts"][0]["run_id"]
    batch_id = submit_result["run_artifacts"][0]["batch_jobs"][0]["provider_batch_id"]

    backend.set_batch_result(
        batch_id,
        status="completed",
        output_lines=[
            {
                "custom_id": f"{run_id}:0",
                "response": {
                    "status_code": 200,
                    "body": {
                        "choices": [{
                            "message": {
                                "content": json.dumps({
                                    "incidents": [
                                        {
                                            "location_city_en": "Beijing",
                                            "location_province_en": None,
                                            "should_keep": True,
                                        }
                                    ]
                                })
                            }
                        }],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
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
        "SELECT status, processed_rows, error_count FROM _enrichment_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    enrichment_row = conn.execute(
        """
        SELECT value
        FROM _enrichments
        WHERE run_id = ? AND enrichment_name = 'incident_extract' AND field_name = 'incidents'
        """,
        (run_id,),
    ).fetchone()
    conn.close()

    assert run_row["status"] == "completed"
    assert run_row["processed_rows"] == 1
    assert run_row["error_count"] == 0
    incidents = json.loads(enrichment_row["value"])
    assert incidents[0]["location_province_en"] is None

@pytest.mark.parametrize("batch_command", ["poll", "watch"])
def test_cli_batch_polling_exits_nonzero_on_row_errors(temp_env, monkeypatch, batch_command):
    """Batch poll/watch should fail loudly while preserving provider usage."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "batch_provider_usage.yml"

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
            ("usage_sha1_1", "doc1.txt", "alpha"),
            ("usage_sha1_2", "doc2.txt", "beta"),
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
                "name": "batch_summary",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["filename"],
                },
                "prompt": "Summarize {filename}",
                "model": "gpt-4o-mini",
                "output_column": "summary",
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    provider = OpenAIProvider(api_key="test-key", model="gpt-4o-mini")
    backend = FakeOpenAIBatchBackend(provider)
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

    batch_catalog = get_openai_batch_model_info("gpt-4o-mini")
    cached_tokens = 10
    total_input_tokens = 40
    total_output_tokens = 8
    expected_cost = (
        ((total_input_tokens - cached_tokens) / 1_000_000) * batch_catalog["batch_input"]
        + (cached_tokens / 1_000_000) * batch_catalog["batch_cached_input"]
        + (total_output_tokens / 1_000_000) * batch_catalog["batch_output"]
    )

    backend.set_batch_result(
        batch_id,
        status="completed",
        output_lines=[
            {
                "custom_id": f"{run_id}:0",
                "response": {
                    "status_code": 200,
                    "body": {
                        "choices": [{"message": {"content": json.dumps({"summary": "summary-1"})}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                    },
                },
            },
            {
                "custom_id": f"{run_id}:1",
                "response": {
                    "status_code": 200,
                    "body": {
                        "choices": [{"message": {"content": json.dumps({"summary": ["wrong-type"]})}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                    },
                },
            },
        ],
        usage={
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "input_tokens_details": {"cached_tokens": cached_tokens},
        },
    )

    runner = CliRunner()
    poll_result = runner.invoke(cli, [
        "batch",
        batch_command,
        "--db-path", str(db_path),
        "--run-id", run_id,
    ])
    assert poll_result.exit_code == 1, poll_result.output
    assert f"{run_id}: status=completed_with_errors processed=1 errors=1" in poll_result.output
    assert "Batch completed with errors: 1 succeeded, 1 errored across 1 run(s)" in poll_result.output

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_row = conn.execute(
        """
        SELECT status, processed_rows, error_count, input_tokens, output_tokens, estimated_cost
        FROM _enrichment_runs
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    job_row = conn.execute(
        "SELECT metadata FROM _enrichment_batch_jobs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    audit_count = conn.execute(
        "SELECT COUNT(*) FROM _enrichment_audit WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]
    conn.close()

    metadata = json.loads(job_row["metadata"])
    assert metadata["provider_usage"]["cached_input_tokens"] == cached_tokens
    assert run_row["status"] == "completed_with_errors"
    assert run_row["processed_rows"] == 1
    assert run_row["error_count"] == 1
    assert run_row["input_tokens"] == total_input_tokens
    assert run_row["output_tokens"] == total_output_tokens
    assert run_row["estimated_cost"] == pytest.approx(expected_cost)
    assert audit_count == 2

def test_cli_batch_cancel_requests_provider_cancellation(temp_env, monkeypatch):
    """The CLI batch cancel command should cancel active provider jobs for a run."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "batch_cancel.yml"

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
        ("cancel_sha1_1", "cancel.txt", "cancel me"),
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
                "name": "batch_summary",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["filename"],
                },
                "prompt": "Summarize {filename}",
                "model": "gpt-4o-mini",
                "output_column": "summary",
            }
        ],
    }
    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    provider = OpenAIProvider(api_key="test-key", model="gpt-4o-mini")
    backend = FakeOpenAIBatchBackend(provider)
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
    assert backend.batch_records[batch_id]["status"] == "cancelling"
