"""Integration tests for CLI provider — calls real CLIs and writes to database.

Full pipeline: CLI subprocess → JSON parse → Pydantic model → enrichments table → query back.

Run with: pytest tests/test_cli_provider_integration.py -v
Skip slow tests: pytest tests/test_cli_provider_integration.py -v -k "not Gemini and not Codex"
"""

import asyncio
import json
import os
import sqlite3
import uuid

import pytest
from pydantic import BaseModel

from doctrail.llm_providers.cli_provider import CLIProvider
from doctrail.llm_providers.factory import get_llm_provider
from doctrail.llm_providers.claude_sdk_provider import ClaudeSDKProvider
from doctrail.db_operations import (
    get_db_connection, ensure_enrichments_table, write_enrichment,
    ensure_enrichment_audit_table, store_raw_enrichment_response,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("DOCTRAIL_RUN_LIVE_CLI_TESTS") != "1",
    reason="live CLI integration tests are opt-in; set DOCTRAIL_RUN_LIVE_CLI_TESTS=1 to run",
)


# --- Pydantic models ---

class SentimentResult(BaseModel):
    sentiment: str
    confidence: float


class ClassificationResult(BaseModel):
    category: str
    confidence: float


class SimpleLabel(BaseModel):
    label: str


# --- Database fixture ---

@pytest.fixture
def test_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE documents (
            sha1 TEXT PRIMARY KEY, filename TEXT, raw_content TEXT
        )
    """)
    conn.execute("""
        INSERT INTO documents VALUES
        ('abc123', 'climate.txt', 'Climate change threatens global food security.'),
        ('def456', 'econ.txt', 'The central bank raised interest rates by 25 basis points.')
    """)
    conn.commit()
    conn.close()
    ensure_enrichments_table(db_path)
    ensure_enrichment_audit_table(db_path)
    return db_path


def _cli_available(tool: str) -> bool:
    import subprocess
    env = dict(os.environ)
    if tool == "claude":
        env.pop("CLAUDECODE", None)
    try:
        cmd = [tool, "--version"] if tool != "codex" else ["codex", "--version"]
        return subprocess.run(cmd, capture_output=True, timeout=10, env=env).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# --- Claude CLI ---

@pytest.mark.skipif(not _cli_available("claude"), reason="claude CLI not installed")
class TestClaudeCLIIntegration:

    @pytest.mark.asyncio
    async def test_structured_output(self):
        """CLI → JSON → Pydantic model."""
        provider = CLIProvider(cli_tool="claude", model="haiku")
        result = await provider.generate_structured(
            messages=[
                {"role": "system", "content": "Classify sentiment as positive/negative/neutral."},
                {"role": "user", "content": "I love sunny days!"},
            ],
            pydantic_model=SentimentResult,
        )
        assert isinstance(result, SentimentResult)
        assert result.sentiment
        assert 0.0 <= result.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_return_usage(self):
        provider = CLIProvider(cli_tool="claude", model="haiku")
        result, usage = await provider.generate_structured(
            messages=[{"role": "user", "content": "Label: 'hello'"}],
            pydantic_model=SimpleLabel,
            return_usage=True,
        )
        assert isinstance(result, SimpleLabel)
        assert usage.estimate_cost() == 0.0
        assert usage.input_tokens > 0

    @pytest.mark.asyncio
    async def test_text_generation(self):
        provider = CLIProvider(cli_tool="claude", model="haiku")
        text = await provider.generate_text(
            messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}],
        )
        assert isinstance(text, str)
        assert len(text) > 0

    @pytest.mark.asyncio
    async def test_full_pipeline_to_database(self, test_db):
        """CLI call → parse → write enrichments + audit → query back."""
        provider = CLIProvider(cli_tool="claude", model="haiku")

        # 1. Get structured output
        result = await provider.generate_structured(
            messages=[
                {"role": "system", "content": "Classify the topic."},
                {"role": "user", "content": "Climate change threatens global food security."},
            ],
            pydantic_model=ClassificationResult,
        )
        assert isinstance(result, ClassificationResult)

        # 2. Write to enrichments table
        eid = str(uuid.uuid4())
        model_name = "cli/claude/haiku"
        sha1 = "abc123"

        write_enrichment(test_db, sha1, "topic", "category", result.category, model_name, enrichment_id=eid)
        write_enrichment(test_db, sha1, "topic", "confidence", result.confidence, model_name, enrichment_id=eid)

        # 3. Write audit
        store_raw_enrichment_response(test_db, sha1, "topic", result.model_dump_json(), model_name, enrichment_id=eid)

        # 4. Query back enrichments
        with get_db_connection(test_db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT field_name, value, model FROM _enrichments WHERE key_value=? AND enrichment_name=? ORDER BY field_name",
                (sha1, "topic"),
            ).fetchall()

            assert len(rows) == 2
            fields = {r["field_name"]: r["value"] for r in rows}
            assert "category" in fields
            assert "confidence" in fields
            assert fields["category"]  # non-empty
            assert float(fields["confidence"]) > 0
            for row in rows:
                assert row["model"] == "cli/claude/haiku"

            # 5. Query back audit
            audit = conn.execute(
                "SELECT raw_json, model_used FROM _enrichment_audit WHERE key_value=? AND enrichment_name=?",
                (sha1, "topic"),
            ).fetchone()
            assert audit is not None
            assert audit["model_used"] == "cli/claude/haiku"
            audit_data = json.loads(audit["raw_json"])
            assert "category" in audit_data
            assert "confidence" in audit_data


# --- Factory integration ---

@pytest.mark.skipif(not _cli_available("claude"), reason="claude CLI not installed")
class TestFactoryIntegration:

    @pytest.mark.asyncio
    async def test_factory_creates_working_provider(self):
        provider = get_llm_provider("cli/claude/haiku")
        assert isinstance(provider, ClaudeSDKProvider)
        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "Label: 'hello world'"}],
            pydantic_model=SimpleLabel,
        )
        assert isinstance(result, SimpleLabel)
        assert result.label

    @pytest.mark.asyncio
    async def test_call_llm_structured_pathway(self):
        """Test through call_llm_structured (what the enrichment pipeline uses)."""
        from doctrail.llm_operations import call_llm_structured
        result = await call_llm_structured(
            model="cli/claude/haiku",
            messages=[
                {"role": "system", "content": "Label the text."},
                {"role": "user", "content": "Stock market crashed."},
            ],
            pydantic_model=SimpleLabel,
        )
        assert isinstance(result, SimpleLabel)
        assert result.label


# --- Gemini CLI ---

@pytest.mark.skipif(not _cli_available("gemini"), reason="gemini CLI not installed")
class TestGeminiCLIIntegration:

    @pytest.mark.asyncio
    async def test_structured_output(self):
        provider = CLIProvider(cli_tool="gemini", model="gemini-2.5-flash")
        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "Label: 'rain in Spain'"}],
            pydantic_model=SimpleLabel,
        )
        assert isinstance(result, SimpleLabel)
        assert result.label

    @pytest.mark.asyncio
    async def test_write_to_database(self, test_db):
        provider = CLIProvider(cli_tool="gemini", model="gemini-2.5-flash")
        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "Label: 'economics text'"}],
            pydantic_model=SimpleLabel,
        )
        eid = str(uuid.uuid4())
        write_enrichment(test_db, "def456", "gemini_label", "label", result.label,
                         "cli/gemini/gemini-2.5-flash", enrichment_id=eid)

        with get_db_connection(test_db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT value, model FROM _enrichments WHERE key_value=? AND enrichment_name=?",
                ("def456", "gemini_label"),
            ).fetchone()
            assert row is not None
            assert row["value"] == result.label
            assert row["model"] == "cli/gemini/gemini-2.5-flash"


# --- Codex CLI ---

@pytest.mark.skipif(not _cli_available("codex"), reason="codex CLI not installed")
class TestCodexCLIIntegration:

    @pytest.mark.asyncio
    async def test_structured_output(self):
        provider = CLIProvider(cli_tool="codex", model="gpt-5.3-codex")
        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "Label: 'breaking tech news'"}],
            pydantic_model=SimpleLabel,
        )
        assert isinstance(result, SimpleLabel)
        assert result.label

    @pytest.mark.asyncio
    async def test_write_to_database(self, test_db):
        provider = CLIProvider(cli_tool="codex", model="gpt-5.3-codex")
        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "Label: 'interest rate policy'"}],
            pydantic_model=SimpleLabel,
        )
        eid = str(uuid.uuid4())
        write_enrichment(test_db, "def456", "codex_label", "label", result.label,
                         "cli/codex/gpt-5.3-codex", enrichment_id=eid)

        with get_db_connection(test_db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT value, model FROM _enrichments WHERE key_value=? AND enrichment_name=?",
                ("def456", "codex_label"),
            ).fetchone()
            assert row is not None
            assert row["value"] == result.label
            assert row["model"] == "cli/codex/gpt-5.3-codex"
