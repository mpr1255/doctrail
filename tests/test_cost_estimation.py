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
from pathlib import Path
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.cost_estimation import (
    estimate_enrichment_cost,
    format_cost_estimate,
    should_confirm_cost,
    count_tokens,
    estimate_output_tokens,
    get_encoding_for_model
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
    assert "Cost Estimate" in formatted
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
    from src.cost_estimation import MODEL_PRICING
    
    # Check some common models have pricing
    assert "gpt-4o" in MODEL_PRICING
    assert "gpt-4o-mini" in MODEL_PRICING
    assert "gpt-4.1" in MODEL_PRICING  # Updated model name
    assert "gemini-1.5-flash" in MODEL_PRICING
    
    # Check pricing structure
    pricing = MODEL_PRICING["gpt-4o-mini"]
    assert len(pricing) == 3  # input, cached_input, output
    assert pricing[0] > 0  # input price
    assert pricing[2] > 0  # output price


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])