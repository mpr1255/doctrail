#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest",
#     "pytest-mock",
#     "click",
#     "tiktoken",
# ]
# ///
"""Test cost estimation functionality."""

import pytest
import json
from pathlib import Path
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from doctrail.cost_estimation import (
    estimate_enrichment_cost,
    format_cost_estimate,
    should_confirm_cost,
    get_model_validation_error,
    count_tokens,
    estimate_output_tokens,
    get_encoding_for_model,
    validate_model,
)


def test_count_tokens():
    """Test token counting for different models."""
    text = "This is a test document with some content."
    
    # Test GPT-4o models (should use o200k_base)
    tokens_4o = count_tokens(text, "gpt-4o")
    assert tokens_4o > 0
    assert tokens_4o < 20  # Reasonable estimate
    
    # Test older models (should use cl100k_base)
    tokens_4 = count_tokens(text, "gpt-4")
    assert tokens_4 > 0
    
    # Test empty text
    assert count_tokens("", "gpt-4o") == 0
    
    # Test Gemini models (should use character estimate)
    tokens_gemini = count_tokens(text, "gemini-1.5-flash")
    assert tokens_gemini > 0


def test_estimate_output_tokens():
    """Test output token estimation based on schema."""
    # Simple string schema
    simple_schema = {
        "summary": {
            "type": "string",
            "maxLength": 200
        }
    }
    tokens = estimate_output_tokens(simple_schema, 1)
    assert tokens > 0
    assert tokens < 100
    
    # Complex schema with arrays
    complex_schema = {
        "topics": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5
        },
        "sentiment": {
            "enum": ["positive", "negative", "neutral"]
        }
    }
    tokens = estimate_output_tokens(complex_schema, 1)
    assert tokens > 50
    
    # Test that output increases with rows (not necessarily linear)
    tokens_multi = estimate_output_tokens(simple_schema, 10)
    assert tokens_multi > tokens


def test_estimate_enrichment_cost():
    """Test full enrichment cost estimation."""
    model = "gpt-4o-mini"
    prompt_template = "Analyze this document: {content}"
    input_sample = {"content": "This is a sample document with some text content."}
    schema = {
        "summary": {"type": "string", "maxLength": 100},
        "topics": {"type": "array", "items": {"type": "string"}, "maxItems": 3}
    }
    
    # Test with 100 rows, 50 to process
    total_cost, breakdown = estimate_enrichment_cost(
        model=model,
        prompt_template=prompt_template,
        input_columns_sample=input_sample,
        schema=schema,
        num_rows=100,
        rows_to_process=50
    )
    
    assert total_cost > 0
    assert breakdown["model"] == model
    assert breakdown["total_rows_in_query"] == 100
    assert breakdown["rows_to_process"] == 50
    assert breakdown["rows_already_processed"] == 50
    assert breakdown["total_input_tokens"] > 0
    assert breakdown["total_output_tokens"] > 0
    assert breakdown["input_cost"] > 0
    assert breakdown["output_cost"] > 0
    
    # Cost should be reasonable for gpt-4o-mini
    assert total_cost < 1.0  # Less than $1 for 50 rows


def test_estimate_enrichment_cost_openai_batch_uses_batch_pricing(monkeypatch):
    """OpenAI batch estimation should use the verified batch catalog prices."""
    import doctrail.utils.cost_estimation as cost_utils

    monkeypatch.setattr(
        cost_utils,
        "get_openai_batch_model_info",
        lambda model: {
            "batch_input": 0.25,
            "batch_cached_input": 0.025,
            "batch_output": 2.0,
        } if model == "gpt-5-mini" else None,
    )

    total_cost, breakdown = estimate_enrichment_cost(
        model="gpt-5-mini",
        prompt_template="Analyze: {content}",
        input_columns_sample={"content": "short sample"},
        schema={"summary": {"type": "string", "maxLength": 80}},
        num_rows=10,
        rows_to_process=10,
        execution_mode="batch",
    )

    assert total_cost > 0
    assert breakdown["execution_mode"] == "batch"
    assert breakdown["pricing_source"] == "openai_batch_catalog"
    assert breakdown["input_price_per_1m"] == 0.25
    assert breakdown["output_price_per_1m"] == 2.0
    assert breakdown["batch_cached_input_price_per_1m"] == 0.025


def test_estimate_enrichment_cost_anthropic_batch_uses_discount(monkeypatch):
    """Anthropic batch estimation should apply the documented 50% batch discount."""
    import doctrail.utils.cost_estimation as cost_utils

    monkeypatch.setattr(
        cost_utils,
        "get_model_price",
        lambda model: (3.0, 15.0) if model == "claude-sonnet-4" else (0.0, 0.0),
    )

    total_cost, breakdown = estimate_enrichment_cost(
        model="claude-sonnet-4",
        prompt_template="Analyze: {content}",
        input_columns_sample={"content": "short sample"},
        schema={"summary": {"type": "string", "maxLength": 80}},
        num_rows=10,
        rows_to_process=10,
        execution_mode="batch",
    )

    assert total_cost > 0
    assert breakdown["pricing_source"] == "anthropic_batch_discount"
    assert breakdown["input_price_per_1m"] == 1.5
    assert breakdown["output_price_per_1m"] == 7.5


def test_estimate_enrichment_cost_gemini_batch_uses_discount(monkeypatch):
    """Gemini batch estimation should apply the documented 50% batch discount."""
    import doctrail.utils.cost_estimation as cost_utils

    monkeypatch.setattr(
        cost_utils,
        "get_model_price",
        lambda model: (0.3, 1.5) if model == "gemini-2.5-flash" else (0.0, 0.0),
    )

    total_cost, breakdown = estimate_enrichment_cost(
        model="gemini-2.5-flash",
        prompt_template="Analyze: {content}",
        input_columns_sample={"content": "short sample"},
        schema={"summary": {"type": "string", "maxLength": 80}},
        num_rows=10,
        rows_to_process=10,
        execution_mode="batch",
    )

    assert total_cost > 0
    assert breakdown["pricing_source"] == "gemini_batch_discount"
    assert breakdown["input_price_per_1m"] == 0.15
    assert breakdown["output_price_per_1m"] == 0.75


def test_gemini_35_flash_validation_and_pricing(monkeypatch):
    """Gemini 3.5 Flash should validate and use direct Gemini pricing offline."""
    import doctrail.utils.model_pricing as pricing

    monkeypatch.setattr(
        pricing,
        "_ensure_cache",
        lambda: {
            "cached_at": 0,
            "models": {},
            "is_bootstrap": False,
        },
    )

    assert validate_model("gemini-3.5-flash")
    assert validate_model("models/gemini-3.5-flash")
    assert pricing.get_model_price("gemini-3.5-flash") == (1.50, 9.00)

    total_cost, breakdown = estimate_enrichment_cost(
        model="gemini-3.5-flash",
        prompt_template="Analyze: {content}",
        input_columns_sample={"content": "short sample"},
        schema={"summary": {"type": "string", "maxLength": 80}},
        num_rows=10,
        rows_to_process=10,
        execution_mode="batch",
    )

    assert total_cost > 0
    assert breakdown["pricing_source"] == "gemini_batch_discount"
    assert breakdown["input_price_per_1m"] == 0.75
    assert breakdown["output_price_per_1m"] == 4.5


def test_gemini_35_flash_uses_openrouter_pricing_cache(monkeypatch):
    """Gemini 3.5 Flash should use the live OpenRouter pricing shape when cached."""
    import doctrail.utils.model_pricing as pricing

    monkeypatch.setattr(
        pricing,
        "_ensure_cache",
        lambda: {
            "cached_at": 1,
            "models": {
                "google/gemini-3.5-flash": {
                    "input": 1.50,
                    "output": 9.00,
                    "name": "Google: Gemini 3.5 Flash",
                    "context_length": 1048576,
                },
            },
        },
    )

    assert pricing.get_model_price("gemini-3.5-flash") == (1.50, 9.00)
    assert pricing.get_model_price("openrouter/google/gemini-3.5-flash") == (1.50, 9.00)


def test_openrouter_pricing_parser_converts_gemini_35_flash(monkeypatch, tmp_path):
    """OpenRouter JSON prices should be converted from per-token to per-1M-token."""
    import doctrail.utils.model_pricing as pricing

    payload = {
        "data": [
            {
                "id": "google/gemini-3.5-flash",
                "name": "Google: Gemini 3.5 Flash",
                "context_length": 1048576,
                "pricing": {
                    "prompt": "0.0000015",
                    "completion": "0.000009",
                },
            },
        ],
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(payload).encode()

    def fake_urlopen(request, timeout):
        assert request.full_url == pricing.OPENROUTER_MODELS_URL
        assert timeout == 15
        return FakeResponse()

    monkeypatch.setattr(pricing.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(pricing, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(pricing, "CACHE_FILE", tmp_path / "model_pricing.json")

    data = pricing._fetch_from_openrouter()

    assert data is not None
    entry = data["models"]["google/gemini-3.5-flash"]
    assert entry["input"] == 1.50
    assert entry["output"] == 9.00
    assert entry["context_length"] == 1048576


def test_format_cost_estimate():
    """Test cost estimate formatting."""
    breakdown = {
        "model": "gpt-4o-mini",
        "total_rows_in_query": 100,
        "rows_to_process": 50,
        "rows_already_processed": 50,
        "input_tokens_per_row": 100,
        "output_tokens_per_row": 50,
        "total_input_tokens": 5000,
        "total_output_tokens": 2500,
        "total_tokens": 7500,
        "input_price_per_1m": 0.15,
        "output_price_per_1m": 0.60,
        "input_cost": 0.00075,
        "output_cost": 0.0015,
        "total_cost": 0.00225
    }
    
    formatted = format_cost_estimate(breakdown)
    assert "cost estimate" in formatted.lower()
    assert "gpt-4o-mini" in formatted
    assert "100" in formatted  # total rows
    assert "50" in formatted   # rows to process
    assert "$0.0022" in formatted  # total cost


def test_should_confirm_cost():
    """Test cost confirmation threshold."""
    # Should not confirm for low costs
    assert not should_confirm_cost(1.0, threshold=5.0)
    assert not should_confirm_cost(4.99, threshold=5.0)
    
    # Should confirm for high costs
    assert should_confirm_cost(5.01, threshold=5.0)
    assert should_confirm_cost(10.0, threshold=5.0)
    
    # Test custom thresholds
    assert should_confirm_cost(0.5, threshold=0.1)
    assert not should_confirm_cost(0.5, threshold=1.0)


def test_model_pricing():
    """Test that pricing data is available for common models."""
    from doctrail.utils.model_pricing import get_model_price

    # Check some common models have pricing
    input_p, output_p = get_model_price("gpt-4o")
    assert input_p > 0
    assert output_p > 0

    input_p, output_p = get_model_price("gpt-4o-mini")
    assert input_p > 0
    assert output_p > 0

    input_p, output_p = get_model_price("claude-haiku-4.5")
    assert input_p > 0
    assert output_p > 0


def test_refresh_openai_batch_catalog_discovers_models_from_docs(monkeypatch, tmp_path):
    """OpenAI batch refresh should discover model pages from the official docs index."""
    import doctrail.utils.model_pricing as pricing

    class FakeResponse:
        def __init__(self, text):
            self._text = text.encode("utf-8")

        def read(self):
            return self._text

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    all_models_html = """
    <a href="/api/docs/models/gpt-5-mini">gpt-5-mini</a>
    <a href="/api/docs/models/gpt-4.5-preview">gpt-4.5-preview</a>
    <a href="/api/docs/models/gpt-4-turbo-preview">gpt-4-turbo-preview</a>
    <a href="/api/docs/models/gpt-audio">gpt-audio</a>
    """
    gpt_5_mini_html = """
    <h2>Endpoints</h2>
    <div>v1/chat/completions</div>
    <div>v1/batch</div>
    <h2>Pricing</h2>
    <div>Batch API price Input $0.25 Cached input $0.025 Output $2.00</div>
    <h2>Snapshots</h2>
    <div>gpt-5-mini -> gpt-5-mini-2025-08-07</div>
    <h2>Rate limits</h2>
    """
    gpt_45_preview_html = """
    <h2>Endpoints</h2>
    <div>v1/chat/completions</div>
    <div>v1/batch</div>
    <h2>Pricing</h2>
    <div>Batch API price Input $37.50 Cached input $18.75 Output $75.00</div>
    <h2>Snapshots</h2>
    <div>gpt-4.5-preview -> gpt-4.5-preview-2025-02-27</div>
    <h2>Rate limits</h2>
    """
    gpt_4_turbo_preview_html = """
    <h2>Endpoints</h2>
    <div>v1/chat/completions</div>
    <div>v1/batch</div>
    <h2>Pricing</h2>
    <div>Batch API price Input $5.00 Cached input $2.50 Output $15.00</div>
    <h2>Snapshots</h2>
    <div>gpt-4-turbo-preview -> gpt-4-0125-preview</div>
    <div>gpt-4-1106-vision-preview</div>
    <h2>Rate limits</h2>
    """
    gpt_audio_html = """
    <h2>Endpoints</h2>
    <div>v1/responses</div>
    <h2>Pricing</h2>
    <div>Input $1 Output $2</div>
    """

    payloads = {
        pricing.OPENAI_MODELS_INDEX_URL: all_models_html,
        pricing.OPENAI_BATCH_DOC_URL.format(model="gpt-5-mini"): gpt_5_mini_html,
        pricing.OPENAI_BATCH_DOC_URL.format(model="gpt-4.5-preview"): gpt_45_preview_html,
        pricing.OPENAI_BATCH_DOC_URL.format(model="gpt-4-turbo-preview"): gpt_4_turbo_preview_html,
        pricing.OPENAI_BATCH_DOC_URL.format(model="gpt-audio"): gpt_audio_html,
    }

    def fake_urlopen(request, timeout=20):
        url = request.full_url if hasattr(request, "full_url") else request
        if url not in payloads:
            raise AssertionError(f"unexpected url {url}")
        return FakeResponse(payloads[url])

    monkeypatch.setattr(pricing.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(pricing, "OPENAI_BATCH_CACHE_FILE", tmp_path / "openai_batch_catalog.json")
    monkeypatch.setattr(pricing, "_openai_batch_cache", None)

    count = pricing.refresh_openai_batch_catalog()
    models = pricing.get_openai_batch_models()

    assert count == 3
    assert set(models) == {"gpt-4.5-preview", "gpt-4-turbo-preview", "gpt-5-mini"}
    assert models["gpt-5-mini"]["batch_input"] == 0.25
    assert models["gpt-5-mini"]["batch_cached_input"] == 0.025
    assert models["gpt-5-mini"]["batch_output"] == 2.0
    assert models["gpt-5-mini"]["snapshots"] == ["gpt-5-mini-2025-08-07"]
    assert models["gpt-4.5-preview"]["snapshots"] == ["gpt-4.5-preview-2025-02-27"]
    assert models["gpt-4-turbo-preview"]["snapshots"] == [
        "gpt-4-0125-preview",
        "gpt-4-1106-vision-preview",
    ]
    assert pricing.get_openai_batch_model_info("gpt-4.5-preview-2025-02-27") == models["gpt-4.5-preview"]
    assert pricing.get_openai_batch_model_info("gpt-audio") is None


def test_encoding_selection():
    """Test model to encoding mapping."""
    # o200k_base models
    assert get_encoding_for_model("gpt-4o") == "o200k_base"
    assert get_encoding_for_model("gpt-4o-mini") == "o200k_base"
    assert get_encoding_for_model("gpt-4o-2024-11-20") == "o200k_base"
    
    # cl100k_base models
    assert get_encoding_for_model("gpt-4") == "cl100k_base"
    assert get_encoding_for_model("gpt-3.5-turbo") == "cl100k_base"
    
    # Unknown model should default to o200k_base
    assert get_encoding_for_model("unknown-model") == "o200k_base"


def test_validate_model_rejects_wrong_openai_namespace():
    """Direct OpenAI models must use bare IDs, not openai/ prefixes."""
    error = get_model_validation_error("openai/gpt-5-mini")
    assert error is not None
    assert "gpt-5-mini" in error
    assert "openrouter/openai/gpt-5-mini" in error
    assert not validate_model("openai/gpt-5-mini")


def test_validate_model_accepts_known_openai_model():
    """Known direct OpenAI model IDs should validate without hitting the endpoint."""
    assert validate_model("gpt-5-mini")


def test_validate_model_accepts_openai_compatible_model_for_sync_only():
    model = "openai-compatible/Qwen/Qwen3-32B"

    assert validate_model(model)
    assert get_model_validation_error(model, execution_mode="batch") == (
        "OpenAI-compatible endpoints currently support execution_mode=sync only."
    )


def test_validate_model_rejects_empty_openai_compatible_model():
    assert get_model_validation_error("openai-compatible/") == (
        "An openai-compatible model ID is required after 'openai-compatible/'."
    )


def test_validate_model_accepts_openai_batch_snapshot(monkeypatch):
    """OpenAI batch validation should accept documented snapshot IDs."""
    import doctrail.utils.cost_estimation as cost_utils

    snapshot = "gpt-4.1-2025-04-14"
    monkeypatch.setattr(
        cost_utils,
        "get_openai_batch_model_info",
        lambda model: {"snapshots": [snapshot]} if model == snapshot else None,
    )
    assert validate_model(snapshot, execution_mode="batch")


def test_validate_model_rejects_undocumented_openai_batch_snapshot(monkeypatch):
    """OpenAI batch validation should reject snapshot ids that are not documented."""
    import doctrail.utils.cost_estimation as cost_utils

    monkeypatch.setattr(cost_utils, "get_openai_batch_model_info", lambda model: None)
    monkeypatch.setattr(
        cost_utils,
        "get_openai_batch_models",
        lambda: {"gpt-4.5-preview": {"snapshots": ["gpt-4.5-preview-2025-02-27"]}},
    )
    monkeypatch.setattr(cost_utils, "_get_provider_models", lambda provider: {"gpt-4.5-preview"} if provider == "openai" else set())

    error = get_model_validation_error("gpt-4.5-preview-2099-01-01", execution_mode="openai-batch")
    assert error is not None
    assert "Unknown OpenAI batch model" in error
    assert not validate_model("gpt-4.5-preview-2099-01-01", execution_mode="openai-batch")


def test_validate_model_rejects_openai_model_outside_batch_catalog(monkeypatch):
    """OpenAI batch validation should reject models outside the verified batch catalog."""
    import doctrail.utils.cost_estimation as cost_utils

    monkeypatch.setattr(cost_utils, "get_openai_batch_model_info", lambda model: None)
    monkeypatch.setattr(cost_utils, "get_openai_batch_models", lambda: {"gpt-5-mini": {"snapshots": []}})
    monkeypatch.setattr(cost_utils, "_get_provider_models", lambda provider: {"gpt-5-mini", "gpt-5-codex"} if provider == "openai" else set())

    error = get_model_validation_error("gpt-5-codex", execution_mode="openai-batch")
    assert error is not None
    assert "verified OpenAI batch catalog" in error
    assert not validate_model("gpt-5-codex", execution_mode="openai-batch")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
