"""Token estimation and truncation utilities."""

import logging
from typing import Tuple, Optional

# Model context limits (in tokens)
MODEL_CONTEXT_LIMITS = {
    'gpt-4o-mini': 128000,
    'gpt-4o': 128000,
    'gpt-4': 8192,
    'gpt-4-32k': 32768,
    'gpt-3.5-turbo': 16384,
    'gpt-3.5-turbo-16k': 16384,
    'gemini-2.5-flash-preview-05-20': 1000000,
    'models/gemini-2.5-flash-preview-05-20': 1000000,
    'gemini-2.5-flash': 1000000,
    'models/gemini-2.5-flash': 1000000,
    'gemini-2.0-flash': 1000000,
    'models/gemini-2.0-flash': 1000000,
}


def estimate_tokens(text: str) -> int:
    """Rough token estimation (1 token ≈ 4 characters)."""
    return len(text) // 4


def truncate_input_for_model(
    full_prompt: str, 
    input_text: str, 
    model: str, 
    safety_margin: int = 2000
) -> Tuple[str, bool]:
    """
    Truncate input text to fit within model's context window.
    
    Args:
        full_prompt: The complete prompt including the input text
        input_text: The input text portion that can be truncated
        model: The model name
        safety_margin: Tokens to reserve for response and safety
        
    Returns:
        Tuple of (truncated prompt, was_truncated boolean)
    """
    # Get model's context limit
    context_limit = MODEL_CONTEXT_LIMITS.get(model, 8192)  # Default to 8k if unknown
    
    # Estimate tokens in full prompt
    estimated_tokens = estimate_tokens(full_prompt)
    
    # Check if we're within limits
    if estimated_tokens <= (context_limit - safety_margin):
        return full_prompt, False
    
    # Calculate how much we need to truncate
    # First, find the base prompt size (without input text)
    base_prompt = full_prompt.replace(input_text, "")
    base_tokens = estimate_tokens(base_prompt)
    
    # Calculate available tokens for input text
    available_for_input = context_limit - safety_margin - base_tokens
    
    if available_for_input <= 0:
        # Even without input text, we exceed the limit
        logging.warning(f"Prompt without input text exceeds model limit for {model}")
        return full_prompt, True
    
    # Calculate how many characters we can keep (4 chars per token estimate)
    max_input_chars = available_for_input * 4
    
    # Truncate the input text
    if len(input_text) > max_input_chars:
        truncated_input = input_text[:max_input_chars] + "... [TRUNCATED]"
        truncated_prompt = full_prompt.replace(input_text, truncated_input)
        
        logging.info(f"Truncated input from {len(input_text)} to {len(truncated_input)} characters for {model}")
        return truncated_prompt, True
    
    return full_prompt, False


def get_model_context_limit(model: str) -> int:
    """Get the context limit for a model.
    
    Args:
        model: Model name
        
    Returns:
        Context limit in tokens
    """
    return MODEL_CONTEXT_LIMITS.get(model, 8192)


def calculate_available_tokens(
    model: str,
    prompt_tokens: int,
    safety_margin: int = 2000
) -> int:
    """Calculate available tokens for output.
    
    Args:
        model: Model name
        prompt_tokens: Number of tokens in prompt
        safety_margin: Safety margin to reserve
        
    Returns:
        Available tokens for output
    """
    context_limit = get_model_context_limit(model)
    available = context_limit - prompt_tokens - safety_margin
    return max(0, available)