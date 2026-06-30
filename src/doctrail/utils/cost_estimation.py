"""
Cost estimation for LLM API calls in doctrail.

Pricing is fetched dynamically from OpenRouter's API via model_pricing module.
"""

import json
import logging
import os
import difflib
from typing import Dict, Tuple, Optional, List
import tiktoken
from ..core_utils import load_doctrail_environment
from .model_pricing import (
    get_model_price,
    get_all_models,
    canonicalize_model_name,
    get_openai_batch_model_info,
    get_openai_batch_models,
)

_provider_model_cache: Dict[str, set[str]] = {}

KNOWN_OPENAI_MODELS = {
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5.2",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
    "o3",
    "o3-mini",
    "o4-mini",
}

KNOWN_GEMINI_MODELS = {
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-001",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash-lite-001",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-3.5-flash",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-pro-latest",
}

KNOWN_ANTHROPIC_MODELS = {
    "claude-opus-4-1",
    "claude-opus-4",
    "claude-opus-4-6",
    "claude-sonnet-4-5",
    "claude-sonnet-4",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-3-haiku",
    "claude-3-haiku-20240307",
}

KNOWN_CLI_CLAUDE_MODELS = {
    "sonnet",
    "opus",
    "haiku",
    *KNOWN_ANTHROPIC_MODELS,
}

# Model to encoding mapping
MODEL_ENCODINGS = {
    # GPT-4.x models likely use o200k_base
    "gpt-5": "o200k_base",
    "gpt-5-mini": "o200k_base",
    "gpt-5-nano": "o200k_base",
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
        # Default to o200k_base — close enough for cost estimation
        logging.debug(f"No tiktoken encoding for '{model}', using o200k_base")
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
    rows_to_process: int,
    execution_mode: str = "sync",
) -> Tuple[float, Dict[str, any]]:
    """
    Estimate the cost of an enrichment task.
    
    Returns:
        (total_cost, cost_breakdown)
    """
    pricing_source = "openrouter_pricing"
    batch_catalog_entry = None
    normalized_model = canonicalize_model_name(model)

    if normalized_model == "replay" or normalized_model.startswith("replay/"):
        input_price = 0.0
        output_price = 0.0
        pricing_source = "replay_fixture"
    elif normalized_model.startswith("cli/"):
        input_price = 0.0
        output_price = 0.0
        pricing_source = "cli_subscription"
    elif execution_mode in {"batch", "openai-batch"}:
        if normalized_model.startswith("anthropic/") or normalized_model.startswith("claude"):
            input_price, output_price = get_model_price(model)
            input_price *= 0.5
            output_price *= 0.5
            pricing_source = "anthropic_batch_discount"
        elif "gemini" in normalized_model.lower() or normalized_model.startswith("models/"):
            input_price, output_price = get_model_price(model)
            input_price *= 0.5
            output_price *= 0.5
            pricing_source = "gemini_batch_discount"
        elif "/" not in normalized_model:
            batch_catalog_entry = get_openai_batch_model_info(model)
            if batch_catalog_entry:
                input_price = batch_catalog_entry["batch_input"]
                output_price = batch_catalog_entry["batch_output"]
                pricing_source = "openai_batch_catalog"
            else:
                input_price, output_price = get_model_price(model)
        else:
            input_price, output_price = get_model_price(model)
    else:
        input_price, output_price = get_model_price(model)

    if input_price == 0.0 and output_price == 0.0 and pricing_source not in {"cli_subscription", "replay_fixture"}:
        logging.warning(f"No pricing data for model '{model}', using gpt-4o pricing as estimate")
        input_price, output_price = get_model_price("gpt-4o")
        pricing_source = "fallback_gpt4o"
    
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
        "total_cost": total_cost,
        "execution_mode": execution_mode,
        "pricing_source": pricing_source,
        "batch_cached_input_price_per_1m": (
            batch_catalog_entry.get("batch_cached_input") if batch_catalog_entry else None
        ),
    }
    
    return total_cost, breakdown


def format_cost_estimate(breakdown: Dict[str, any]) -> str:
    """Format cost estimate for display."""
    lines = [
        f"\nCost estimate for {breakdown['model']}:",
        f"   Total rows in query: {breakdown['total_rows_in_query']:,}",
        f"   Rows to process: {breakdown['rows_to_process']:,}",
        f"   Already processed: {breakdown['rows_already_processed']:,}",
        f"",
        f"   Token estimates:",
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
        lines.append(f"\n   Estimated cost: ${breakdown['total_cost']:.2f}")
    
    return "\n".join(lines)


def should_confirm_cost(cost: float, threshold: float = 5.0) -> bool:
    """Check if cost exceeds threshold and requires confirmation."""
    return cost > threshold


def _suggest_model(model: str, candidates: set[str]) -> Optional[str]:
    matches = difflib.get_close_matches(model, sorted(candidates), n=1, cutoff=0.5)
    return matches[0] if matches else None


def _fetch_openai_models() -> set[str]:
    load_doctrail_environment()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return set()

    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    return {model.id for model in client.models.list().data}


def _fetch_gemini_models() -> set[str]:
    load_doctrail_environment()
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return set()

    from google import genai

    client = genai.Client(api_key=api_key)
    names = set()
    for model in client.models.list():
        if model.name.startswith("models/"):
            names.add(model.name.removeprefix("models/"))
        else:
            names.add(model.name)
    return names


def _fetch_anthropic_models() -> set[str]:
    load_doctrail_environment()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return set()

    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    return {model.id for model in client.models.list(limit=100)}


def _get_provider_models(provider: str) -> set[str]:
    cached = _provider_model_cache.get(provider)
    if cached is not None:
        return cached

    try:
        if provider == "openai":
            models = _fetch_openai_models()
        elif provider == "gemini":
            models = _fetch_gemini_models()
        elif provider == "anthropic":
            models = _fetch_anthropic_models()
        elif provider == "openrouter":
            models = set(get_all_models().keys())
        else:
            models = set()
    except Exception as exc:
        logging.warning("Could not fetch %s model list for validation: %s", provider, exc)
        models = set()

    _provider_model_cache[provider] = models
    return models


def _validate_openai_model(model: str, execution_mode: str = "sync") -> Optional[str]:
    if "/" in model:
        if model.startswith("openai/"):
            suggested = model.removeprefix("openai/")
            return (
                f"Invalid OpenAI model '{model}'. "
                f"Use '{suggested}' for direct OpenAI, or 'openrouter/{model}' for OpenRouter."
            )
        return (
            f"Invalid direct OpenAI model '{model}'. "
            "Direct OpenAI model IDs must be bare names like 'gpt-5-mini'. "
            "Provider-prefixed IDs belong under 'openrouter/...'."
        )

    if execution_mode in {"batch", "openai-batch"}:
        batch_entry = get_openai_batch_model_info(model)
        if batch_entry:
            return None

        batch_models = set(get_openai_batch_models().keys())
        for entry in get_openai_batch_models().values():
            batch_models.update(entry.get("snapshots", []))

        live_models = _get_provider_models("openai")
        suggestion = _suggest_model(model, batch_models or live_models)

        if model in live_models:
            message = (
                f"OpenAI model '{model}' is not in Doctrail's verified OpenAI batch catalog. "
                "Run 'doctrail models --openai-batch --refresh' to refresh the catalog."
            )
        else:
            message = (
                f"Unknown OpenAI batch model '{model}'. "
                "Run 'doctrail models --openai-batch --refresh' to inspect the current catalog."
            )

        if suggestion:
            message += f" Did you mean '{suggestion}'?"
        return message

    if model in KNOWN_OPENAI_MODELS:
        return None

    live_models = _get_provider_models("openai")
    if model in live_models:
        return None

    candidates = KNOWN_OPENAI_MODELS | live_models
    suggestion = _suggest_model(model, candidates)
    message = f"Unknown OpenAI model '{model}'."
    if suggestion:
        message += f" Did you mean '{suggestion}'?"
    return message


def _validate_gemini_model(model: str) -> Optional[str]:
    if model.startswith("google/"):
        suggested = model.removeprefix("google/").removeprefix("models/")
        return (
            f"Invalid Gemini model '{model}'. "
            f"Use '{suggested}' for direct Gemini, or 'openrouter/{model}' for OpenRouter."
        )

    normalized = model.removeprefix("models/")
    if normalized in KNOWN_GEMINI_MODELS:
        return None

    live_models = _get_provider_models("gemini")
    if normalized in live_models:
        return None

    candidates = KNOWN_GEMINI_MODELS | live_models
    suggestion = _suggest_model(normalized, candidates)
    message = f"Unknown Gemini model '{model}'."
    if suggestion:
        message += f" Did you mean '{suggestion}'?"
    return message


def _validate_anthropic_model(model: str) -> Optional[str]:
    normalized = canonicalize_model_name(model).removeprefix("anthropic/")

    if normalized in KNOWN_ANTHROPIC_MODELS:
        return None

    live_models = _get_provider_models("anthropic")
    if normalized in live_models:
        return None

    candidates = KNOWN_ANTHROPIC_MODELS | live_models
    suggestion = _suggest_model(normalized, candidates)
    message = f"Unknown Anthropic model '{model}'."
    if suggestion:
        message += f" Did you mean '{suggestion}'?"
    return message


def _validate_openrouter_model(model: str) -> Optional[str]:
    actual_model = model.removeprefix("openrouter/")
    live_models = _get_provider_models("openrouter")
    if actual_model in live_models:
        return None

    suggestion = _suggest_model(actual_model, live_models)
    message = f"Unknown OpenRouter model '{model}'."
    if suggestion:
        message += f" Did you mean 'openrouter/{suggestion}'?"
    return message


def _validate_cli_model(model: str) -> Optional[str]:
    parts = model.removeprefix("cli/").split("/", 1)
    cli_tool = parts[0]
    cli_model = parts[1] if len(parts) > 1 else ""

    if cli_tool == "claude":
        if not cli_model:
            return None
        normalized = canonicalize_model_name(f"cli/claude/{cli_model}").removeprefix("cli/claude/")
        if normalized in KNOWN_CLI_CLAUDE_MODELS or normalized in _get_provider_models("anthropic"):
            return None
        suggestion = _suggest_model(
            normalized,
            KNOWN_CLI_CLAUDE_MODELS | _get_provider_models("anthropic"),
        )
        message = f"Unknown cli/claude model '{model}'."
        if suggestion:
            message += f" Did you mean 'cli/claude/{suggestion}'?"
        return message

    if cli_tool == "gemini":
        return None if not cli_model else _validate_gemini_model(cli_model)

    if cli_tool == "codex":
        return None if not cli_model else _validate_openai_model(cli_model)

    return f"Unknown CLI tool '{cli_tool}' in model '{model}'."


def get_model_validation_error(model: str, execution_mode: str = "sync") -> Optional[str]:
    """Return a human-readable validation error, or None if the model is valid."""
    normalized = canonicalize_model_name(model)

    if normalized == "replay" or normalized.startswith("replay/"):
        return None

    if normalized.startswith("cli/"):
        return _validate_cli_model(normalized)

    if normalized.startswith("openrouter/"):
        return _validate_openrouter_model(normalized)

    if normalized.startswith("anthropic/") or normalized.startswith("claude"):
        return _validate_anthropic_model(normalized)

    if normalized.startswith("openai/"):
        return _validate_openai_model(normalized, execution_mode=execution_mode)

    if normalized.startswith("google/"):
        return _validate_gemini_model(normalized)

    if "gemini" in normalized.lower() or normalized.startswith("models/"):
        return _validate_gemini_model(normalized)

    return _validate_openai_model(normalized, execution_mode=execution_mode)


def validate_model(model: str, execution_mode: str = "sync") -> bool:
    """Return True only for provider-valid model identifiers."""
    error = get_model_validation_error(model, execution_mode=execution_mode)
    if error is None:
        return True

    logging.warning(error)
    return False


def get_provider_models(provider: str) -> set[str]:
    """Return the provider model set used by doctrail validation."""
    return set(_get_provider_models(provider))


def get_supported_models() -> List[str]:
    """
    Get list of all models we have pricing data for.

    Returns:
        List of all model IDs from the pricing cache
    """
    from .model_pricing import get_all_models
    return list(get_all_models().keys())
