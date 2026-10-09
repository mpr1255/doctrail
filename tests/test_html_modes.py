"""HTML mode settings on the CLI and on the Python extraction engine.

The suite runs with DOCTRAIL_DISABLE_NATIVE=1 (see conftest.py), so ingests
here use the Python engine. Native behaviour is in test_native_extractor.py.
"""

import sqlite3

import click
import pytest
from click.testing import CliRunner

from doctrail.cli import cli
from doctrail.cli.ingest import _html_config
from doctrail.ingest.core import process_ingest

PAGE = (
    "<html><head><title>Notice</title></head><body>"
    "<nav>Home | Open government | Services</nav>"
    "<article><h1>Organ donation notice</h1>"
    "<p>District health commissions should report progress on organ donation "
    "and transplantation work every quarter, starting this year.</p></article>"
    "<footer>Copyright city health commission</footer></body></html>"
)


def test_html_config_absent_flags_keep_engine_defaults():
    assert _html_config(None, None, False) is None


def test_html_config_profile_alone_means_full_mode(tmp_path):
    profile = tmp_path / "gov.yml"
    profile.write_text("drop_selectors: [nav]\ndrop_line_patterns: ['^Copyright']\n")

    assert _html_config(None, profile, False) == {
        "mode": "full",
        "drop_selectors": ["nav"],
        "drop_line_patterns": ["^Copyright"],
    }


def test_html_config_flag_overrides_profile_mode(tmp_path):
    profile = tmp_path / "gov.yml"
    profile.write_text("mode: full\nreject_low_value: true\n")

    assert _html_config("article", profile, False) == {
        "mode": "article",
        "reject_low_value": True,
    }


def test_html_config_rejects_readability_with_full_mode():
    with pytest.raises(click.UsageError, match="--readability"):
        _html_config("full", None, True)


def test_html_config_rejects_a_profile_that_is_not_a_mapping(tmp_path):
    profile = tmp_path / "list.yml"
    profile.write_text("- nav\n- footer\n")

    with pytest.raises(click.UsageError, match="YAML mapping"):
        _html_config(None, profile, False)


def test_cli_reports_readability_conflict(tmp_path):
    (tmp_path / "in").mkdir()
    result = CliRunner().invoke(
        cli,
        [
            "ingest",
            "--input-dir", str(tmp_path / "in"),
            "--db-path", str(tmp_path / "out.db"),
            "--readability",
            "--html-mode", "full",
            "--yes",
        ],
    )

    assert result.exit_code != 0
    assert "--readability conflicts with full HTML mode" in result.output


async def _ingest_python(tmp_path, html_config):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "notice.html").write_text(PAGE, encoding="utf-8")
    db_path = tmp_path / "python.db"
    result = await process_ingest(
        db_path=str(db_path),
        input_dir=str(source),
        table="documents",
        extractor="python",
        html_config=html_config,
        yes=True,
    )
    return result, db_path


async def test_python_engine_full_mode_keeps_the_whole_page(tmp_path):
    result, db_path = await _ingest_python(tmp_path, {"mode": "full"})

    assert result["successful"] == 1
    with sqlite3.connect(db_path) as conn:
        content = conn.execute("SELECT raw_content FROM documents").fetchone()[0]
    assert "Open government" in content
    assert "report progress on organ donation" in content
    assert "Copyright city health commission" in content


async def test_python_engine_rejects_selector_rules(tmp_path):
    with pytest.raises(RuntimeError, match="drop_selectors need the native extractor"):
        await _ingest_python(tmp_path, {"mode": "full", "drop_selectors": ["nav"]})


async def test_python_engine_rejects_unknown_mode(tmp_path):
    with pytest.raises(RuntimeError, match="unknown HTML mode"):
        await _ingest_python(tmp_path, {"mode": "everything"})


async def test_python_engine_records_the_mode_it_used(tmp_path):
    result, db_path = await _ingest_python(tmp_path, {"mode": "full"})

    with sqlite3.connect(db_path) as conn:
        metadata = conn.execute("SELECT metadata FROM documents").fetchone()[0]
    assert '"html_mode": "full"' in metadata


async def test_python_engine_rejects_low_value_rejection(tmp_path):
    with pytest.raises(RuntimeError, match="reject_low_value need the native extractor"):
        await _ingest_python(tmp_path, {"mode": "full", "reject_low_value": True})


async def test_python_engine_rejects_unknown_settings(tmp_path):
    with pytest.raises(RuntimeError, match="unknown HTML setting drop"):
        await _ingest_python(tmp_path, {"mode": "full", "drop": ["nav"]})
