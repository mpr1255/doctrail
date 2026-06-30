import json
import sqlite3
from pathlib import Path

import yaml
from click.testing import CliRunner

from doctrail.cli import cli


def _write_project(tmp_path, *, fixture_rows):
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                text TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO documents (id, text) VALUES (?, ?)",
            [
                ("doc-1", "First document with enough text."),
                ("doc-2", "Second document with enough text."),
            ],
        )

    replay_dir = tmp_path / ".doctrail" / "replay"
    replay_dir.mkdir(parents=True)
    fixture_path = replay_dir / "sentiment.jsonl"
    fixture_path.write_text(
        "\n".join(json.dumps(row) for row in fixture_rows) + "\n",
        encoding="utf-8",
    )

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "key_column": "id",
        "sql_queries": {
            "all_docs": "SELECT rowid, id FROM documents ORDER BY id",
        },
        "enrichments": [
            {
                "name": "sentiment",
                "model": "replay",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["text"],
                },
                "prompt": "Classify the document sentiment.",
                "schema": {
                    "sentiment": {"enum": ["positive", "negative"]},
                },
            }
        ],
    }
    config_path = tmp_path / "config.yml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return config_path


def _write_integer_key_project(tmp_path, *, fixture_rows):
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY,
                text TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO documents (id, text) VALUES (?, ?)",
            [
                (0, "Zero-key document with enough text."),
                (1, "One-key document with enough text."),
            ],
        )

    replay_dir = tmp_path / ".doctrail" / "replay"
    replay_dir.mkdir(parents=True)
    fixture_path = replay_dir / "sentiment.jsonl"
    fixture_path.write_text(
        "\n".join(json.dumps(row) for row in fixture_rows) + "\n",
        encoding="utf-8",
    )

    config = {
        "database": str(db_path),
        "default_table": "documents",
        "key_column": "id",
        "sql_queries": {
            "all_docs": "SELECT rowid, id FROM documents ORDER BY id",
        },
        "enrichments": [
            {
                "name": "sentiment",
                "model": "replay",
                "input": {
                    "query": "all_docs",
                    "input_columns": ["text"],
                },
                "prompt": "Classify the document sentiment.",
                "schema": {
                    "sentiment": {"enum": ["positive", "negative"]},
                },
            }
        ],
    }
    config_path = tmp_path / "config.yml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return config_path


def test_plain_replay_label_mismatch_fails_fast(mocker, monkeypatch, tmp_path):
    mocker.stopall()
    config_path = _write_project(
        tmp_path,
        fixture_rows=[
            {
                "key_value": "doc-1",
                "label": "coder-a",
                "response": {"sentiment": "positive"},
            },
            {
                "key_value": "doc-1",
                "label": "coder-b",
                "response": {"sentiment": "negative"},
            },
        ],
    )
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--skip-requirements", "run", "sentiment", "--config", str(config_path)],
        catch_exceptions=False,
    )

    assert result.exit_code != 0
    assert "Available labels: replay/coder-a, replay/coder-b" in result.output
    assert "--model replay/coder-a" in result.output
    assert "Successfully completed" not in result.output
    assert "Processing 2 rows" not in result.output


def test_all_replay_row_errors_exit_nonzero_with_truthful_summary(mocker, monkeypatch, tmp_path):
    mocker.stopall()
    config_path = _write_project(
        tmp_path,
        fixture_rows=[
            {
                "key_value": "not-a-candidate",
                "label": "default",
                "response": {"sentiment": "positive"},
            },
        ],
    )
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--skip-requirements", "run", "sentiment", "--config", str(config_path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Failed: 0 succeeded, 2 errored across 1 enrichment(s)" in result.output
    assert "Status: completed with errors (0 succeeded, 2 errored)" in result.output
    assert "Successfully completed" not in result.output

    with sqlite3.connect(tmp_path / "test.db") as conn:
        run_row = conn.execute(
            """
            SELECT status, success_count, error_count
            FROM _enrichment_runs
            ORDER BY rowid DESC
            LIMIT 1
            """
        ).fetchone()

    assert run_row == ("completed_with_errors", 0, 2)


def test_partial_replay_row_errors_exit_nonzero_with_truthful_summary(mocker, monkeypatch, tmp_path):
    mocker.stopall()
    config_path = _write_project(
        tmp_path,
        fixture_rows=[
            {
                "key_value": "doc-1",
                "label": "default",
                "response": {"sentiment": "positive"},
            },
        ],
    )
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--skip-requirements", "run", "sentiment", "--config", str(config_path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Completed with errors: 1 succeeded, 1 errored across 1 enrichment(s)" in result.output
    assert "Status: completed with errors (1 succeeded, 1 errored)" in result.output
    assert "Successfully completed" not in result.output

    with sqlite3.connect(tmp_path / "test.db") as conn:
        run_row = conn.execute(
            """
            SELECT status, success_count, error_count
            FROM _enrichment_runs
            ORDER BY rowid DESC
            LIMIT 1
            """
        ).fetchone()

    assert run_row == ("completed_with_errors", 1, 1)


def test_falsy_key_row_errors_are_counted(mocker, monkeypatch, tmp_path):
    mocker.stopall()
    config_path = _write_integer_key_project(
        tmp_path,
        fixture_rows=[
            {
                "key_value": 1,
                "label": "default",
                "response": {"sentiment": "positive"},
            },
        ],
    )
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--skip-requirements", "run", "sentiment", "--config", str(config_path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    assert "Completed with errors: 1 succeeded, 1 errored across 1 enrichment(s)" in result.output
    assert "Successfully completed" not in result.output

    with sqlite3.connect(tmp_path / "test.db") as conn:
        run_row = conn.execute(
            """
            SELECT status, success_count, error_count
            FROM _enrichment_runs
            ORDER BY rowid DESC
            LIMIT 1
            """
        ).fetchone()
        item_statuses = dict(
            conn.execute(
                """
                SELECT key_value, status
                FROM _enrichment_run_items
                ORDER BY key_value
                """
            ).fetchall()
        )

    assert run_row == ("completed_with_errors", 1, 1)
    assert item_statuses["0"] == "error"
    assert item_statuses["1"] == "processed"
