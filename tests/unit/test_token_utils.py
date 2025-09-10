"""Unit tests for token utilities."""

import pytest
from src.llm.token_utils import (
    estimate_tokens, truncate_input_for_model, get_model_context_limit,
    calculate_available_tokens
)


class TestEstimateTokens:
    """Test token estimation."""
    
    def test_empty_text(self):
        """Test empty text returns 0 tokens."""
        assert estimate_tokens("") == 0
    
    def test_short_text(self):
        """Test short text estimation."""
        text = "Hello world"  # 11 chars
        assert estimate_tokens(text) == 2  # 11 // 4 = 2
    
    def test_long_text(self):
        """Test longer text estimation."""
        text = "a" * 400  # 400 chars
        assert estimate_tokens(text) == 100  # 400 // 4 = 100


class TestTruncateInputForModel:
    """Test input truncation for model context limits."""
    
    def test_no_truncation_needed(self):
        """Test when text fits within context."""
        prompt = "Analyze this: small text"
        input_text = "small text"
        result, truncated = truncate_input_for_model(prompt, input_text, "gpt-4o")
        assert result == prompt
        assert truncated is False
    
    def test_truncation_needed(self):
        """Test when text exceeds context."""
        input_text = "a" * 50000  # Very long text
        prompt = f"Analyze this: {input_text}"
        result, truncated = truncate_input_for_model(prompt, input_text, "gpt-4", safety_margin=1000)
        assert truncated is True
        assert "... [TRUNCATED]" in result
        assert len(result) < len(prompt)
    
    def test_unknown_model_default(self):
        """Test unknown model uses default context limit."""
        prompt = "Test prompt"
        input_text = "Test"
        result, truncated = truncate_input_for_model(prompt, input_text, "unknown-model")
        assert result == prompt
        assert truncated is False


class TestGetModelContextLimit:
    """Test getting model context limits."""
    
    def test_known_models(self):
        """Test known model limits."""
        assert get_model_context_limit("gpt-4") == 8192
        assert get_model_context_limit("gpt-4o") == 128000
        assert get_model_context_limit("gemini-2.0-flash") == 1000000
    
    def test_unknown_model(self):
        """Test unknown model returns default."""
        assert get_model_context_limit("unknown-model") == 8192


class TestCalculateAvailableTokens:
    """Test available token calculation."""
    
    def test_normal_calculation(self):
        """Test normal token calculation."""
        available = calculate_available_tokens("gpt-4", prompt_tokens=1000)
        # 8192 - 1000 - 2000 = 5192
        assert available == 5192
    
    def test_no_tokens_available(self):
        """Test when prompt exceeds limit."""
        available = calculate_available_tokens("gpt-4", prompt_tokens=10000)
        assert available == 0
    
    def test_custom_safety_margin(self):
        """Test with custom safety margin."""
        available = calculate_available_tokens("gpt-4", prompt_tokens=1000, safety_margin=500)
        # 8192 - 1000 - 500 = 6692
        assert available == 6692