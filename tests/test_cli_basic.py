#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest",
#     "click",
# ]
# ///

"""
Basic CLI smoke tests.

Tests that CLI commands respond correctly without mocking anything.
These are fast sanity checks, not full integration tests.
"""

import json
import py_compile
import sqlite3
import subprocess
from io import StringIO
from pathlib import Path
import csv

import pytest
import yaml
from click.testing import CliRunner
from doctrail.cli import cli
from doctrail.cli import models as models_module
from doctrail.ingest.document_processor import format_supported_extensions_for_help


def test_cli_help():
    """Test that --help works."""
    runner = CliRunner()
    result = runner.invoke(cli, ['--help'])
    assert result.exit_code == 0
    assert 'Usage:' in result.output
    assert 'enrich' in result.output
    assert 'ingest' in result.output


def test_enrich_help():
    """Test that enrich --help works."""
    runner = CliRunner()
    result = runner.invoke(cli, ['enrich', '--help'])
    assert result.exit_code == 0
    assert 'config' in result.output.lower()
    assert 'enrichments' in result.output.lower()
    assert 'dedupe-scope' in result.output
    assert 'materialize-inputs' in result.output


def test_run_alias_help_matches_enrich_options():
    """The friendly run spelling should expose the enrich option surface."""
    runner = CliRunner()
    result = runner.invoke(cli, ['run', '--help'])
    assert result.exit_code == 0
    assert 'dry-run' in result.output
    assert 'dedupe-scope' in result.output


def test_docs_command_prints_manual():
    """doctrail docs should print the single packaged manual."""
    runner = CliRunner()
    result = runner.invoke(cli, ['docs'])
    assert result.exit_code == 0
    assert '# Doctrail' in result.output


def test_skill_command_prints_repo_skill():
    """doctrail skill should print the packaged operating doctrine."""
    runner = CliRunner()
    result = runner.invoke(cli, ['skill'])
    assert result.exit_code == 0
    assert '# Doctrail' in result.output
    assert 'doctrail docs' in result.output


def test_skill_install_refuses_different_existing_file(monkeypatch, tmp_path):
    """doctrail skill --install should not overwrite a different skill unless forced."""
    runner = CliRunner()
    monkeypatch.setenv("HOME", str(tmp_path))
    destination = tmp_path / ".claude" / "skills" / "doctrail" / "SKILL.md"

    install_result = runner.invoke(cli, ['skill', '--install'])
    assert install_result.exit_code == 0, install_result.output
    assert destination.exists()
    installed = destination.read_text(encoding="utf-8")
    assert '# Doctrail' in installed

    repeat_result = runner.invoke(cli, ['skill', '--install'])
    assert repeat_result.exit_code == 0, repeat_result.output
    assert 'already installed' in repeat_result.output

    destination.write_text("local edits\n", encoding="utf-8")
    refused_result = runner.invoke(cli, ['skill', '--install'])
    assert refused_result.exit_code != 0
    assert 'Use --force to overwrite' in refused_result.output

    force_result = runner.invoke(cli, ['skill', '--install', '--force'])
    assert force_result.exit_code == 0, force_result.output
    assert destination.read_text(encoding="utf-8") == installed


def test_questionary_imports_for_interactive_prompts():
    """The interactive wizard dependency should import successfully."""
    import questionary

    assert hasattr(questionary, "path")


def test_new_creates_key_only_enrichment_yaml(tmp_path):
    """doctrail new should create YAML using key-only scope plus input_columns."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        init_result = runner.invoke(cli, ["init", "-y", "--api-key", "sk-test"])
        assert init_result.exit_code == 0, init_result.output

        result = runner.invoke(cli, [
            "new",
            "sentiment",
            "--prompt", "Classify the sentiment.",
            "--enum", "positive,negative,neutral",
        ])

        assert result.exit_code == 0, result.output
        generated = Path(".doctrail/enrichments/sentiment.yml").read_text()
        config = yaml.safe_load(generated)
        assert config["input"]["query"] == "all_docs"
        assert config["input"]["input_columns"] == ["raw_content:3000"]
        assert config["schema"] == {"enum": ["positive", "negative", "neutral"]}
        assert config["output_column"] == "sentiment"
        assert "Write the prompt as a codebook" in generated
        assert "input_columns" in generated
        assert "doctrail docs" in generated


def test_new_prompt_output_flag_mode_does_not_prompt(tmp_path):
    """doctrail new with prompt/output flags should not enter the wizard."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        init_result = runner.invoke(cli, ["init", "-y", "--api-key", "sk-test"])
        assert init_result.exit_code == 0, init_result.output

        result = runner.invoke(cli, ["new", "x", "-p", "Classify tone.", "-o", "tone"])

        assert result.exit_code == 0, result.output
        generated = Path(".doctrail/enrichments/x.yml").read_text()
        config = yaml.safe_load(generated)
        assert config["schema"] == "string"
        assert config["output_column"] == "tone"
        assert "Classify tone." in generated
        assert "breaks the shared prompt prefix" in generated


def test_new_prompt_only_defaults_output_to_safe_name(tmp_path):
    """doctrail new with only -p should default output_column to the safe name."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        init_result = runner.invoke(cli, ["init", "-y", "--api-key", "sk-test"])
        assert init_result.exit_code == 0, init_result.output

        result = runner.invoke(cli, ["new", "tone check", "-p", "Classify tone."])

        assert result.exit_code == 0, result.output
        generated = Path(".doctrail/enrichments/tone_check.yml").read_text()
        config = yaml.safe_load(generated)
        assert config["schema"] == "string"
        assert config["output_column"] == "tone_check"


def test_edit_opens_project_enrichment(tmp_path):
    """doctrail edit should resolve local enrichment YAMLs."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        init_result = runner.invoke(cli, ["init", "-y", "--api-key", "sk-test"])
        assert init_result.exit_code == 0, init_result.output

        result = runner.invoke(cli, ["edit", "language"], env={"VISUAL": "true", "EDITOR": "true"})

        assert result.exit_code == 0, result.output
        assert "Edited:" in result.output


def test_enrich_without_name_lists_available_noninteractive(tmp_path):
    """Non-interactive no-arg enrich should keep listing available enrichments."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        init_result = runner.invoke(cli, ["init", "-y", "--api-key", "sk-test"])
        assert init_result.exit_code == 0, init_result.output

        result = runner.invoke(cli, ["enrich"])

        assert result.exit_code == 0, result.output
        assert "Available enrichments:" in result.output
        assert "language" in result.output
        assert "Run: doctrail enrich <name>" in result.output


def test_enrich_missing_config():
    """Test that enrich fails gracefully with missing config."""
    runner = CliRunner()
    result = runner.invoke(cli, [
        'enrich',
        '--config', '/nonexistent/config.yml',
        '--enrichments', 'test'
    ])
    assert result.exit_code != 0
    assert 'config' in result.output.lower() or 'not found' in result.output.lower() or 'error' in result.output.lower()


def test_enrich_missing_database_exits_nonzero(tmp_path):
    """A missing database should not be reported as a successful CLI run."""
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
database: missing.db
default_table: documents
default_model: gpt-4o-mini
sql_queries:
  all_docs: SELECT rowid, sha1 FROM documents
enrichments:
  - name: test
    input:
      query: all_docs
      input_columns: [raw_content]
    prompt: Summarize {raw_content}
    output_column: summary
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(cli, [
        "enrich",
        "--config", str(config_path),
        "--enrichments", "test",
        "--skip-cost-check",
    ])

    assert result.exit_code != 0
    assert "database" in result.output.lower() or "not found" in result.output.lower()


def test_enrich_config_imports_survive_cli_merge(tmp_path):
    """CLI overrides should not erase YAML !import enrichment definitions."""
    db_path = tmp_path / "docs.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE documents (
                sha1 TEXT PRIMARY KEY,
                raw_content TEXT
            )
        """)
        conn.execute(
            "INSERT INTO documents (sha1, raw_content) VALUES (?, ?)",
            ("doc1", "short imported config smoke test"),
        )

    (tmp_path / "enrichment.yml").write_text(
        """
name: classify
input:
  query: all_docs
  input_columns: [raw_content]
prompt: "Classify this text from {model}: {raw_content}"
model: gpt-4o-mini
output_column: label
""",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        f"""
database: {db_path}
default_table: documents
default_model: gpt-4o-mini
sql_queries:
  all_docs: SELECT rowid, sha1 FROM documents
enrichments:
  - !import enrichment.yml
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(cli, [
        "enrich",
        "--config", str(config_path),
        "--enrichments", "classify",
        "--dry-run",
        "--skip-cost-check",
    ])

    assert result.exit_code == 0, result.output
    assert "classify" in result.output


def test_tracked_python_compile_scope_excludes_unison_backups():
    """The project compile scope should follow tracked files, not backup trees."""
    listed = subprocess.check_output(
        ["git", "ls-files", "*.py"],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
    ).splitlines()

    assert listed
    assert not any("/.unison/" in path or path.startswith(".unison/") for path in listed)
    for relative_path in listed:
        py_compile.compile(
            str(Path(__file__).resolve().parents[1] / relative_path),
            doraise=True,
        )


def test_ingest_help():
    """Test that ingest --help works."""
    runner = CliRunner()
    result = runner.invoke(cli, ['ingest', '--help'])
    assert result.exit_code == 0
    assert 'db-path' in result.output.lower() or 'database' in result.output.lower()
    normalized_output = " ".join(result.output.split())
    assert format_supported_extensions_for_help() in normalized_output


def test_ingest_defaults_to_cwd_doctrail_db(tmp_path):
    """Ingest should default to ./doctrail.db when no db-path is provided."""
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        docs_dir = Path("docs")
        docs_dir.mkdir()
        (docs_dir / "sample.txt").write_text("hello doctrail\n", encoding="utf-8")

        result = runner.invoke(cli, [
            "--skip-requirements",
            "ingest",
            "--input-dir", str(docs_dir),
            "--yes",
        ])

        assert result.exit_code == 0, result.output
        db_path = Path("doctrail.db")
        assert db_path.exists()
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert count == 1


def test_ingest_workers_handle_non_picklable_metadata(monkeypatch, tmp_path):
    """Threaded ingest should normalize odd metadata objects before persistence."""
    runner = CliRunner()

    class _NonPicklableTitle:
        def __init__(self, text):
            self.text = text

        def __str__(self):
            return self.text

        def __reduce__(self):
            raise TypeError("do not pickle me")

    async def _fake_process_document(file_path, file_sha1, **kwargs):
        return file_sha1, f"content from {Path(file_path).name}", {
            "title": _NonPicklableTitle(f"title for {Path(file_path).name}"),
            "author": _NonPicklableTitle("archive bot"),
            "original_file_path": file_path,
            "extraction_method": "fake_extractor",
        }

    monkeypatch.setattr("doctrail.ingest.core.process_document", _fake_process_document)

    with runner.isolated_filesystem(temp_dir=tmp_path):
        docs_dir = Path("docs")
        docs_dir.mkdir()
        (docs_dir / "first.txt").write_text("first\n", encoding="utf-8")
        (docs_dir / "second.txt").write_text("second\n", encoding="utf-8")

        result = runner.invoke(cli, [
            "--skip-requirements",
            "ingest",
            "--input-dir", str(docs_dir),
            "--workers", "2",
            "--yes",
        ])

        assert result.exit_code == 0, result.output

        with sqlite3.connect("doctrail.db") as conn:
            rows = conn.execute(
                """
                SELECT filename, title, author, metadata
                FROM documents
                ORDER BY filename
                """
            ).fetchall()

        normalized_rows = [
            (filename, title, author, json.loads(metadata))
            for filename, title, author, metadata in rows
        ]
        assert normalized_rows[0][:3] == ("first.txt", "title for first.txt", "archive bot")
        assert normalized_rows[0][3]["title"] == "title for first.txt"
        assert normalized_rows[0][3]["author"] == "archive bot"
        assert normalized_rows[1][:3] == ("second.txt", "title for second.txt", "archive bot")
        assert normalized_rows[1][3]["title"] == "title for second.txt"
        assert normalized_rows[1][3]["author"] == "archive bot"


def test_ingest_directory_db_path_uses_doctrail_db(tmp_path):
    """Directory-style db paths should resolve to <dir>/doctrail.db."""
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        docs_dir = Path("docs")
        docs_dir.mkdir()
        (docs_dir / "sample.txt").write_text("hello doctrail\n", encoding="utf-8")

        result = runner.invoke(cli, [
            "--skip-requirements",
            "ingest",
            "--input-dir", str(docs_dir),
            "--db-path", ".",
            "--yes",
        ])

        assert result.exit_code == 0, result.output
        db_path = Path("doctrail.db")
        assert db_path.exists()
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert count == 1


def test_ingest_manifest_stores_metadata_json_and_promoted_columns(tmp_path):
    """Manifest fields should stay in metadata JSON; common fields are promoted."""
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        docs_dir = Path("docs")
        docs_dir.mkdir()
        (docs_dir / "sample.txt").write_text("manifest-backed document\n", encoding="utf-8")
        (docs_dir / "manifest.json").write_text(
            json.dumps({
                "sample.txt": {
                    "url": "https://example.com/sample",
                    "scraped_at": "2026-03-20 13:11:49",
                    "category": "research",
                }
            }),
            encoding="utf-8",
        )

        result = runner.invoke(cli, [
            "--skip-requirements",
            "ingest",
            "--input-dir", str(docs_dir),
            "--yes",
        ])

        assert result.exit_code == 0, result.output

        with sqlite3.connect("doctrail.db") as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()]
            row = conn.execute(
                """
                SELECT url, metadata
                FROM documents
                WHERE filename = 'sample.txt'
                """
            ).fetchone()

        assert row is not None
        url, metadata_json = row
        assert url == "https://example.com/sample"
        assert "metadata_url" not in columns
        assert "metadata_scraped_at" not in columns
        assert "metadata_category" not in columns

        payload = json.loads(metadata_json)
        assert payload["url"] == "https://example.com/sample"
        assert payload["scraped_at"] == "2026-03-20 13:11:49"
        assert payload["category"] == "research"


def test_ingest_nonexistent_directory_db_path_creates_doctrail_db(tmp_path):
    """Non-file db paths should be treated as directories even when they do not exist yet."""
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        docs_dir = Path("docs")
        docs_dir.mkdir()
        (docs_dir / "sample.txt").write_text("hello doctrail\n", encoding="utf-8")

        result = runner.invoke(cli, [
            "--skip-requirements",
            "ingest",
            "--input-dir", str(docs_dir),
            "--db-path", "dbs/archive",
            "--yes",
        ])

        assert result.exit_code == 0, result.output
        db_path = Path("dbs/archive/doctrail.db")
        assert db_path.exists()
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert count == 1


def test_ingest_explicit_file_db_path_is_used(tmp_path):
    """Explicit db file paths should be used exactly as provided."""
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        docs_dir = Path("docs")
        docs_dir.mkdir()
        (docs_dir / "sample.txt").write_text("hello doctrail\n", encoding="utf-8")

        result = runner.invoke(cli, [
            "--skip-requirements",
            "ingest",
            "--input-dir", str(docs_dir),
            "--db-path", "dbs/custom.db",
            "--yes",
        ])

        assert result.exit_code == 0, result.output
        db_path = Path("dbs/custom.db")
        assert db_path.exists()
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert count == 1


def test_ingest_with_multiple_workers_processes_all_files(tmp_path):
    """Ingest should work end-to-end with a real process pool and single writer."""
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        docs_dir = Path("docs")
        docs_dir.mkdir()
        for i in range(4):
            (docs_dir / f"sample_{i}.txt").write_text(f"hello doctrail {i}\n", encoding="utf-8")

        result = runner.invoke(cli, [
            "--skip-requirements",
            "ingest",
            "--input-dir", str(docs_dir),
            "--workers", "2",
            "--yes",
        ])

        assert result.exit_code == 0, result.output
        with sqlite3.connect("doctrail.db") as conn:
            count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert count == 4


def test_ingest_with_workers_uses_env_mac_ocr_client(tmp_path):
    """Worker processes should be able to load a local OCR client from an env var."""
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        docs_dir = Path("docs")
        docs_dir.mkdir()
        (docs_dir / "sample.pdf").write_bytes(b"%PDF-1.4\nplaceholder pdf bytes\n")

        client_path = Path("local_ocr_client.py")
        client_path.write_text(
            "async def ocr_async(file_path, node=None):\n"
            "    return '==== Page 1 ====\\nLOCAL OCR TEXT'\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            cli,
            [
                "--skip-requirements",
                "ingest",
                "--input-dir", str(docs_dir),
                "--workers", "2",
                "--pdf-engine", "mac-ocr",
                "--ocr-engine", "mac-ocr",
                "--yes",
            ],
            env={"DOCTRAIL_MAC_OCR_CLIENT_PATH": str(client_path)},
        )

        assert result.exit_code == 0, result.output
        with sqlite3.connect("doctrail.db") as conn:
            row = conn.execute("SELECT raw_content FROM documents LIMIT 1").fetchone()
        assert row is not None
        assert "LOCAL OCR TEXT" in row[0]


def test_ingest_preserves_spreadsheet_csv_payload(tmp_path):
    """Spreadsheet ingest should persist sheet rows plus CSV-usable payloads."""
    from openpyxl import Workbook

    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        docs_dir = Path("docs")
        docs_dir.mkdir()

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Summary"
        sheet.append(["Name", "Count"])
        sheet.append(["Alice", 3])
        sheet.append(["Bob", 4])
        workbook.save(docs_dir / "sample.xlsx")

        result = runner.invoke(cli, [
            "--skip-requirements",
            "ingest",
            "--input-dir", str(docs_dir),
            "--yes",
        ])

        assert result.exit_code == 0, result.output

        with sqlite3.connect("doctrail.db") as conn:
            row = conn.execute(
                "SELECT raw_content, json_metadata FROM documents WHERE filename = ?",
                ("sample.xlsx",),
            ).fetchone()

        assert row is not None
        raw_content, json_metadata = row
        assert "Sheet: Summary" in raw_content

        payload = json.loads(json_metadata)
        assert payload["spreadsheet_sheet_count"] == 1
        sheet_payload = payload["spreadsheet_sheets"][0]
        assert sheet_payload["sheet_name"] == "Summary"
        assert sheet_payload["rows"] == [["Name", "Count"], ["Alice", "3"], ["Bob", "4"]]

        parsed_csv = list(csv.reader(StringIO(sheet_payload["csv"])))
        assert parsed_csv == [["Name", "Count"], ["Alice", "3"], ["Bob", "4"]]


def test_ingest_supports_csv_files_with_structured_payload(tmp_path):
    """CSV files should ingest as spreadsheets and keep a CSV-usable payload."""
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        docs_dir = Path("docs")
        docs_dir.mkdir()
        (docs_dir / "sample.csv").write_text(
            "Name,Count\nAlice,5\nBob,7\n",
            encoding="utf-8",
        )

        result = runner.invoke(cli, [
            "--skip-requirements",
            "ingest",
            "--input-dir", str(docs_dir),
            "--yes",
        ])

        assert result.exit_code == 0, result.output

        with sqlite3.connect("doctrail.db") as conn:
            row = conn.execute(
                "SELECT raw_content, json_metadata, metadata FROM documents WHERE filename = ?",
                ("sample.csv",),
            ).fetchone()

        assert row is not None
        raw_content, json_metadata, metadata = row
        assert "Sheet: sample" in raw_content

        payload = json.loads(json_metadata)
        assert payload["spreadsheet_sheet_count"] == 1
        sheet_payload = payload["spreadsheet_sheets"][0]
        assert sheet_payload["rows"] == [["Name", "Count"], ["Alice", "5"], ["Bob", "7"]]
        assert list(csv.reader(StringIO(sheet_payload["csv"]))) == [["Name", "Count"], ["Alice", "5"], ["Bob", "7"]]

        metadata_payload = json.loads(metadata)
        assert metadata_payload["spreadsheet_sheet_count"] == "1"


def test_ingest_schema_mismatch_exits_without_success_banner(tmp_path):
    """Schema errors should fail the command instead of printing a success summary."""
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        docs_dir = Path("docs")
        docs_dir.mkdir()
        (docs_dir / "sample.txt").write_text("hello doctrail\n", encoding="utf-8")

        db_path = Path("bad.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY,
                    sha1 TEXT,
                    filename TEXT,
                    filepath TEXT,
                    raw_content TEXT,
                    file_created TEXT,
                    file_modified TEXT
                )
                """
            )

        result = runner.invoke(cli, [
            "--skip-requirements",
            "ingest",
            "--input-dir", str(docs_dir),
            "--db-path", str(db_path),
            "--yes",
        ])

        assert result.exit_code != 0
        assert "Database schema mismatch" in result.output
        assert "Ingestion completed" not in result.output


def test_run_workflow_commands_help():
    """The run inspection and override commands should be registered."""
    runner = CliRunner()

    runs_result = runner.invoke(cli, ['runs', '--help'])
    assert runs_result.exit_code == 0
    assert 'recent enrichment runs' in runs_result.output.lower()

    diff_result = runner.invoke(cli, ['diff-runs', '--help'])
    assert diff_result.exit_code == 0
    assert 'run-a' in diff_result.output.lower()
    assert 'run-b' in diff_result.output.lower()

    export_result = runner.invoke(cli, ['overrides-export', '--help'])
    assert export_result.exit_code == 0
    assert 'run-id' in export_result.output.lower()

    import_result = runner.invoke(cli, ['overrides-import', '--help'])
    assert import_result.exit_code == 0
    assert 'reviewer' in import_result.output.lower()

    rebuild_result = runner.invoke(cli, ['rebuild-enrichments', '--help'])
    assert rebuild_result.exit_code == 0
    assert 'projection payloads' in rebuild_result.output.lower()

    finalize_result = runner.invoke(cli, ['finalize', '--help'])
    assert finalize_result.exit_code == 0
    assert 'editable final table' in finalize_result.output.lower()

    view_result = runner.invoke(cli, ['view', '--help'])
    assert view_result.exit_code == 0
    assert 'spec' in view_result.output.lower()
    assert 'render' in view_result.output.lower()


def test_models_lists_grouped_identifiers(monkeypatch):
    """The models command should show exact doctrail identifiers grouped by backend."""
    openrouter_models = {
        "openai/gpt-5-mini": {"name": "GPT-5 Mini", "input": 0.25, "output": 2.0, "context_length": 200000},
        "anthropic/claude-sonnet-4": {"name": "Claude Sonnet 4", "input": 3.0, "output": 15.0, "context_length": 200000},
        "google/gemini-2.5-flash": {"name": "Gemini 2.5 Flash", "input": 0.3, "output": 2.5, "context_length": 1000000},
    }

    monkeypatch.setattr(
        models_module.cost_utils,
        "get_provider_models",
        lambda provider: {
            "openai": {"gpt-5-mini"},
            "anthropic": {"claude-sonnet-4"},
            "gemini": {"gemini-2.5-flash"},
        }.get(provider, set()),
    )
    monkeypatch.setattr(models_module.pricing_utils, "get_all_models", lambda: openrouter_models)
    monkeypatch.setattr(
        models_module.pricing_utils,
        "get_cache_info",
        lambda: {"model_count": 3, "cached_at": 0, "age_hours": 0.0, "is_bootstrap": False, "cache_file": "test"},
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["models", "--limit", "10"])

    assert result.exit_code == 0
    assert "Direct providers" in result.output
    assert "CLI backends" in result.output
    assert "OpenRouter" in result.output
    assert "gpt-5-mini" in result.output
    assert "claude-sonnet-4" in result.output
    assert "gemini-3.5-flash" in result.output
    assert "gemini-2.5-flash" in result.output
    assert "cli/claude/sonnet" in result.output
    assert "cli/gemini/gemini-3.5-flash" in result.output
    assert "cli/codex/gpt-5.3-codex" in result.output
    assert "openrouter/openai/gpt-5-mini" in result.output
    assert "openrouter/anthropic/claude-sonnet-4" in result.output
    assert "openrouter/google/gemini-2.5-flash" in result.output


def test_models_json_groups_sections(monkeypatch):
    """The JSON output should expose grouped sections instead of a flat mixed catalog."""
    monkeypatch.setattr(
        models_module.cost_utils,
        "get_provider_models",
        lambda provider: {
            "openai": {"gpt-5-mini"},
            "anthropic": {"claude-sonnet-4"},
            "gemini": {"gemini-2.5-flash"},
        }.get(provider, set()),
    )
    monkeypatch.setattr(
        models_module.pricing_utils,
        "get_all_models",
        lambda: {
            "openai/gpt-5-mini": {"name": "GPT-5 Mini", "input": 0.25, "output": 2.0, "context_length": 200000},
            "anthropic/claude-sonnet-4": {"name": "Claude Sonnet 4", "input": 3.0, "output": 15.0, "context_length": 200000},
            "google/gemini-2.5-flash": {"name": "Gemini 2.5 Flash", "input": 0.3, "output": 2.5, "context_length": 1000000},
        },
    )
    monkeypatch.setattr(
        models_module.pricing_utils,
        "get_cache_info",
        lambda: {"model_count": 3, "cached_at": 0, "age_hours": 0.0, "is_bootstrap": False, "cache_file": "test"},
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["models", "--json", "--limit", "2"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "curated"
    section_ids = {section["id"] for section in payload["sections"]}
    assert "openai" in section_ids
    assert "anthropic" in section_ids
    assert "gemini" in section_ids
    assert "cli/claude" in section_ids
    assert "openrouter/openai" in section_ids


def test_models_all_uses_prefixed_openrouter_ids(monkeypatch):
    """The full catalog mode should still use exact doctrail OpenRouter identifiers."""
    monkeypatch.setattr(
        models_module.pricing_utils,
        "get_all_models",
        lambda: {
            "openai/gpt-5-mini": {"name": "GPT-5 Mini", "input": 0.25, "output": 2.0, "context_length": 200000},
            "anthropic/claude-sonnet-4": {"name": "Claude Sonnet 4", "input": 3.0, "output": 15.0, "context_length": 200000},
        },
    )
    monkeypatch.setattr(
        models_module.pricing_utils,
        "get_cache_info",
        lambda: {"model_count": 2, "cached_at": 0, "age_hours": 0.0, "is_bootstrap": False, "cache_file": "test"},
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["models", "--all", "--provider", "anthropic", "--limit", "5"])

    assert result.exit_code == 0
    assert "openrouter/anthropic/claude-sonnet-4" in result.output
    assert "openrouter/openai/gpt-5-mini" not in result.output


def test_models_openai_batch_lists_verified_catalog(monkeypatch):
    """The models command should expose the OpenAI batch catalog with batch pricing."""
    monkeypatch.setattr(
        models_module.pricing_utils,
        "get_openai_batch_models",
        lambda: {
            "gpt-5-mini": {
                "batch_input": 0.25,
                "batch_cached_input": 0.025,
                "batch_output": 2.0,
                "snapshots": ["gpt-5-mini-2025-08-07"],
            },
            "gpt-4.1": {
                "batch_input": 2.0,
                "batch_cached_input": 0.5,
                "batch_output": 8.0,
                "snapshots": ["gpt-4.1-2025-04-14"],
            },
        },
    )
    monkeypatch.setattr(
        models_module.pricing_utils,
        "get_openai_batch_cache_info",
        lambda: {
            "model_count": 2,
            "cached_at": 0,
            "age_hours": 0.0,
            "is_bootstrap": False,
            "cache_file": "test",
            "fetched_at": "2026-03-14",
        },
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["models", "--openai-batch", "--limit", "5"])

    assert result.exit_code == 0
    assert "gpt-5-mini" in result.output
    assert "gpt-4.1" in result.output
    assert "2026-03-14" in result.output
    assert "$   0.250" in result.output
    assert "$   8.000" in result.output


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
