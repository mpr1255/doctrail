"""Tests for OpenAI provider, especially the three-tier structured output fallback.

Tests the fallback chain:
1. Native structured output (beta.chat.completions.parse)
2. JSON mode (response_format=json_object + schema in prompt)
3. Plain text + JSON extraction from markdown/explanatory text
"""

import json
import os
import urllib.error
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from pydantic import BaseModel, Field
from typing import Optional

from doctrail.llm_providers.anthropic_provider import AnthropicProvider, TokenUsage as AnthropicTokenUsage
from doctrail.llm_providers.gemini_provider import GeminiProvider, TokenUsage as GeminiTokenUsage
from doctrail.llm_providers.openai_provider import OpenAIProvider, TokenUsage
from doctrail.utils.model_pricing import get_openai_batch_model_info


# --- Test models ---

class SimpleResult(BaseModel):
    hostility_level: int
    explanation: str


class ClassifyResult(BaseModel):
    category: str
    confidence: float
    tags: list[str]


class BatchIncidentItem(BaseModel):
    location_city_en: str
    location_province_en: str
    should_keep: bool


class BatchIncidentItemNullable(BaseModel):
    location_city_en: Optional[str] = None
    location_province_en: Optional[str] = None
    should_keep: Optional[bool] = None


class BatchIncidentResult(BaseModel):
    incidents: list[BatchIncidentItem]


class BatchIncidentNullableResult(BaseModel):
    incidents: list[BatchIncidentItemNullable]


class BoundedIntegerResult(BaseModel):
    menace_level: int = Field(ge=0, le=3)
    explanation: str


# --- _extract_json_from_text tests (pure unit tests, no mocking needed) ---

class TestExtractJsonFromText:
    """Test the JSON extraction helper that handles messy LLM text output."""

    def test_plain_json(self):
        text = '{"hostility_level": 3, "explanation": "aggressive tone"}'
        result = OpenAIProvider._extract_json_from_text(text)
        assert result == {"hostility_level": 3, "explanation": "aggressive tone"}

    def test_json_with_whitespace(self):
        text = '  \n  {"hostility_level": 2, "explanation": "mild"}  \n  '
        result = OpenAIProvider._extract_json_from_text(text)
        assert result["hostility_level"] == 2

    def test_markdown_json_code_block(self):
        text = '''Here is my analysis:

```json
{"hostility_level": 4, "explanation": "very hostile language used"}
```

I hope this helps!'''
        result = OpenAIProvider._extract_json_from_text(text)
        assert result["hostility_level"] == 4
        assert result["explanation"] == "very hostile language used"

    def test_markdown_plain_code_block(self):
        text = '''The result is:

```
{"hostility_level": 1, "explanation": "neutral"}
```'''
        result = OpenAIProvider._extract_json_from_text(text)
        assert result["hostility_level"] == 1

    def test_json_with_preamble(self):
        """Models like Claude via OpenRouter often add explanatory text before JSON."""
        text = '''Based on my analysis of the document, I would classify this as follows:

{"hostility_level": 2, "explanation": "somewhat confrontational language"}

This classification takes into account the overall tone and specific phrases used.'''
        result = OpenAIProvider._extract_json_from_text(text)
        assert result["hostility_level"] == 2

    def test_json_with_only_preamble(self):
        """JSON preceded by text, no trailing text."""
        text = '''After careful review:
{"category": "policy", "confidence": 0.85, "tags": ["government", "regulation"]}'''
        result = OpenAIProvider._extract_json_from_text(text)
        assert result["category"] == "policy"
        assert result["confidence"] == 0.85

    def test_multiline_json_in_code_block(self):
        text = '''```json
{
    "hostility_level": 3,
    "explanation": "the text contains aggressive rhetoric and threatening language"
}
```'''
        result = OpenAIProvider._extract_json_from_text(text)
        assert result["hostility_level"] == 3

    def test_empty_text_raises(self):
        with pytest.raises(ValueError, match="Empty text"):
            OpenAIProvider._extract_json_from_text("")

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="Could not extract JSON"):
            OpenAIProvider._extract_json_from_text("This is just plain text with no JSON at all.")

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Could not extract JSON"):
            OpenAIProvider._extract_json_from_text("{this is not: valid json, missing quotes}")

    def test_nested_json(self):
        text = '{"hostility_level": 2, "explanation": "text with {braces} inside"}'
        result = OpenAIProvider._extract_json_from_text(text)
        assert result["hostility_level"] == 2


# --- Mock helpers ---

def _make_usage_mock(prompt_tokens=100, completion_tokens=50):
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    return usage


def _make_parse_response(parsed_obj, usage=None):
    """Create a mock response for beta.chat.completions.parse()."""
    message = MagicMock()
    message.parsed = parsed_obj
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage or _make_usage_mock()
    return response


def _make_chat_response(content, usage=None):
    """Create a mock response for chat.completions.create()."""
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage or _make_usage_mock()
    return response


# --- Tier 1: Native structured output tests ---

class TestTier1NativeStructured:
    """Test that native structured output works when the API supports it."""

    @pytest.mark.asyncio
    async def test_native_structured_success(self):
        provider = OpenAIProvider(api_key="test-key", model="gpt-4o-mini")

        expected = SimpleResult(hostility_level=3, explanation="aggressive")
        mock_response = _make_parse_response(expected)

        provider.client.beta.chat.completions.parse = AsyncMock(return_value=mock_response)

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify this"}],
            pydantic_model=SimpleResult,
        )

        assert isinstance(result, SimpleResult)
        assert result.hostility_level == 3
        assert result.explanation == "aggressive"

    @pytest.mark.asyncio
    async def test_native_structured_with_usage(self):
        provider = OpenAIProvider(api_key="test-key", model="gpt-4o-mini")

        expected = SimpleResult(hostility_level=1, explanation="neutral")
        usage = _make_usage_mock(prompt_tokens=200, completion_tokens=30)
        mock_response = _make_parse_response(expected, usage=usage)

        provider.client.beta.chat.completions.parse = AsyncMock(return_value=mock_response)

        result, token_usage = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify this"}],
            pydantic_model=SimpleResult,
            return_usage=True,
        )

        assert result.hostility_level == 1
        assert isinstance(token_usage, TokenUsage)
        assert token_usage.input_tokens == 200
        assert token_usage.output_tokens == 30


# --- Tier 2: JSON mode fallback tests ---

class TestTier2JsonMode:
    """Test JSON mode fallback when native structured output fails."""

    @pytest.mark.asyncio
    async def test_json_mode_fallback(self):
        """When tier 1 fails, tier 2 should use JSON mode."""
        provider = OpenAIProvider(
            api_key="test-key", model="anthropic/claude-sonnet-4",
            base_url="https://openrouter.ai/api/v1"
        )

        # Tier 1 fails
        provider.client.beta.chat.completions.parse = AsyncMock(
            side_effect=Exception("Structured output not supported")
        )

        # Tier 2 succeeds with valid JSON
        json_response = json.dumps({"hostility_level": 2, "explanation": "mild hostility"})
        mock_response = _make_chat_response(json_response)
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify this"}],
            pydantic_model=SimpleResult,
        )

        assert isinstance(result, SimpleResult)
        assert result.hostility_level == 2
        assert result.explanation == "mild hostility"

        # Verify JSON mode was used (response_format should be json_object)
        call_kwargs = provider.client.chat.completions.create.call_args[1]
        assert call_kwargs["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_json_mode_injects_schema(self):
        """Verify that JSON mode adds schema instructions to the prompt."""
        provider = OpenAIProvider(
            api_key="test-key", model="deepseek-chat",
            base_url="https://openrouter.ai/api/v1"
        )

        provider.client.beta.chat.completions.parse = AsyncMock(
            side_effect=Exception("not supported")
        )

        json_response = json.dumps({"hostility_level": 0, "explanation": "peaceful"})
        mock_response = _make_chat_response(json_response)
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        await provider.generate_structured(
            messages=[{"role": "user", "content": "classify this"}],
            pydantic_model=SimpleResult,
        )

        # Check that schema was injected into messages
        call_kwargs = provider.client.chat.completions.create.call_args[1]
        messages = call_kwargs["messages"]
        # Should have a system message with schema
        system_msg = next((m for m in messages if m["role"] == "system"), None)
        assert system_msg is not None
        assert "JSON Schema" in system_msg["content"]
        assert "hostility_level" in system_msg["content"]

    @pytest.mark.asyncio
    async def test_json_mode_preserves_existing_system_prompt(self):
        """JSON mode should append schema to existing system prompt, not replace it."""
        provider = OpenAIProvider(
            api_key="test-key", model="test-model",
            base_url="https://openrouter.ai/api/v1"
        )

        provider.client.beta.chat.completions.parse = AsyncMock(
            side_effect=Exception("not supported")
        )

        json_response = json.dumps({"hostility_level": 1, "explanation": "fine"})
        mock_response = _make_chat_response(json_response)
        provider.client.chat.completions.create = AsyncMock(return_value=mock_response)

        await provider.generate_structured(
            messages=[
                {"role": "system", "content": "You are a content analyst."},
                {"role": "user", "content": "classify this text"},
            ],
            pydantic_model=SimpleResult,
        )

        call_kwargs = provider.client.chat.completions.create.call_args[1]
        messages = call_kwargs["messages"]
        # System message should contain both original content and schema
        assert "You are a content analyst." in messages[0]["content"]
        assert "JSON Schema" in messages[0]["content"]


# --- Tier 3: Text extraction fallback tests ---

class TestTier3TextExtraction:
    """Test plain text + JSON extraction when both tier 1 and tier 2 fail."""

    @pytest.mark.asyncio
    async def test_text_extraction_from_markdown(self):
        """When tier 1 and 2 fail, tier 3 should extract JSON from markdown."""
        provider = OpenAIProvider(
            api_key="test-key", model="anthropic/claude-sonnet-4",
            base_url="https://openrouter.ai/api/v1"
        )

        # Tier 1 fails
        provider.client.beta.chat.completions.parse = AsyncMock(
            side_effect=Exception("Structured output not supported")
        )

        # Tier 2 also fails (some APIs don't support json_object either)
        # First call is JSON mode (tier 2), second call is text (tier 3)
        markdown_response = '''Based on my analysis:

```json
{"hostility_level": 3, "explanation": "the document uses threatening language"}
```

This reflects the aggressive tone throughout.'''

        # JSON mode fails, then text mode returns markdown-wrapped JSON
        provider.client.chat.completions.create = AsyncMock(
            side_effect=[
                Exception("json_object not supported"),  # tier 2
                _make_chat_response(markdown_response),   # tier 3
            ]
        )

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify this"}],
            pydantic_model=SimpleResult,
        )

        assert isinstance(result, SimpleResult)
        assert result.hostility_level == 3

    @pytest.mark.asyncio
    async def test_text_extraction_from_preamble(self):
        """Extract JSON from text with explanatory preamble (no markdown)."""
        provider = OpenAIProvider(
            api_key="test-key", model="meta-llama/llama-3.1-70b",
            base_url="https://openrouter.ai/api/v1"
        )

        provider.client.beta.chat.completions.parse = AsyncMock(
            side_effect=Exception("not supported")
        )

        preamble_response = 'I would classify this document as follows: {"hostility_level": 4, "explanation": "extremely hostile"}'

        provider.client.chat.completions.create = AsyncMock(
            side_effect=[
                Exception("json_object not supported"),
                _make_chat_response(preamble_response),
            ]
        )

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify this"}],
            pydantic_model=SimpleResult,
        )

        assert result.hostility_level == 4
        assert result.explanation == "extremely hostile"

    @pytest.mark.asyncio
    async def test_tier3_injects_schema_instructions(self):
        """Tier 3 should inject schema into messages so models know to return JSON."""
        provider = OpenAIProvider(
            api_key="test-key", model="anthropic/claude-sonnet-4",
            base_url="https://openrouter.ai/api/v1"
        )

        provider.client.beta.chat.completions.parse = AsyncMock(
            side_effect=Exception("not supported")
        )

        # Track the messages sent to generate_text (tier 3)
        tier3_messages = None

        async def mock_create(**kwargs):
            nonlocal tier3_messages
            if kwargs.get("response_format") == {"type": "json_object"}:
                # Tier 2 call — fail it
                raise Exception("json_object not supported")
            else:
                # Tier 3 call — capture messages and return JSON
                tier3_messages = kwargs.get("messages")
                return _make_chat_response('{"hostility_level": 1, "explanation": "fine"}')

        provider.client.chat.completions.create = AsyncMock(side_effect=mock_create)

        await provider.generate_structured(
            messages=[{"role": "user", "content": "classify this"}],
            pydantic_model=SimpleResult,
        )

        # Tier 3 should have injected schema instructions
        assert tier3_messages is not None
        system_msg = next((m for m in tier3_messages if m["role"] == "system"), None)
        assert system_msg is not None
        assert "JSON" in system_msg["content"]
        assert "hostility_level" in system_msg["content"]

    @pytest.mark.asyncio
    async def test_tier3_returns_usage_when_requested(self):
        """Tier 3 should preserve chat completion usage for cost audit."""
        provider = OpenAIProvider(
            api_key="test-key",
            model="anthropic/claude-sonnet-4",
            base_url="https://openrouter.ai/api/v1",
            capabilities={"structured_outputs": False, "response_format": False},
        )

        provider.client.chat.completions.create = AsyncMock(
            return_value=_make_chat_response(
                '{"hostility_level": 2, "explanation": "confrontational rhetoric"}',
                usage=_make_usage_mock(prompt_tokens=123, completion_tokens=17),
            )
        )

        result, token_usage = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify this"}],
            pydantic_model=SimpleResult,
            return_usage=True,
        )

        assert result.hostility_level == 2
        assert isinstance(token_usage, TokenUsage)
        assert token_usage.input_tokens == 123
        assert token_usage.output_tokens == 17

    @pytest.mark.asyncio
    async def test_claude_sonnet_via_openrouter(self):
        """Simulate Claude Sonnet 4 via OpenRouter: no structured_outputs, no response_format.

        This is the exact scenario that was broken before the fix.
        Claude returns clean JSON when instructed properly (tier 3).
        """
        provider = OpenAIProvider(
            api_key="test-key", model="anthropic/claude-sonnet-4",
            base_url="https://openrouter.ai/api/v1"
        )

        # Tier 1: fails (Claude doesn't support structured_outputs on OpenRouter)
        provider.client.beta.chat.completions.parse = AsyncMock(
            side_effect=Exception("structured_outputs not supported for this model")
        )

        # Tier 2: fails (Claude doesn't support response_format on OpenRouter)
        # Tier 3: succeeds — Claude returns clean JSON when schema is in prompt
        provider.client.chat.completions.create = AsyncMock(
            side_effect=[
                Exception("response_format is not supported for this model"),
                _make_chat_response('{"hostility_level": 2, "explanation": "confrontational rhetoric"}'),
            ]
        )

        result = await provider.generate_structured(
            messages=[
                {"role": "system", "content": "You are a content analyst."},
                {"role": "user", "content": "Classify the hostility level of this text."},
            ],
            pydantic_model=SimpleResult,
        )

        assert isinstance(result, SimpleResult)
        assert result.hostility_level == 2
        assert "confrontational" in result.explanation


# --- Full fallback chain test ---

class TestFullFallbackChain:
    """Test the complete fallback sequence across all three tiers."""

    @pytest.mark.asyncio
    async def test_all_tiers_fail(self):
        """When all three tiers fail, should raise the final error."""
        provider = OpenAIProvider(
            api_key="test-key", model="broken-model",
            base_url="https://example.com/api/v1"
        )

        provider.client.beta.chat.completions.parse = AsyncMock(
            side_effect=Exception("tier 1 fail")
        )
        provider.client.chat.completions.create = AsyncMock(
            side_effect=[
                Exception("tier 2 fail"),
                _make_chat_response("This is just plain text with no JSON whatsoever."),
            ]
        )

        with pytest.raises(ValueError, match="Could not extract JSON"):
            await provider.generate_structured(
                messages=[{"role": "user", "content": "classify"}],
                pydantic_model=SimpleResult,
            )

    @pytest.mark.asyncio
    async def test_tier1_success_skips_rest(self):
        """When tier 1 succeeds, tiers 2 and 3 are never called."""
        provider = OpenAIProvider(api_key="test-key", model="gpt-4o-mini")

        expected = SimpleResult(hostility_level=0, explanation="peaceful")
        provider.client.beta.chat.completions.parse = AsyncMock(
            return_value=_make_parse_response(expected)
        )
        provider.client.chat.completions.create = AsyncMock(
            side_effect=Exception("should not be called")
        )

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify"}],
            pydantic_model=SimpleResult,
        )

        assert result.hostility_level == 0
        provider.client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_complex_model_through_fallback(self):
        """Test that complex Pydantic models with lists work through fallback."""
        provider = OpenAIProvider(
            api_key="test-key", model="test-model",
            base_url="https://openrouter.ai/api/v1"
        )

        provider.client.beta.chat.completions.parse = AsyncMock(
            side_effect=Exception("not supported")
        )

        json_response = json.dumps({
            "category": "academic",
            "confidence": 0.92,
            "tags": ["research", "methodology", "statistics"]
        })
        provider.client.chat.completions.create = AsyncMock(
            return_value=_make_chat_response(json_response)
        )

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify"}],
            pydantic_model=ClassifyResult,
        )

        assert isinstance(result, ClassifyResult)
        assert result.category == "academic"
        assert result.confidence == 0.92
        assert len(result.tags) == 3
        assert "research" in result.tags


# --- Provider initialization tests ---

class TestProviderInit:
    def test_native_openai(self):
        provider = OpenAIProvider(api_key="test-key", model="gpt-4o")
        assert not provider._is_third_party

    def test_openrouter(self):
        provider = OpenAIProvider(
            api_key="test-key", model="anthropic/claude-sonnet-4",
            base_url="https://openrouter.ai/api/v1"
        )
        assert provider._is_third_party


# --- Capability-aware routing tests ---

class TestCapabilityRouting:
    """Test that OpenRouter model capabilities control which tiers are attempted."""

    @pytest.mark.asyncio
    async def test_no_caps_model_falls_to_tier3(self):
        """Models supporting neither structured_outputs nor response_format use tier 3."""
        provider = OpenAIProvider(
            api_key="test-key", model="anthropic/claude-sonnet-4",
            base_url="https://openrouter.ai/api/v1",
            capabilities={"structured_outputs": False, "response_format": False},
        )

        # Tier 1 and 2 should NOT be called (skipped entirely)
        provider.client.beta.chat.completions.parse = AsyncMock(
            side_effect=Exception("should not be called")
        )

        # Tier 3: text generation returns JSON
        provider.client.chat.completions.create = AsyncMock(
            return_value=_make_chat_response('{"hostility_level": 3, "explanation": "hostile text"}')
        )

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify"}],
            pydantic_model=SimpleResult,
        )

        assert isinstance(result, SimpleResult)
        assert result.hostility_level == 3
        # Tier 1 should NOT have been called
        provider.client.beta.chat.completions.parse.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_tier1_when_no_structured_outputs(self):
        """Models with response_format but no structured_outputs skip tier 1, use tier 2."""
        provider = OpenAIProvider(
            api_key="test-key", model="meta-llama/llama-3.1-70b-instruct",
            base_url="https://openrouter.ai/api/v1",
            capabilities={"structured_outputs": False, "response_format": True},
        )

        # Tier 1 should NOT be called
        provider.client.beta.chat.completions.parse = AsyncMock(
            side_effect=Exception("should not be called")
        )

        # Tier 2 should be called and succeed
        json_response = json.dumps({"hostility_level": 2, "explanation": "mild"})
        provider.client.chat.completions.create = AsyncMock(
            return_value=_make_chat_response(json_response)
        )

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify"}],
            pydantic_model=SimpleResult,
        )

        assert result.hostility_level == 2
        provider.client.beta.chat.completions.parse.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_to_tier2_no_tier3_fallback(self):
        """When caps say rf-only and tier 2 fails, tier 3 is NOT attempted."""
        provider = OpenAIProvider(
            api_key="test-key", model="meta-llama/llama-3.1-70b-instruct",
            base_url="https://openrouter.ai/api/v1",
            capabilities={"structured_outputs": False, "response_format": True},
        )

        provider.client.beta.chat.completions.parse = AsyncMock()
        provider.client.chat.completions.create = AsyncMock(
            side_effect=Exception("tier 2 failed")
        )

        with pytest.raises(ValueError, match="Structured output failed"):
            await provider.generate_structured(
                messages=[{"role": "user", "content": "classify"}],
                pydantic_model=SimpleResult,
            )

    @pytest.mark.asyncio
    async def test_full_caps_model_uses_tier1(self):
        """Models with both capabilities try tier 1 first, skip tier 3."""
        provider = OpenAIProvider(
            api_key="test-key", model="openai/gpt-4o-mini",
            base_url="https://openrouter.ai/api/v1",
            capabilities={"structured_outputs": True, "response_format": True},
        )

        expected = SimpleResult(hostility_level=5, explanation="very hostile")
        provider.client.beta.chat.completions.parse = AsyncMock(
            return_value=_make_parse_response(expected)
        )
        provider.client.chat.completions.create = AsyncMock(
            side_effect=Exception("should not be called")
        )

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify"}],
            pydantic_model=SimpleResult,
        )

        assert result.hostility_level == 5
        provider.client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_caps_falls_to_tier2_no_tier3(self):
        """When caps=both and tier 1 fails, tries tier 2 but NOT tier 3."""
        provider = OpenAIProvider(
            api_key="test-key", model="openai/gpt-4o-mini",
            base_url="https://openrouter.ai/api/v1",
            capabilities={"structured_outputs": True, "response_format": True},
        )

        provider.client.beta.chat.completions.parse = AsyncMock(
            side_effect=Exception("tier 1 fail")
        )

        json_response = json.dumps({"hostility_level": 1, "explanation": "neutral"})
        provider.client.chat.completions.create = AsyncMock(
            return_value=_make_chat_response(json_response)
        )

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify"}],
            pydantic_model=SimpleResult,
        )

        assert result.hostility_level == 1

    @pytest.mark.asyncio
    async def test_native_openai_no_caps_tries_all_tiers(self):
        """Native OpenAI (caps=None) tries all 3 tiers as emergency fallback."""
        provider = OpenAIProvider(api_key="test-key", model="gpt-4o")
        assert provider._capabilities is None

        # Tier 1 fails
        provider.client.beta.chat.completions.parse = AsyncMock(
            side_effect=Exception("tier 1 fail")
        )

        # Tier 2 fails, tier 3 succeeds
        provider.client.chat.completions.create = AsyncMock(
            side_effect=[
                Exception("tier 2 fail"),
                _make_chat_response('{"hostility_level": 0, "explanation": "peaceful"}'),
            ]
        )

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify"}],
            pydantic_model=SimpleResult,
        )

        assert result.hostility_level == 0

    @pytest.mark.asyncio
    async def test_no_caps_model_tier3_extracts_markdown_json(self):
        """Models with no caps should extract JSON from markdown code blocks in tier 3."""
        provider = OpenAIProvider(
            api_key="test-key", model="anthropic/claude-opus-4",
            base_url="https://openrouter.ai/api/v1",
            capabilities={"structured_outputs": False, "response_format": False},
        )

        markdown_response = '''Here is the analysis:

```json
{"hostility_level": 5, "explanation": "extremely hostile"}
```'''

        provider.client.beta.chat.completions.parse = AsyncMock()
        provider.client.chat.completions.create = AsyncMock(
            return_value=_make_chat_response(markdown_response)
        )

        result = await provider.generate_structured(
            messages=[{"role": "user", "content": "classify"}],
            pydantic_model=SimpleResult,
        )

        assert result.hostility_level == 5
        assert result.explanation == "extremely hostile"


class TestBatchParsing:
    """Batch-specific parsing and pricing behavior."""

    def test_parse_batch_chat_response_accepts_null_only_validation_failures_with_nullable_fallback(self):
        provider = OpenAIProvider(api_key="test-key", model="gpt-4o-mini")
        body = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "incidents": [{
                            "location_city_en": "Beijing",
                            "location_province_en": None,
                            "should_keep": True,
                        }]
                    })
                }
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }

        parsed, usage, raw_json = provider.parse_batch_chat_response(
            body,
            BatchIncidentResult,
            nullable_pydantic_model=BatchIncidentNullableResult,
        )

        assert parsed["incidents"][0]["location_province_en"] is None
        assert usage.cached_input_tokens == 0
        raw_payload = json.loads(raw_json)
        assert raw_payload["incidents"][0]["location_city_en"] == "Beijing"
        assert "validation_warning" in raw_payload
        assert "location_province_en" in raw_payload["validation_warning"]

    def test_batch_token_usage_uses_cached_input_pricing(self):
        batch_catalog = get_openai_batch_model_info("gpt-4o-mini")
        assert batch_catalog is not None

        usage = TokenUsage(
            input_tokens=1_000_000,
            output_tokens=100_000,
            cached_input_tokens=250_000,
            model="gpt-4o-mini",
            batch_pricing=True,
        )

        expected = (
            (750_000 / 1_000_000) * batch_catalog["batch_input"]
            + (250_000 / 1_000_000) * batch_catalog["batch_cached_input"]
            + (100_000 / 1_000_000) * batch_catalog["batch_output"]
        )
        assert usage.estimate_cost() == pytest.approx(expected)

    def test_build_batch_chat_request_defaults_gpt5_reasoning_to_minimal(self):
        provider = OpenAIProvider(api_key="test-key", model="gpt-5-mini")
        request = provider.build_batch_chat_request(
            messages=[{"role": "user", "content": "Return JSON"}],
            pydantic_model=SimpleResult,
        )

        assert request["reasoning_effort"] == "minimal"
        assert "max_tokens" not in request

    def test_build_batch_chat_request_uses_max_completion_tokens_for_gpt5(self):
        provider = OpenAIProvider(api_key="test-key", model="gpt-5-mini")
        request = provider.build_batch_chat_request(
            messages=[{"role": "user", "content": "Return JSON"}],
            max_tokens=77,
        )

        assert request["max_completion_tokens"] == 77
        assert "max_tokens" not in request

    @pytest.mark.asyncio
    async def test_generate_structured_passes_reasoning_effort_for_gpt5(self):
        provider = OpenAIProvider(api_key="test-key", model="gpt-5-mini")
        expected = SimpleResult(hostility_level=1, explanation="neutral")
        mock_response = _make_parse_response(expected)
        provider.client.beta.chat.completions.parse = AsyncMock(return_value=mock_response)

        await provider.generate_structured(
            messages=[{"role": "user", "content": "classify this"}],
            pydantic_model=SimpleResult,
            reasoning_effort="low",
        )

        call_kwargs = provider.client.beta.chat.completions.parse.call_args[1]
        assert call_kwargs["reasoning_effort"] == "low"


class TestAnthropicBatchHelpers:
    """Anthropic batch request, parsing, and pricing behavior."""

    def test_build_batch_message_request_uses_output_config_json_schema(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-haiku-4-5")
        request = provider.build_batch_message_request(
            messages=[{"role": "user", "content": "Return JSON"}],
            pydantic_model=SimpleResult,
        )

        assert request["model"] == "claude-haiku-4-5"
        assert request["output_config"]["format"]["type"] == "json_schema"
        assert request["output_config"]["format"]["schema"]["additionalProperties"] is False
        assert request["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_get_batch_schema_compatibility_issues_reports_integer_bounds(self):
        issues = AnthropicProvider.get_batch_schema_compatibility_issues(BoundedIntegerResult)

        assert len(issues) == 1
        issue = issues[0]
        assert issue["level"] == "warning"
        assert issue["code"] == "anthropic_batch_unsupported_integer_constraints"
        assert "$.properties.menace_level.minimum" in issue["paths"]
        assert "$.properties.menace_level.maximum" in issue["paths"]

    def test_build_batch_message_request_strips_integer_bounds_anthropic_rejects(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")
        request = provider.build_batch_message_request(
            messages=[{"role": "user", "content": "Return JSON"}],
            pydantic_model=BoundedIntegerResult,
        )

        menace_schema = request["output_config"]["format"]["schema"]["properties"]["menace_level"]
        assert menace_schema["type"] == "integer"
        assert "minimum" not in menace_schema
        assert "maximum" not in menace_schema

    def test_parse_batch_message_response_accepts_null_only_validation_failures_with_nullable_fallback(self):
        provider = AnthropicProvider(api_key="test-key", model="claude-sonnet-4")
        message = {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "incidents": [{
                        "location_city_en": "Beijing",
                        "location_province_en": None,
                        "should_keep": True,
                    }]
                }),
            }],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }

        parsed, usage, raw_json = provider.parse_batch_message_response(
            message,
            BatchIncidentResult,
            nullable_pydantic_model=BatchIncidentNullableResult,
        )

        assert parsed["incidents"][0]["location_province_en"] is None
        assert usage.batch_pricing is True
        assert json.loads(raw_json)["incidents"][0]["location_city_en"] == "Beijing"

    def test_anthropic_batch_token_usage_halves_standard_pricing(self):
        usage = AnthropicTokenUsage(
            input_tokens=1_000_000,
            output_tokens=100_000,
            model="claude-sonnet-4",
            batch_pricing=True,
        )

        assert usage.estimate_cost() == pytest.approx(2.25)


class TestGeminiBatchHelpers:
    """Gemini batch request, parsing, and pricing behavior."""

    def test_build_batch_generate_content_request_uses_json_mode_prompt_injection(self):
        provider = GeminiProvider(api_key="test-key", model="gemini-2.5-flash")
        request = provider.build_batch_generate_content_request(
            messages=[{"role": "user", "content": "Return JSON"}],
            pydantic_model=SimpleResult,
        )

        assert request["contents"][0]["role"] == "user"
        assert request["generationConfig"]["responseMimeType"] == "application/json"
        assert "JSON Schema" in request["contents"][0]["parts"][0]["text"]

    def test_build_batch_generate_content_file_request_uses_jsonl_shape(self):
        provider = GeminiProvider(api_key="test-key", model="gemini-2.5-flash")
        request = provider.build_batch_generate_content_file_request(
            key="row_7",
            messages=[{"role": "user", "content": "Return JSON"}],
            pydantic_model=SimpleResult,
        )

        assert request["key"] == "row_7"
        assert request["request"]["generation_config"]["response_mime_type"] == "application/json"
        assert "JSON Schema" in request["request"]["contents"][0]["parts"][0]["text"]

    def test_create_batch_job_uses_file_name_input_config(self, monkeypatch):
        provider = GeminiProvider(api_key="test-key", model="gemini-2.5-flash")
        observed = {}

        async def fake_batch_request(method, path, payload=None):
            observed["method"] = method
            observed["path"] = path
            observed["payload"] = payload
            return {
                "name": "batches/test-1",
                "metadata": {
                    "state": "BATCH_STATE_PENDING",
                    "batchStats": {
                        "requestCount": "1",
                        "pendingRequestCount": "1",
                    },
                },
            }

        monkeypatch.setattr(provider, "_batch_request", fake_batch_request)

        batch = asyncio.run(provider.create_batch_job(
            "files/input-123",
            display_name="doctrail-smoke",
        ))

        assert observed["method"] == "POST"
        assert observed["path"] == "models/gemini-2.5-flash:batchGenerateContent"
        assert observed["payload"]["batch"]["input_config"]["file_name"] == "files/input-123"
        assert observed["payload"]["batch"]["display_name"] == "doctrail-smoke"
        assert batch["name"] == "batches/test-1"
        assert batch["state"] == "BATCH_STATE_PENDING"

    def test_parse_batch_generate_content_response_accepts_nullable_fallback(self):
        provider = GeminiProvider(api_key="test-key", model="gemini-2.5-flash")
        response = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps({
                            "incidents": [{
                                "location_city_en": "Beijing",
                                "location_province_en": None,
                                "should_keep": True,
                            }]
                        })
                    }]
                }
            }],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2},
        }

        parsed, usage, raw_json = provider.parse_batch_generate_content_response(
            response,
            BatchIncidentResult,
            nullable_pydantic_model=BatchIncidentNullableResult,
        )

        assert parsed["incidents"][0]["location_province_en"] is None
        assert usage.batch_pricing is True
        assert json.loads(raw_json)["incidents"][0]["location_city_en"] == "Beijing"

    def test_extract_generate_content_text_reads_sdk_parts_without_text_accessor(self):
        class Part:
            thought_signature = "signature"

            @property
            def text(self):
                raise AssertionError("part.text should not be accessed for SDK objects")

            def to_json_dict(self):
                return {"text": '{"ok": true}', "thought_signature": self.thought_signature}

        class Content:
            parts = [Part()]

        class Candidate:
            content = Content()

        class Response:
            candidates = [Candidate()]

            @property
            def text(self):
                raise AssertionError("response.text should not be accessed for SDK objects")

        assert GeminiProvider.extract_generate_content_text(Response()) == '{"ok": true}'

    def test_gemini_usage_counts_thought_tokens_as_output(self):
        provider = GeminiProvider(api_key="test-key", model="gemini-3.5-flash")
        usage = provider.build_token_usage(
            {
                "promptTokenCount": 10,
                "candidatesTokenCount": 2,
                "thoughtsTokenCount": 50,
            }
        )

        assert usage.input_tokens == 10
        assert usage.output_tokens == 52
        assert usage.thought_tokens == 50

    def test_gemini_batch_token_usage_halves_standard_pricing(self, monkeypatch):
        monkeypatch.setattr(
            "doctrail.utils.model_pricing.get_model_price",
            lambda model: (0.3, 1.5) if model == "gemini-2.5-flash" else (0.0, 0.0),
        )
        usage = GeminiTokenUsage(
            input_tokens=1_000_000,
            output_tokens=100_000,
            model="gemini-2.5-flash",
            batch_pricing=True,
        )

        assert usage.estimate_cost() == pytest.approx(0.225)


# --- Factory integration tests ---

class TestFactoryCapabilities:
    """Test that the factory correctly fetches and passes capabilities."""

    def test_factory_prefers_project_env_file_over_inherited_environment(self, monkeypatch, tmp_path):
        """Provider resolution should prefer the nearest project .env over shell env."""
        import doctrail.llm_providers.factory as factory_module

        (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-project-local\n")
        monkeypatch.chdir(tmp_path)

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-inherited-shell"}):
            provider = factory_module.get_llm_provider("gpt-4o-mini")

        assert provider.client.api_key == "sk-project-local"

    @pytest.mark.asyncio
    async def test_factory_fetches_openrouter_capabilities(self):
        """Factory should query OpenRouter API and pass capabilities to provider."""
        mock_response_data = {
            "data": [
                {
                    "id": "anthropic/claude-sonnet-4",
                    "supported_parameters": ["temperature", "top_p"],
                },
                {
                    "id": "openai/gpt-4o-mini",
                    "supported_parameters": ["temperature", "structured_outputs", "response_format"],
                },
            ]
        }

        import doctrail.llm_providers.factory as factory_module
        # Reset cache before test
        factory_module._openrouter_capabilities_cache = {}
        factory_module._openrouter_cache_loaded = False

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            with patch("doctrail.llm_providers.factory.urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps(mock_response_data).encode()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = mock_resp

                provider = factory_module.get_llm_provider("openrouter/anthropic/claude-sonnet-4")

        assert provider._capabilities == {"structured_outputs": False, "response_format": False}

        # Reset cache after test
        factory_module._openrouter_capabilities_cache = {}
        factory_module._openrouter_cache_loaded = False

    def test_factory_graceful_on_api_error(self):
        """When OpenRouter API is unreachable, capabilities should be None."""
        import doctrail.llm_providers.factory as factory_module
        factory_module._openrouter_capabilities_cache = {}
        factory_module._openrouter_cache_loaded = False

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            with patch("doctrail.llm_providers.factory.urllib.request.urlopen",
                       side_effect=urllib.error.URLError("network down")):
                provider = factory_module.get_llm_provider("openrouter/anthropic/claude-sonnet-4")

        assert provider._capabilities is None

        # Reset cache after test
        factory_module._openrouter_capabilities_cache = {}
        factory_module._openrouter_cache_loaded = False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
