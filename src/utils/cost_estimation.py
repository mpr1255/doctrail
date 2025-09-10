"""
Cost estimation for LLM API calls in doctrail.
"""

import json
import logging
from typing import Dict, Tuple, Optional, List
import tiktoken

# Model pricing per 1M tokens (as of the user's provided data)
MODEL_PRICING = {
    # Model: (input_price, cached_input_price, output_price)
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-4.1-2025-04-14": (2.00, 0.50, 8.00),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1-mini-2025-04-14": (0.40, 0.10, 1.60),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
    "gpt-4.1-nano-2025-04-14": (0.10, 0.025, 0.40),
    "gpt-4.5-preview": (75.00, 37.50, 150.00),
    "gpt-4.5-preview-2025-02-27": (75.00, 37.50, 150.00),
    "gpt-4o": (2.50, 1.25, 10.00),
    "gpt-4o-2024-08-06": (2.50, 1.25, 10.00),
    "gpt-4o-2024-11-20": (2.50, 1.25, 10.00),
    "gpt-4o-2024-05-13": (5.00, None, 15.00),
    "gpt-4o-audio-preview": (2.50, None, 10.00),
    "gpt-4o-audio-preview-2024-12-17": (2.50, None, 10.00),
    "gpt-4o-audio-preview-2025-06-03": (2.50, None, 10.00),
    "gpt-4o-audio-preview-2024-10-01": (2.50, None, 10.00),
    "gpt-4o-realtime-preview": (5.00, 2.50, 20.00),
    "gpt-4o-realtime-preview-2024-12-17": (5.00, 2.50, 20.00),
    "gpt-4o-realtime-preview-2025-06-03": (5.00, 2.50, 20.00),
    "gpt-4o-realtime-preview-2024-10-01": (5.00, 2.50, 20.00),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-4o-mini-2024-07-18": (0.15, 0.075, 0.60),
    "gpt-4o-mini-audio-preview": (0.15, None, 0.60),
    "gpt-4o-mini-audio-preview-2024-12-17": (0.15, None, 0.60),
    "gpt-4o-mini-realtime-preview": (0.60, 0.30, 2.40),
    "gpt-4o-mini-realtime-preview-2024-12-17": (0.60, 0.30, 2.40),
    "o1": (15.00, 7.50, 60.00),
    "o1-2024-12-17": (15.00, 7.50, 60.00),
    "o1-preview-2024-09-12": (15.00, 7.50, 60.00),
    "o1-pro": (150.00, None, 600.00),
    "o1-pro-2025-03-19": (150.00, None, 600.00),
    "o3-pro": (20.00, None, 80.00),
    "o3-pro-2025-06-10": (20.00, None, 80.00),
    "o3": (2.00, 0.50, 8.00),
    "o3-2025-04-16": (2.00, 0.50, 8.00),
    "o4-mini": (1.10, 0.275, 4.40),
    "o4-mini-2025-04-16": (1.10, 0.275, 4.40),
    "o3-mini": (1.10, 0.55, 4.40),
    "o3-mini-2025-01-31": (1.10, 0.55, 4.40),
    "o1-mini": (1.10, 0.55, 4.40),
    "o1-mini-2024-09-12": (1.10, 0.55, 4.40),
    "codex-mini-latest": (1.50, 0.375, 6.00),
    "gpt-4o-mini-search-preview": (0.15, None, 0.60),
    "gpt-4o-mini-search-preview-2025-03-11": (0.15, None, 0.60),
    "gpt-4o-search-preview": (2.50, None, 10.00),
    "gpt-4o-search-preview-2025-03-11": (2.50, None, 10.00),
    "computer-use-preview": (3.00, None, 12.00),
    "computer-use-preview-2025-03-11": (3.00, None, 12.00),
    # Flex processing (o3 models)
    "o3-flex": (1.00, 0.25, 4.00),
    "o4-mini-flex": (0.55, 0.138, 2.20),
    # Gemini models (approximated)
    "gemini-2.0-flash-exp": (0.15, None, 0.60),
    "gemini-1.5-flash": (0.15, None, 0.60),
    "gemini-1.5-pro": (2.50, None, 10.00),
    # Legacy GPT models (for backward compatibility)
    "gpt-3.5-turbo": (0.50, None, 1.50),
    "gpt-3.5-turbo-0125": (0.50, None, 1.50),
    "gpt-3.5-turbo-1106": (1.00, None, 2.00),
    "gpt-4": (30.00, None, 60.00),
    "gpt-4-0613": (30.00, None, 60.00),
    "gpt-4-turbo": (10.00, None, 30.00),
    "gpt-4-turbo-preview": (10.00, None, 30.00),
}

# Model to encoding mapping
MODEL_ENCODINGS = {
    # GPT-4.x models likely use o200k_base
    "gpt-4.1": "o200k_base",
    "gpt-4.1-mini": "o200k_base",
    "gpt-4.1-nano": "o200k_base",
    "gpt-4.5-preview": "o200k_base",
    # GPT-4o models use o200k_base
    "gpt-4o": "o200k_base",
    "gpt-4o-mini": "o200k_base",
    # O-series models likely use o200k_base
    "o1": "o200k_base",
    "o3": "o200k_base",
    "o3-mini": "o200k_base",
    "o4-mini": "o200k_base",
    # Legacy models
    "gpt-4": "cl100k_base",
    "gpt-4-turbo": "cl100k_base",
    "gpt-4-turbo-preview": "cl100k_base",
    "gpt-3.5-turbo": "cl100k_base",
    "gpt-3.5-turbo-0125": "cl100k_base",
    "gpt-3.5-turbo-1106": "cl100k_base",
}


def get_encoding_for_model(model: str) -> str:
    """Get the encoding name for a model."""
    # Strip version suffixes for lookup
    base_model = model.split("-20")[0]
    
    # Check direct mapping first
    if model in MODEL_ENCODINGS:
        return MODEL_ENCODINGS[model]
    elif base_model in MODEL_ENCODINGS:
        return MODEL_ENCODINGS[base_model]
    else:
        # Default to o200k_base for newer models
        logging.warning(f"Unknown model '{model}', defaulting to o200k_base encoding")
        return "o200k_base"


def count_tokens(text: str, model: str) -> int:
    """Count tokens in text for a specific model."""
    try:
        encoding_name = get_encoding_for_model(model)
        encoding = tiktoken.get_encoding(encoding_name)
        return len(encoding.encode(text))
    except Exception as e:
        logging.warning(f"Error counting tokens: {e}. Using rough estimate.")
        # Rough estimate: ~4 characters per token
        return len(text) // 4


def estimate_output_tokens(schema: Dict, num_rows: int) -> int:
    """Estimate output tokens based on schema complexity."""
    # Base tokens per response (JSON structure overhead)
    base_tokens = 50
    
    # Estimate tokens per field
    field_tokens = 0
    for field_name, field_def in schema.items():
        if isinstance(field_def, dict):
            field_type = field_def.get('type', 'string')
            if field_type == 'string':
                max_length = field_def.get('maxLength', 100)
                # Assume average fill of 50% of max length, ~4 chars per token
                field_tokens += max_length // 8
            elif field_type == 'array':
                max_items = field_def.get('maxItems', 5)
                # Assume 10 tokens per array item on average
                field_tokens += max_items * 10
            else:
                # Numbers, booleans, enums: ~5 tokens
                field_tokens += 5
        else:
            # Simple types
            field_tokens += 5
    
    # Total estimate per row
    tokens_per_row = base_tokens + field_tokens
    
    return tokens_per_row * num_rows


def estimate_enrichment_cost(
    model: str,
    prompt_template: str,
    input_columns_sample: Dict[str, str],
    schema: Dict,
    num_rows: int,
    rows_to_process: int
) -> Tuple[float, Dict[str, any]]:
    """
    Estimate the cost of an enrichment task.
    
    Returns:
        (total_cost, cost_breakdown)
    """
    # Get pricing for model
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        # Try base model name
        base_model = model.split("-20")[0]
        pricing = MODEL_PRICING.get(base_model)
    
    if not pricing:
        logging.warning(f"No pricing data for model '{model}', using gpt-4o pricing as estimate")
        pricing = MODEL_PRICING.get("gpt-4o")
    
    input_price, _, output_price = pricing
    
    # Estimate input tokens
    # Build a sample prompt with the template and sample data
    sample_prompt = prompt_template
    for col_name, col_value in input_columns_sample.items():
        sample_prompt = sample_prompt.replace(f"{{{col_name}}}", str(col_value))
    
    # Add system prompt overhead (structured output instructions)
    system_overhead = 200  # Approximate tokens for system instructions
    
    # Count tokens in sample prompt
    input_tokens_per_row = count_tokens(sample_prompt, model) + system_overhead
    total_input_tokens = input_tokens_per_row * rows_to_process
    
    # Estimate output tokens
    total_output_tokens = estimate_output_tokens(schema, rows_to_process)
    
    # Calculate costs (prices are per 1M tokens)
    input_cost = (total_input_tokens / 1_000_000) * input_price
    output_cost = (total_output_tokens / 1_000_000) * output_price
    total_cost = input_cost + output_cost
    
    breakdown = {
        "model": model,
        "total_rows_in_query": num_rows,
        "rows_to_process": rows_to_process,
        "rows_already_processed": num_rows - rows_to_process,
        "input_tokens_per_row": input_tokens_per_row,
        "output_tokens_per_row": total_output_tokens // rows_to_process if rows_to_process > 0 else 0,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_input_tokens + total_output_tokens,
        "input_price_per_1m": input_price,
        "output_price_per_1m": output_price,
        "input_cost": input_cost,
        "output_cost": output_cost,
        "total_cost": total_cost
    }
    
    return total_cost, breakdown


def format_cost_estimate(breakdown: Dict[str, any]) -> str:
    """Format cost estimate for display."""
    lines = [
        f"\n💰 Cost Estimate for {breakdown['model']}:",
        f"   Total rows in query: {breakdown['total_rows_in_query']:,}",
        f"   Rows to process: {breakdown['rows_to_process']:,}",
        f"   Already processed: {breakdown['rows_already_processed']:,}",
        f"",
        f"   📊 Token estimates:",
        f"      Input: ~{breakdown['input_tokens_per_row']:,} tokens/row × {breakdown['rows_to_process']:,} rows = {breakdown['total_input_tokens']:,} tokens",
        f"      Output: ~{breakdown['output_tokens_per_row']:,} tokens/row × {breakdown['rows_to_process']:,} rows = {breakdown['total_output_tokens']:,} tokens",
        f"      Total: {breakdown['total_tokens']:,} tokens",
        f"",
        f"   💵 Cost breakdown:",
        f"      Input: ${breakdown['input_cost']:.4f} (${breakdown['input_price_per_1m']:.2f}/1M tokens)",
        f"      Output: ${breakdown['output_cost']:.4f} (${breakdown['output_price_per_1m']:.2f}/1M tokens)",
        f"      Total: ${breakdown['total_cost']:.4f}",
    ]
    
    if breakdown['total_cost'] > 1.00:
        lines.append(f"\n   ⚠️  Estimated cost: ${breakdown['total_cost']:.2f}")
    
    return "\n".join(lines)


def should_confirm_cost(cost: float, threshold: float = 5.0) -> bool:
    """Check if cost exceeds threshold and requires confirmation."""
    return cost > threshold


def validate_model(model: str) -> bool:
    """
    Validate that a model is supported by checking against our pricing table.
    
    Args:
        model: The model name to validate
        
    Returns:
        True if model is supported, False otherwise
    """
    return model in MODEL_PRICING


def get_supported_models() -> List[str]:
    """
    Get list of all models in our pricing table.
    
    Returns:
        List of all model names we have pricing data for
    """
    return list(MODEL_PRICING.keys())


def get_models_with_structured_output() -> List[str]:
    """
    Get list of models that support structured output (JSON schema).
    
    Returns:
        List of model names that support structured output
    """
    # Only these models support structured output
    structured_output_models = {
        "gpt-4o",
        "gpt-4o-2024-08-06",
        "gpt-4o-2024-11-20", 
        "gpt-4o-mini",
        "gpt-4o-mini-2024-07-18",
    }
    
    return [model for model in get_supported_models() if model in structured_output_models]