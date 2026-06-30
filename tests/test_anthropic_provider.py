"""Tests for the Anthropic provider implementation.

Tests structured output via messages.parse() and text fallback.
"""

import json
import asyncio
import sqlite3
import pytest
import yaml
from unittest.mock import AsyncMock, MagicMock
from pydantic import BaseModel

from doctrail.core import run_enrichment
from doctrail.llm_operations import _render_row_prompt_content
from doctrail.llm_providers.anthropic_provider import AnthropicProvider, TokenUsage
from doctrail.utils.model_pricing import get_model_price


# --- Test models ---

class SimpleResult(BaseModel):
    hostility_level: int
    explanation: str


class ClassifyResult(BaseModel):
    category: str
    confidence: float
    tags: list[str]


class CapturingAnthropicProvider:
    def __init__(self):
        self.calls = []

    def supports_reasoning_effort(self):
        return False

    async def generate_structured(
        self,
        *,
        messages,
        pydantic_model,
        temperature=0.0,
        return_usage=False,
        **kwargs,
    ):
        self.calls.append({
            "messages": messages,
            "temperature": temperature,
            "kwargs": kwargs,
        })
        result = pydantic_model(hostility_level=0, explanation="calm")
        usage = TokenUsage(
            input_tokens=6500,
            cached_input_tokens=2048,
            cache_creation_input_tokens=1024,
            output_tokens=12,
            model="claude-haiku-4-5",
        )
        if return_usage:
            return result, usage
        return result


# --- Mock helpers ---

def _make_usage_mock(
    input_tokens=100,
    output_tokens=50,
    cache_creation_input_tokens=0,
    cache_read_input_tokens=0,
):
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = cache_creation_input_tokens
    usage.cache_read_input_tokens = cache_read_input_tokens
    return usage


def _make_parse_response(parsed_obj, usage=None):
    """Create a mock response for messages.parse()."""
    response = MagicMock()
    response.parsed_output = parsed_obj
    response.usage = usage or _make_usage_mock()
    return response


def _make_text_response(text, usage=None):
    """Create a mock response for messages.create()."""
    content_block = MagicMock()
    content_block.text = text
    response = MagicMock()
    response.content = [content_block]
    response.usage = usage or _make_usage_mock()
    return response


# --- Structured output tests ---

class TestAnthropicStructured:
    """Test structured output via messages.parse()."""

    @pytest.mark.asyncio
    async def test_structured_output_success(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")

        expected = SimpleResult(hostility_level=3, explanation="aggressive tone")
        mock_response = _make_parse_response(expected)

        provider.client.messages.parse = AsyncMock(return_value=mock_response)

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify this"}],
            pydantic_model=SimpleResult,
        )

        assert isinstance(result, SimpleResult)
        assert result.hostility_level == 3
        assert result.explanation == "aggressive tone"

    @pytest.mark.asyncio
    async def test_structured_output_with_usage(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")

        expected = SimpleResult(hostility_level=1, explanation="neutral")
        usage = _make_usage_mock(input_tokens=200, output_tokens=30)
        mock_response = _make_parse_response(expected, usage=usage)

        provider.client.messages.parse = AsyncMock(return_value=mock_response)

        result, token_usage = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify this"}],
            pydantic_model=SimpleResult,
            return_usage=True,
        )

        assert result.hostility_level == 1
        assert isinstance(token_usage, TokenUsage)
        assert token_usage.input_tokens == 200
        assert token_usage.output_tokens == 30

    @pytest.mark.asyncio
    async def test_structured_output_extracts_system_prompt(self):
        """Verify system messages are passed as 'system' parameter, not in messages."""
        provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")

        expected = SimpleResult(hostility_level=0, explanation="peaceful")
        mock_response = _make_parse_response(expected)
        provider.client.messages.parse = AsyncMock(return_value=mock_response)

        await provider.generate_structured(
            messages=[
                {"role": "system", "content": "You are a content analyst."},
                {"role": "user", "content": "classify this"},
            ],
            pydantic_model=SimpleResult,
        )

        call_kwargs = provider.client.messages.parse.call_args[1]
        assert call_kwargs["system"] == [
            {
                "type": "text",
                "text": "You are a content analyst.",
                "cache_control": {"type": "ephemeral"},
            }
        ]
        # Messages should NOT contain system role
        for msg in call_kwargs["messages"]:
            assert msg["role"] != "system"

    @pytest.mark.asyncio
    async def test_structured_output_adds_message_cache_control(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-haiku-4-5")

        expected = SimpleResult(hostility_level=0, explanation="peaceful")
        provider.client.messages.parse = AsyncMock(return_value=_make_parse_response(expected))

        await provider.generate_structured(
            messages=[{"role": "user", "content": "classify this"}],
            pydantic_model=SimpleResult,
        )

        call_kwargs = provider.client.messages.parse.call_args[1]
        content = call_kwargs["messages"][0]["content"]
        assert content == [
            {
                "type": "text",
                "text": "classify this",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def test_row_renderer_splits_static_prompt_for_anthropic_cache(self):
        rendered = _render_row_prompt_content(
            row={"sha1": "doc-1", "text": "row-specific content"},
            parsed_input_cols=[("text", None)],
            prompt="Classify the document.",
            model="claude-haiku-4-5",
            key_column="sha1",
            truncate=False,
        )

        assert rendered["full_prompt_content"] == "Classify the document.\n\ntext: row-specific content"
        assert rendered["message_content"] == [
            {
                "type": "text",
                "text": "Classify the document.",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": "text: row-specific content",
            },
        ]

    def test_run_enrichment_path_persists_anthropic_cache_usage(self, mocker, tmp_path, capsys):
        db_path = tmp_path / "cache_e2e.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE documents (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO documents (id, text) VALUES (?, ?)",
                ("doc-1", "This row-specific text should not be cached."),
            )

        config = {
            "database": str(db_path),
            "default_table": "documents",
            "key_column": "id",
            "default_model": "claude-haiku-4-5",
            "sql_queries": {
                "all_docs": "SELECT rowid, id FROM documents ORDER BY id",
            },
            "enrichments": [
                {
                    "name": "tone",
                    "input": {
                        "query": "all_docs",
                        "input_columns": ["text"],
                    },
                    "prompt": "Classify the document tone.",
                    "schema": {
                        "hostility_level": {"type": "integer"},
                        "explanation": {"type": "string"},
                    },
                }
            ],
        }
        config_path = tmp_path / "config.yml"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

        provider = CapturingAnthropicProvider()
        mocker.patch("doctrail.llm_providers.factory.get_llm_provider", return_value=provider)
        mocker.patch("doctrail.core_runtime.enrichment.validate_model", return_value=True)

        result = asyncio.run(
            run_enrichment(
                config_path=str(config_path),
                enrichments=["tone"],
                skip_cost_check=True,
            )
        )

        assert result["status"] == "success"
        output = capsys.readouterr().out
        assert "Prompt cache: 1,024 created + 2,048 read" in output
        assert len(provider.calls) == 1
        content = provider.calls[0]["messages"][0]["content"]
        assert content == [
            {
                "type": "text",
                "text": "Classify the document tone.",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": "text: This row-specific text should not be cached.",
            },
        ]

        expected_cost = TokenUsage(
            input_tokens=6500,
            cached_input_tokens=2048,
            cache_creation_input_tokens=1024,
            output_tokens=12,
            model="claude-haiku-4-5",
        ).estimate_cost()
        with sqlite3.connect(db_path) as conn:
            audit_row = conn.execute(
                """
                SELECT input_tokens, cached_input_tokens, cache_creation_input_tokens,
                       output_tokens, estimated_cost
                FROM _enrichment_audit
                """
            ).fetchone()
            run_row = conn.execute(
                """
                SELECT status, success_count, error_count, input_tokens,
                       cached_input_tokens, cache_creation_input_tokens,
                       output_tokens, estimated_cost
                FROM _enrichment_runs
                """
            ).fetchone()

        assert audit_row[:4] == (6500, 2048, 1024, 12)
        assert audit_row[4] == pytest.approx(expected_cost)
        assert run_row[:7] == ("completed", 1, 0, 6500, 2048, 1024, 12)
        assert run_row[7] == pytest.approx(expected_cost)


# --- Text fallback tests ---

class TestAnthropicTextFallback:
    """Test text + JSON extraction fallback when structured output fails."""

    @pytest.mark.asyncio
    async def test_fallback_to_text_extraction(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")

        # Tier 1 fails
        provider.client.messages.parse = AsyncMock(
            side_effect=Exception("Structured output not supported")
        )

        # Tier 2: text generation returns JSON
        json_response = '{"hostility_level": 2, "explanation": "mild hostility"}'
        provider.client.messages.create = AsyncMock(
            return_value=_make_text_response(json_response)
        )

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify this"}],
            pydantic_model=SimpleResult,
        )

        assert isinstance(result, SimpleResult)
        assert result.hostility_level == 2

    @pytest.mark.asyncio
    async def test_fallback_extracts_from_markdown(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-opus-4")

        provider.client.messages.parse = AsyncMock(
            side_effect=Exception("parse failed")
        )

        markdown_response = '''Here is my analysis:

```json
{"hostility_level": 4, "explanation": "very hostile"}
```'''

        provider.client.messages.create = AsyncMock(
            return_value=_make_text_response(markdown_response)
        )

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify"}],
            pydantic_model=SimpleResult,
        )

        assert result.hostility_level == 4

    @pytest.mark.asyncio
    async def test_fallback_with_complex_model(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")

        provider.client.messages.parse = AsyncMock(
            side_effect=Exception("parse failed")
        )

        json_response = json.dumps({
            "category": "academic",
            "confidence": 0.92,
            "tags": ["research", "methodology"]
        })
        provider.client.messages.create = AsyncMock(
            return_value=_make_text_response(json_response)
        )

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify"}],
            pydantic_model=ClassifyResult,
        )

        assert isinstance(result, ClassifyResult)
        assert result.category == "academic"
        assert len(result.tags) == 2


# --- Text generation tests ---

class TestAnthropicTextGeneration:

    @pytest.mark.asyncio
    async def test_generate_text(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-haiku-4-5")

        provider.client.messages.create = AsyncMock(
            return_value=_make_text_response("This is a test response.")
        )

        result = await provider.generate_text(
            messages=[{"role": "user", "content": "hello"}],
        )

        assert result == "This is a test response."
        call_kwargs = provider.client.messages.create.call_args[1]
        assert call_kwargs["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    async def test_generate_text_normalizes_dotted_alias(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-haiku-4.5")

        provider.client.messages.create = AsyncMock(
            return_value=_make_text_response("This is a test response.")
        )

        result = await provider.generate_text(
            messages=[{"role": "user", "content": "hello"}],
        )

        assert result == "This is a test response."
        assert provider.model == "claude-haiku-4-5"
        assert provider.client.messages.create.call_args[1]["model"] == "claude-haiku-4-5"


# --- Message conversion tests ---

class TestMessageConversion:

    def test_no_system_message(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")
        system, messages = provider._convert_messages([
            {"role": "user", "content": "hello"},
        ])
        assert system is None
        assert len(messages) == 1
        assert messages[0]["role"] == "user"

    def test_system_message_extracted(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")
        system, messages = provider._convert_messages([
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ])
        assert system == [
            {
                "type": "text",
                "text": "You are helpful.",
                "cache_control": {"type": "ephemeral"},
            }
        ]
        assert len(messages) == 1

    def test_multiple_system_messages_merged(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")
        system, messages = provider._convert_messages([
            {"role": "system", "content": "Be helpful."},
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "hello"},
        ])
        assert "Be helpful." in system[0]["text"]
        assert "Be concise." in system[0]["text"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert len(messages) == 1


# --- Token usage tests ---

class TestTokenUsage:

    def test_cost_estimation(self):
        usage = TokenUsage(input_tokens=1_000_000, output_tokens=100_000, model="claude-sonnet-4")
        cost = usage.estimate_cost()
        # claude-sonnet-4: $3/1M input + $15/1M output
        expected = 3.00 + 1.50  # 1M input + 100K output
        assert abs(cost - expected) < 0.01

    def test_unknown_model_fallback(self):
        usage = TokenUsage(input_tokens=1000, output_tokens=100, model="claude-future-model")
        cost = usage.estimate_cost()
        # Unknown models return $0 with a warning (no silent fallback to wrong price)
        assert cost == 0.0

    def test_anthropic_cache_token_cost_estimation(self):
        usage = TokenUsage(
            input_tokens=1_000_000,
            output_tokens=100_000,
            cache_creation_input_tokens=100_000,
            cached_input_tokens=250_000,
            model="claude-haiku-4-5",
        )
        input_price, output_price = get_model_price("claude-haiku-4-5")

        expected = (
            (650_000 / 1_000_000) * input_price
            + (100_000 / 1_000_000) * input_price * 1.25
            + (250_000 / 1_000_000) * input_price * 0.1
            + (100_000 / 1_000_000) * output_price
        )
        assert usage.estimate_cost() == pytest.approx(expected)


# --- Provider init tests ---

class TestProviderInit:

    def test_context_limits(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-opus-4")
        assert provider.max_context_tokens == 200000

    def test_default_context_limit(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-unknown")
        assert provider.max_context_tokens == 200000

    def test_token_counting(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")
        count = provider.count_tokens("Hello world")
        assert count > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
