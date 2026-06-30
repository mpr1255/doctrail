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


def test_view_create_with_run_id(temp_env):
    """`doctrail view create --run-id` should recreate the default run review view."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "view_run_id.yml"

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
            ("view_seed_1", "doc1.txt", "x" * 300),
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
                "name": "view_review",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["raw_content"],
                },
                "prompt": "Review this document.",
                "model": "gpt-4o-mini",
                "output_column": "review_label",
                "schema": {
                    "review_label": {
                        "type": "string",
                    }
                },
            }
        ],
    }

    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["view_review"],
        overwrite=True,
        skip_cost_check=True,
    ))
    assert result["status"] == "success"
    assert result.get("run_artifacts")
    run_id = result["run_artifacts"][0]["run_id"]

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(temp_env["temp_dir"])):
        project_dir = Path(".doctrail")
        project_dir.mkdir(exist_ok=True)
        with open(project_dir / "config.yml", "w") as handle:
            yaml.dump({"database": str(db_path), "default_table": "documents", "key_column": "sha1"}, handle)

        cli_result = runner.invoke(cli, [
            "view",
            "create",
            "--run-id", run_id,
        ])

    assert cli_result.exit_code == 0, cli_result.output
    assert "Created view:" in cli_result.output
    view_name = cli_result.output.split("Created view:", 1)[1].splitlines()[0].strip()

    conn = sqlite3.connect(db_path)
    created = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'view' AND name = ?",
        (view_name,),
    ).fetchone()
    assert created is not None
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({view_name})").fetchall()]
    assert "review_label" in columns
    assert "_run_id" in columns
    conn.close()

def test_finalize_materializes_editable_table_from_run_id(temp_env):
    """`doctrail finalize --run-id` should create a writable final table without duplicate machine columns."""
    from doctrail.core import run_enrichment

    db_path = temp_env["db_path"]
    config_path = temp_env["temp_dir"] / "finalize_run.yml"

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
            ("finalize_seed_1", "doc1.txt", "x" * 300),
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
                "name": "finalize_review",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["raw_content"],
                },
                "prompt": "Review this document.",
                "model": "gpt-4o-mini",
                "output_column": "review_label",
                "schema": {
                    "review_label": {
                        "type": "string",
                    }
                },
            }
        ],
    }

    with open(config_path, "w") as handle:
        yaml.dump(config, handle)

    result = asyncio.run(run_enrichment(
        config_path=str(config_path),
        enrichments=["finalize_review"],
        overwrite=True,
        skip_cost_check=True,
    ))
    assert result["status"] == "success"
    run_id = result["run_artifacts"][0]["run_id"]

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(temp_env["temp_dir"])):
        project_dir = Path(".doctrail")
        project_dir.mkdir(exist_ok=True)
        with open(project_dir / "config.yml", "w") as handle:
            yaml.dump({"database": str(db_path), "default_table": "documents", "key_column": "sha1"}, handle)

        cli_result = runner.invoke(cli, [
            "finalize",
            "--run-id", run_id,
            "--table", "final_review_table",
        ])

    assert cli_result.exit_code == 0, cli_result.output
    assert "Created editable table: final_review_table" in cli_result.output

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    table_row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'final_review_table'"
    ).fetchone()
    assert table_row is not None
    columns = [row[1] for row in conn.execute("PRAGMA table_info(final_review_table)").fetchall()]
    assert "review_label" in columns
    assert "_run_id" in columns
    assert not any(col.startswith("machine_") for col in columns)

    conn.execute("UPDATE final_review_table SET review_label = 'human_corrected'")
    conn.commit()
    updated = conn.execute("SELECT review_label FROM final_review_table").fetchone()[0]
    assert updated == "human_corrected"

    registry = conn.execute("""
        SELECT source_run_id, source_view
        FROM _doctrail_final_tables
        WHERE table_name = 'final_review_table'
    """).fetchone()
    conn.close()

    assert registry is not None
    assert registry["source_run_id"] == run_id
    assert registry["source_view"].startswith("v_final_")


def test_run_and_pivot_views_cast_numeric_fields_and_break_timestamp_ties(temp_env):
    from doctrail.db_operations import (
        create_pivot_view,
        create_run_view,
        ensure_enrichments_table,
        ensure_run_tracking_tables,
        materialize_run_inputs,
        start_enrichment_run,
    )

    db_path = temp_env["db_path"]
    ensure_enrichments_table(str(db_path))
    ensure_run_tracking_tables(str(db_path))

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS numeric_documents (
            sha1 TEXT PRIMARY KEY,
            title TEXT
        )
    """)
    conn.execute(
        "INSERT OR REPLACE INTO numeric_documents (sha1, title) VALUES (?, ?)",
        ("numeric_doc_1", "Numeric doc"),
    )
    conn.commit()
    conn.close()

    run_id = "numeric_run_12345678"
    started_at = datetime.now().isoformat()
    start_enrichment_run(
        db_path=str(db_path),
        run_id=run_id,
        command_started_at=started_at,
        enrichment_name="numeric_review",
        model="gpt-4o-mini",
        prompt_id="numeric_prompt",
        query_sql="SELECT sha1, title FROM numeric_documents",
        query_hash="numeric_query",
        key_column="sha1",
        source_name="numeric_documents",
    )
    materialize_run_inputs(
        str(db_path),
        run_id,
        [{"sha1": "numeric_doc_1", "title": "Numeric doc"}],
        "sha1",
        enabled=True,
    )

    tied_timestamp = "2026-06-10T12:00:00"
    conn = sqlite3.connect(db_path)
    conn.executemany(
        """
        INSERT INTO _enrichments (
            key_value, enrichment_name, field_name, value, value_type,
            timestamp, model, prompt_hash, run_id, query_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "numeric_doc_1",
                "numeric_review",
                "score",
                "2",
                "integer",
                tied_timestamp,
                "gpt-4o",
                "numeric_prompt",
                run_id,
                "numeric_query",
            ),
            (
                "numeric_doc_1",
                "numeric_review",
                "score",
                "10",
                "integer",
                tied_timestamp,
                "gpt-4o-mini",
                "numeric_prompt",
                run_id,
                "numeric_query",
            ),
        ],
    )
    conn.commit()
    conn.close()

    run_view = create_run_view(str(db_path), run_id=run_id)
    pivot_view = create_pivot_view(
        str(db_path),
        "numeric_pivot",
        "numeric_review",
        documents_table="numeric_documents",
        fields=["score"],
        include_columns=["title"],
    )

    conn = sqlite3.connect(db_path)
    run_row = conn.execute(
        f"SELECT score, typeof(score) FROM {run_view} WHERE score > 3"
    ).fetchone()
    pivot_row = conn.execute(
        f"SELECT score, typeof(score) FROM {pivot_view['view_name']} WHERE score > 3"
    ).fetchone()
    conn.close()

    assert run_row == (10, "integer")
    assert pivot_row == (10, "integer")

def test_view_spec_explodes_array_objects_and_renders_html(temp_env):
    """A YAML view spec should explode array-of-object enrichments into repeated keyed rows."""
    from doctrail.db_operations import (
        ensure_enrichments_table,
        ensure_run_tracking_tables,
        materialize_run_inputs,
        start_enrichment_run,
        write_enrichment,
    )

    db_path = temp_env["db_path"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            sha1 TEXT PRIMARY KEY,
            filename TEXT,
            raw_content TEXT
        )
    """)
    existing = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    if existing == 0:
        conn.executemany(
            "INSERT INTO documents (sha1, filename, raw_content) VALUES (?, ?, ?)",
            [
                ("payment_doc_1", "doc1.txt", "Article text about multiple compensation payments." * 8),
                ("payment_doc_2", "doc2.txt", "Article text about a single compensation payment." * 8),
            ],
        )
        conn.commit()
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT sha1, filename, raw_content FROM documents ORDER BY sha1"
        ).fetchall()
    ]
    conn.close()

    ensure_enrichments_table(str(db_path))
    ensure_run_tracking_tables(str(db_path))

    run_id = "payments_run_12345678"
    prompt_id = "payments_prompt_v1"
    query_hash = "payments_query_v1"
    started_at = datetime.now().isoformat()

    start_enrichment_run(
        db_path=str(db_path),
        run_id=run_id,
        command_started_at=started_at,
        enrichment_name="organ_payments",
        model="gpt-4o-mini",
        prompt_id=prompt_id,
        query_sql="SELECT rowid, sha1 FROM documents",
        query_hash=query_hash,
        key_column="sha1",
        source_name="documents",
    )
    materialize_run_inputs(str(db_path), run_id, rows, "sha1", enabled=True)

    write_enrichment(
        db_path=str(db_path),
        key_value=rows[0]["sha1"],
        enrichment_name="organ_payments",
        field_name="payments",
        value=[
            {
                "amount": "50000",
                "payer": "Red Cross",
                "receiver": "Donor family",
                "fund_name": "Relief Fund",
                "evidence": "Distributed 50,000 yuan condolence payment.",
            },
            {
                "amount": "20000",
                "payer": "Hospital",
                "receiver": "Family member",
                "fund_name": "Emergency Fund",
                "evidence": "Hospital separately provided 20,000 yuan support.",
            },
        ],
        model="gpt-4o-mini",
        prompt_hash=prompt_id,
        run_id=run_id,
        query_hash=query_hash,
        overwrite=True,
    )
    write_enrichment(
        db_path=str(db_path),
        key_value=rows[1]["sha1"],
        enrichment_name="organ_payments",
        field_name="payments",
        value=[
            {
                "amount": "10000",
                "payer": "Charity",
                "receiver": "Recipient family",
                "fund_name": "Warmth Fund",
                "evidence": "Charity announced a 10,000 yuan subsidy.",
            }
        ],
        model="gpt-4o-mini",
        prompt_hash=prompt_id,
        run_id=run_id,
        query_hash=query_hash,
        overwrite=True,
    )

    for row in rows:
        write_enrichment(
            db_path=str(db_path),
            key_value=row["sha1"],
            enrichment_name="translate_to_english",
            field_name="english_translation",
            value=f"English translation for {row['filename']}",
            model="gpt-4o-mini",
            prompt_hash="translate_prompt_v1",
            run_id="translation_run_v1",
            query_hash="translation_query_v1",
            overwrite=True,
        )

    runner = CliRunner()
    html_path = temp_env["temp_dir"] / "payments_review.html"

    with runner.isolated_filesystem(temp_dir=str(temp_env["temp_dir"])):
        project_dir = Path(".doctrail")
        views_dir = project_dir / "views"
        views_dir.mkdir(parents=True, exist_ok=True)
        with open(project_dir / "config.yml", "w") as handle:
            yaml.dump({"database": str(db_path), "default_table": "documents", "key_column": "sha1"}, handle)

        spec_path = views_dir / "payments_review.yml"
        spec_path.write_text(f"""name: payments_review
enrichment: organ_payments
run_id: {run_id}
include:
  - filename
  - raw_content:120
columns:
  - field: english_translation
    enrichment: translate_to_english
    alias: english_translation
explode:
  field: payments
  object_fields:
    - amount
    - payer
    - receiver
    - fund_name
    - evidence
  alias_prefix: payment_
""")

        spec_result = runner.invoke(cli, ["view", "spec", "payments_review"])
        assert spec_result.exit_code == 0, spec_result.output
        assert "Created view: v_payments_review" in spec_result.output

        render_result = runner.invoke(
            cli,
            ["view", "render", "v_payments_review", "--output", str(html_path), "--limit", "10"],
        )
        assert render_result.exit_code == 0, render_result.output
        assert html_path.exists()

        finalize_result = runner.invoke(
            cli,
            ["finalize", "--view", "v_payments_review", "--table", "final_payments"],
        )
        assert finalize_result.exit_code == 0, finalize_result.output
        assert "Created editable table: final_payments" in finalize_result.output

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    count = conn.execute("SELECT COUNT(*) FROM v_payments_review").fetchone()[0]
    assert count == 3

    columns = [row[1] for row in conn.execute("PRAGMA table_info(v_payments_review)").fetchall()]
    assert "english_translation" in columns
    assert "payment_amount" in columns
    assert "payment_evidence" in columns
    assert "_item_index" in columns

    result_rows = conn.execute("""
        SELECT sha1, payment_amount, payment_payer, payment_evidence, english_translation
        FROM v_payments_review
        ORDER BY sha1, _item_index
    """).fetchall()
    conn.close()

    assert result_rows[0]["sha1"] == result_rows[1]["sha1"]
    assert result_rows[0]["payment_amount"] == "50000"
    assert result_rows[1]["payment_amount"] == "20000"
    assert result_rows[2]["payment_amount"] == "10000"
    assert "English translation" in result_rows[0]["english_translation"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    final_rows = conn.execute("""
        SELECT sha1, _item_index, payment_amount
        FROM final_payments
        ORDER BY sha1, _item_index
    """).fetchall()
    assert len(final_rows) == 3
    conn.execute("""
        UPDATE final_payments
        SET payment_amount = '55555'
        WHERE sha1 = ? AND _item_index = 0
    """, (rows[0]["sha1"],))
    conn.commit()
    edited_amount = conn.execute("""
        SELECT payment_amount FROM final_payments
        WHERE sha1 = ? AND _item_index = 0
    """, (rows[0]["sha1"],)).fetchone()[0]
    assert edited_amount == "55555"
    registry = conn.execute("""
        SELECT source_view, source_run_id
        FROM _doctrail_final_tables
        WHERE table_name = 'final_payments'
    """).fetchone()
    conn.close()

    assert registry is not None
    assert registry["source_view"] == "v_payments_review"
    assert registry["source_run_id"] is None

    html_output = html_path.read_text(encoding="utf-8")
    assert "<table>" in html_output
    assert "v_payments_review" in html_output
    assert "Distributed 50,000 yuan condolence payment." in html_output
