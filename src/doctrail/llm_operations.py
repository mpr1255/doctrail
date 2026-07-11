import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple, Type, get_args, get_origin

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, create_model
from tqdm import tqdm

from .constants import (
    DEFAULT_API_SEMAPHORE_LIMIT, DEFAULT_DB_SEMAPHORE_LIMIT,
    TRANSLATION_ENRICHMENTS, MAX_RETRY_ATTEMPTS, DEFAULT_KEY_COLUMN
)
from .db_operations import (
    ensure_enrichment_audit_table, checkpoint_wal, get_or_create_prompt_id,
    ensure_enrichments_table, plan_existing_enrichment_skips,
    filter_unskipped_input_rows,
    get_successfully_enriched_keys,
    EnrichmentRunWriter,
    persist_enrichment_result,
)
from .schema_managers import validate_with_schema, get_schema_prompt_instructions, SchemaValidationError, LanguageValidationError
from .core_utils import (
    parse_input_columns_with_limits,
    apply_column_limits,
    detect_mojibake,
    try_fix_mojibake,
    load_doctrail_environment,
    resolve_enrichment_prompt,
)

# Import new modules for structured outputs
try:
    from .enrichment_config import EnrichmentStrategy
    from .pydantic_schema import create_pydantic_model_from_schema
except ImportError:
    from enrichment_config import EnrichmentStrategy
    from pydantic_schema import create_pydantic_model_from_schema

# Try to import Google Generative AI
try:
    # Suppress logging is now handled centrally in logging_config
    from .utils.logging_config import suppress_noisy_loggers
    suppress_noisy_loggers()
    
    from google import genai
    GEMINI_AVAILABLE = True
    
except ImportError:
    GEMINI_AVAILABLE = False
    # Don't log warning here - will log when actually trying to use Gemini

# Model context limits (in tokens)
MODEL_CONTEXT_LIMITS = {
    'gpt-4o-mini': 128000,
    'gpt-4o': 128000,
    'gpt-4': 8192,
    'gpt-4-32k': 32768,
    'gpt-3.5-turbo': 16384,
    'gpt-3.5-turbo-16k': 16384,
    'gemini-2.5-flash-preview-05-20': 1000000,  # 1M context window
    'models/gemini-2.5-flash-preview-05-20': 1000000,  # Alternative name
    'gemini-2.5-flash': 1000000,  # 1M context window
    'models/gemini-2.5-flash': 1000000,  # Alternative name
    'gemini-3.5-flash': 1048576,  # 1M context window
    'models/gemini-3.5-flash': 1048576,  # Alternative name
    'gemini-2.0-flash': 1000000,  # 1M context window  
    'models/gemini-2.0-flash': 1000000,  # Alternative name
}

def estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token for most languages."""
    return len(text) // 4

def truncate_input_for_model(full_prompt: str, input_text: str, model: str, safety_margin: int = 2000, context_window: Optional[int] = None) -> Tuple[str, bool]:
    """
    Truncate input_text if the full message would exceed model's context limit.
    Returns (truncated_input_text, was_truncated)
    """
    # CLI backends (cli/claude/*, cli/codex/*, cli/gemini/*) all have large context windows
    if context_window is not None:
        context_limit = context_window
    elif model.startswith('cli/'):
        context_limit = 200000
    else:
        context_limit = MODEL_CONTEXT_LIMITS.get(model, 8192)
    
    # Estimate tokens for the full prompt
    prompt_tokens = estimate_tokens(full_prompt)
    input_tokens = estimate_tokens(input_text)
    total_tokens = prompt_tokens + input_tokens
    
    max_allowed_tokens = context_limit - safety_margin
    
    if total_tokens <= max_allowed_tokens:
        return input_text, False
    
    # Calculate how much we need to truncate the input
    max_input_tokens = max_allowed_tokens - prompt_tokens
    if max_input_tokens <= 0:
        logging.warning(f"Prompt itself is too long ({prompt_tokens} tokens), cannot fit any input")
        return "", True
    
    # Truncate input text
    max_input_chars = max_input_tokens * 4  # Rough conversion back to chars
    truncated_input = input_text[:max_input_chars]
    
    # Try to truncate at word boundary
    if len(truncated_input) < len(input_text):
        last_space = truncated_input.rfind(' ')
        if last_space > max_input_chars * 0.8:  # If we can find a space in the last 20%
            truncated_input = truncated_input[:last_space]
    
    logging.warning(f"Truncated input from {len(input_text)} to {len(truncated_input)} chars (estimated {input_tokens} -> {estimate_tokens(truncated_input)} tokens)")
    
    return truncated_input, True

# Initialize clients lazily to avoid requiring credentials at import time
_openai_client: Optional[AsyncOpenAI] = None
gemini_client = None


def _key_prefix(key_value: Any) -> str:
    """Return a stable short key preview for logging."""
    text = "" if key_value is None else str(key_value)
    return text[:8] if len(text) >= 8 else text


def _uses_direct_anthropic(model: str) -> bool:
    """Return True for models sent to Anthropic's direct API."""
    return model.startswith("claude") or model.startswith("anthropic/")


def _anthropic_cached_user_content(
    model: str,
    full_prompt: str,
    final_input_text: str,
    full_prompt_content: str,
) -> Any:
    """Split static prompt and row input so Anthropic can cache the stable prefix."""
    if not _uses_direct_anthropic(model) or not full_prompt:
        return full_prompt_content

    blocks = [
        {
            "type": "text",
            "text": full_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if final_input_text:
        blocks.append({"type": "text", "text": final_input_text})
    return blocks


def _token_usage_to_dict(token_usage: Any) -> Dict[str, Any]:
    """Convert provider token usage into Doctrail's audit usage dict."""
    usage_dict = {
        'input_tokens': token_usage.input_tokens,
        'output_tokens': token_usage.output_tokens,
        'estimated_cost': token_usage.estimate_cost(),
    }
    cached_tokens = getattr(token_usage, 'cached_input_tokens', 0) or 0
    cache_creation_tokens = getattr(token_usage, 'cache_creation_input_tokens', 0) or 0
    if cached_tokens:
        usage_dict['cached_input_tokens'] = cached_tokens
    if cache_creation_tokens:
        usage_dict['cache_creation_input_tokens'] = cache_creation_tokens
    return usage_dict


def _split_integer_total(total: Optional[int], count: int) -> List[int]:
    """Split an integer total across count buckets while preserving the sum."""
    if count <= 0:
        return []
    value = int(total or 0)
    base, remainder = divmod(value, count)
    return [base + (1 if index < remainder else 0) for index in range(count)]


def _split_float_total(total: Optional[float], count: int) -> List[float]:
    """Split a float total across count buckets while preserving the sum."""
    if count <= 0:
        return []
    value = float(total or 0.0)
    if count == 1:
        return [value]
    base = value / count
    parts = [base for _ in range(count)]
    parts[-1] = value - sum(parts[:-1])
    return parts


def _split_usage_across_rows(usage: Optional[Dict[str, Any]], count: int) -> List[Optional[Dict[str, Any]]]:
    """Distribute one call's usage across row-level audit records."""
    if count <= 0:
        return []
    if not usage:
        return [None] * count

    input_tokens = _split_integer_total(usage.get("input_tokens"), count)
    output_tokens = _split_integer_total(usage.get("output_tokens"), count)
    cached_input_tokens = _split_integer_total(usage.get("cached_input_tokens"), count)
    cache_creation_input_tokens = _split_integer_total(usage.get("cache_creation_input_tokens"), count)
    estimated_cost = _split_float_total(usage.get("estimated_cost"), count)

    split_rows: List[Dict[str, Any]] = []
    for index in range(count):
        row_usage: Dict[str, Any] = {
            "input_tokens": input_tokens[index],
            "output_tokens": output_tokens[index],
            "estimated_cost": estimated_cost[index],
        }
        if any(cached_input_tokens):
            row_usage["cached_input_tokens"] = cached_input_tokens[index]
        if any(cache_creation_input_tokens):
            row_usage["cache_creation_input_tokens"] = cache_creation_input_tokens[index]
        split_rows.append(row_usage)
    return split_rows


def _annotation_is_bool(annotation: Any) -> bool:
    """Return True if the annotation is bool or Optional[bool]."""
    if annotation is bool:
        return True
    return bool in get_args(annotation)


def _get_pack_settings(
    enrichment_config: Dict[str, Any],
    output_cols: List[str],
    pydantic_model: Optional[Type[BaseModel]],
) -> Optional[Dict[str, Any]]:
    """Return validated pack-mode settings, or None when pack mode is disabled."""
    raw_pack_size = enrichment_config.get("pack_size")
    if raw_pack_size in (None, 0, 1):
        return None

    try:
        pack_size = int(raw_pack_size)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"pack_size must be an integer, got: {raw_pack_size!r}") from exc

    if pack_size < 2:
        raise ValueError(f"pack_size must be >= 2, got: {pack_size}")
    if not pydantic_model:
        raise ValueError("pack_size requires a schema-driven enrichment")

    response_mode = enrichment_config.get("pack_response_mode", "exhaustive")
    if response_mode not in {"exhaustive", "selected_indexes"}:
        raise ValueError(
            "pack_response_mode must be one of: exhaustive, selected_indexes"
        )

    settings: Dict[str, Any] = {
        "pack_size": pack_size,
        "response_mode": response_mode,
    }

    if response_mode == "selected_indexes":
        if len(output_cols) != 1:
            raise ValueError(
                "pack_response_mode=selected_indexes requires exactly one output field"
            )
        output_field = output_cols[0]
        field_info = pydantic_model.model_fields.get(output_field)
        if field_info is None or not _annotation_is_bool(field_info.annotation):
            raise ValueError(
                "pack_response_mode=selected_indexes requires a single boolean output field"
            )
        settings["boolean_output_field"] = output_field

    return settings


def _chunk_rows(rows: List[Dict[str, Any]], chunk_size: int) -> List[List[Dict[str, Any]]]:
    """Split a row list into fixed-size chunks."""
    return [rows[index:index + chunk_size] for index in range(0, len(rows), chunk_size)]


def _template_context(config: Optional[Dict[str, Any]], model: str, table: Optional[str]) -> Dict[str, str]:
    """Build scalar prompt variables from execution context and YAML config."""
    context: Dict[str, str] = {"model": model}
    if table:
        context["table"] = table
    elif config and config.get("default_table"):
        context["table"] = str(config["default_table"])
    if config:
        for key, value in config.items():
            if key.startswith("_") or isinstance(value, (dict, list, tuple, set)):
                continue
            if value is None:
                continue
            context.setdefault(key, str(value))
    return context


def _render_row_prompt_content(
    *,
    row: Dict[str, Any],
    parsed_input_cols: List[Tuple[str, Optional[int]]],
    prompt: str,
    model: str,
    key_column: str,
    truncate: bool,
    output_schema: Any = None,
    config: Optional[Dict[str, Any]] = None,
    table: Optional[str] = None,
    include_schema_instructions: bool = False,
    verbose: bool = False,
    context_window: Optional[int] = None,
) -> Dict[str, Any]:
    """Render one row's effective prompt content and input text."""
    limited_data = apply_column_limits(row, parsed_input_cols)
    skip_cols = {"rowid", key_column}
    templated_prompt = prompt
    template_replacements = {}

    for col, _ in parsed_input_cols:
        if col in skip_cols:
            continue
        col_value = limited_data.get(col, "")
        if f"{{{col}}}" in templated_prompt:
            templated_prompt = templated_prompt.replace(f"{{{col}}}", str(col_value))
            template_replacements[f"{{{col}}}"] = str(col_value)
        if "." in col:
            _, column_only = col.split(".", 1)
            if f"{{{column_only}}}" in templated_prompt:
                templated_prompt = templated_prompt.replace(f"{{{column_only}}}", str(col_value))
                template_replacements[f"{{{column_only}}}"] = str(col_value)

    for var_name, var_value in _template_context(config, model, table).items():
        placeholder = f"{{{var_name}}}"
        if placeholder in templated_prompt:
            templated_prompt = templated_prompt.replace(placeholder, var_value)
            template_replacements[placeholder] = var_value

    if template_replacements and verbose:
        logging.info(f"Template substitutions: {template_replacements}")

    full_prompt = templated_prompt
    if include_schema_instructions and output_schema and config:
        schema_instructions = get_schema_prompt_instructions(config, output_schema)
        if schema_instructions:
            full_prompt = templated_prompt + "\n\n" + schema_instructions

    input_text = "\n".join(
        f"{col}: {limited_data.get(col, '')}"
        for col, _ in parsed_input_cols
        if col not in skip_cols
    )

    final_input_text = input_text
    was_truncated = False
    if truncate:
        final_input_text, was_truncated = truncate_input_for_model(
            full_prompt,
            input_text,
            model,
            context_window=context_window,
        )

    full_prompt_content = full_prompt
    if final_input_text:
        full_prompt_content = full_prompt + "\n\n" + final_input_text
    message_content = _anthropic_cached_user_content(
        model,
        full_prompt,
        final_input_text,
        full_prompt_content,
    )

    return {
        "limited_data": limited_data,
        "templated_prompt": templated_prompt,
        "input_text": input_text,
        "full_prompt": full_prompt,
        "full_prompt_content": full_prompt_content,
        "message_content": message_content,
        "was_truncated": was_truncated,
    }


def _build_packed_response_model(
    base_model: Type[BaseModel],
    response_mode: str,
) -> Type[BaseModel]:
    """Create a wrapper model for one packed LLM response."""
    model_prefix = base_model.__name__
    if response_mode == "selected_indexes":
        return create_model(
            f"{model_prefix}PackedSelectionModel",
            selected_item_indexes=(
                List[int],
                Field(
                    default_factory=list,
                    description="Zero-based item indexes that satisfy the task.",
                ),
            ),
        )

    packed_item_model = create_model(
        f"{model_prefix}PackedItemModel",
        item_index=(
            int,
            Field(..., ge=0, description="Zero-based item index from the packed input list."),
        ),
        result=(base_model, ...),
    )
    return create_model(
        f"{model_prefix}PackedResponseModel",
        items=(
            List[packed_item_model],
            Field(
                ...,
                description="One result per input item, preserving the zero-based item_index.",
            ),
        ),
    )


def _build_packed_prompt(
    *,
    rendered_rows: List[Dict[str, Any]],
    response_mode: str,
) -> str:
    """Compose the user message for a multi-row packed request."""
    instruction_lines = [
        "You are processing multiple independent items in one request.",
        "Evaluate each item independently using the task and fields shown under that item.",
        "Item indexes are zero-based.",
    ]
    if response_mode == "selected_indexes":
        instruction_lines.extend([
            "Return only the zero-based item indexes that satisfy the task.",
            "Omitted indexes will be treated as false.",
            "Do not include duplicates or out-of-range indexes.",
        ])
    else:
        instruction_lines.extend([
            "Return exactly one result for every item.",
            "Do not omit any item and do not duplicate item indexes.",
        ])

    item_blocks = []
    for item_index, rendered in enumerate(rendered_rows):
        item_blocks.append(
            f"Item {item_index}\n{rendered['full_prompt_content']}"
        )

    return "\n\n".join(["\n".join(instruction_lines)] + item_blocks)


def _build_pack_error_results(
    rows: List[Dict[str, Any]],
    *,
    key_column: str,
    error: str,
    full_prompt: Optional[str],
    raw_json: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Create row-shaped error payloads for a failed pack call."""
    payload = raw_json or json.dumps({"error": error}, ensure_ascii=False)
    return [
        {
            "enrichment_id": str(uuid.uuid4()),
            "rowid": row.get("rowid", "NO_ROWID"),
            "key_value": row.get(key_column, "NO_KEY"),
            "original": {},
            "updated": None,
            "error": error,
            "raw_json": payload,
            "full_prompt": full_prompt,
        }
        for row in rows
    ]


def _unpack_packed_results(
    *,
    rows: List[Dict[str, Any]],
    packed_result: Dict[str, Any],
    key_column: str,
    response_mode: str,
    boolean_output_field: Optional[str],
    full_prompt_content: str,
    raw_json: str,
    usage: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Explode one packed response into ordinary row-level enrichment results."""
    row_count = len(rows)
    usage_parts = _split_usage_across_rows(usage, row_count)

    if response_mode == "selected_indexes":
        selected_indexes = packed_result.get("selected_item_indexes", []) or []
        if len(selected_indexes) != len(set(selected_indexes)):
            raise ValueError("Packed response included duplicate selected_item_indexes")
        invalid_indexes = [index for index in selected_indexes if index < 0 or index >= row_count]
        if invalid_indexes:
            raise ValueError(
                f"Packed response included out-of-range selected_item_indexes: {invalid_indexes}"
            )
        selected_set = set(selected_indexes)
        return [
            {
                "enrichment_id": str(uuid.uuid4()),
                "rowid": row.get("rowid", "NO_ROWID"),
                "key_value": row.get(key_column, "NO_KEY"),
                "original": {},
                "updated": {boolean_output_field: index in selected_set},
                "raw_json": raw_json,
                "full_prompt": full_prompt_content,
                "usage": usage_parts[index],
            }
            for index, row in enumerate(rows)
        ]

    items = packed_result.get("items")
    if not isinstance(items, list):
        raise ValueError("Packed exhaustive response must include an items list")

    by_index: Dict[int, Dict[str, Any]] = {}
    for item in items:
        item_index = item.get("item_index")
        if not isinstance(item_index, int):
            raise ValueError(f"Packed item missing integer item_index: {item}")
        if item_index < 0 or item_index >= row_count:
            raise ValueError(f"Packed item_index out of range: {item_index}")
        if item_index in by_index:
            raise ValueError(f"Packed response duplicated item_index {item_index}")
        by_index[item_index] = item

    row_results: List[Dict[str, Any]] = []
    for index, row in enumerate(rows):
        packed_item = by_index.get(index)
        if packed_item is None:
            row_results.append(
                {
                    "enrichment_id": str(uuid.uuid4()),
                    "rowid": row.get("rowid", "NO_ROWID"),
                    "key_value": row.get(key_column, "NO_KEY"),
                    "original": {},
                    "updated": None,
                    "error": f"Packed response omitted item_index {index}",
                    "raw_json": raw_json,
                    "full_prompt": full_prompt_content,
                    "usage": usage_parts[index],
                }
            )
            continue

        row_results.append(
            {
                "enrichment_id": str(uuid.uuid4()),
                "rowid": row.get("rowid", "NO_ROWID"),
                "key_value": row.get(key_column, "NO_KEY"),
                "original": {},
                "updated": packed_item.get("result"),
                "raw_json": raw_json,
                "full_prompt": full_prompt_content,
                "usage": usage_parts[index],
            }
        )

    return row_results


def get_openai_client() -> AsyncOpenAI:
    """Return a lazily-initialized OpenAI client."""

    global _openai_client

    if _openai_client is None:
        load_doctrail_environment()
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY environment variable must be set before invoking OpenAI models."
            )

        _openai_client = AsyncOpenAI(api_key=api_key)

    return _openai_client

def get_gemini_client():
    """Return a lazily-initialized Gemini client."""

    global gemini_client

    if gemini_client is not None:
        return gemini_client

    if not GEMINI_AVAILABLE:
        raise RuntimeError("google-genai is not installed; Gemini models are unavailable.")

    load_doctrail_environment()
    gemini_api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GOOGLE_AI_API_KEY")
    )
    if not gemini_api_key:
        raise RuntimeError(
            "Gemini API key not found in GEMINI_API_KEY, GOOGLE_API_KEY, or GOOGLE_AI_API_KEY."
        )

    try:
        import grpc

        options = [
            ('grpc.keepalive_time_ms', 10000),
            ('grpc.keepalive_timeout_ms', 5000),
            ('grpc.keepalive_permit_without_calls', True),
            ('grpc.http2.max_pings_without_data', 0),
            ('grpc.http2.min_time_between_pings_ms', 10000),
            ('grpc.http2.min_ping_interval_without_data_ms', 300000),
            ('grpc.max_concurrent_streams', 100),
        ]
        gemini_client = genai.Client(api_key=gemini_api_key, transport_options=options)
    except Exception:
        gemini_client = genai.Client(api_key=gemini_api_key)

    return gemini_client

async def call_llm(
    model: str,
    messages: list,
    system_prompt: str = None,
    verbose: bool = False,
    reasoning_effort: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """
    Make a simple LLM API call using the appropriate provider.
    Returns the response text.
    """
    # Import provider factory
    from .llm_providers.factory import get_llm_provider
    
    # Get the appropriate provider
    provider = get_llm_provider(model)
    
    # Prepare messages (system prompt already in messages if needed)
    if system_prompt and messages[0]['role'] != 'system':
        messages = [{'role': 'system', 'content': system_prompt}] + messages
    
    try:
        # Use provider's text generation method
        generate_kwargs = {
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        if hasattr(provider, "supports_reasoning_effort") and provider.supports_reasoning_effort():
            generate_kwargs["reasoning_effort"] = reasoning_effort
        result_text = await provider.generate_text(**generate_kwargs)
        
        # Check for mojibake
        if detect_mojibake(result_text):
            provider_name = type(provider).__name__
            logging.warning(f"Mojibake detected in {provider_name} response (length: {len(result_text)})")
            # Try to fix it
            fixed_text = try_fix_mojibake(result_text)
            if fixed_text != result_text:
                logging.info("Mojibake fixed successfully")
                result_text = fixed_text
            else:
                logging.warning("Unable to fix mojibake automatically")
        
        return result_text
        
    except Exception as e:
        logging.error(f"LLM API call failed: {e}")
        raise

async def call_llm_structured(model: str, messages: List[Dict], pydantic_model: Type[BaseModel],
                             system_prompt: str = None, verbose: bool = False, provider=None,
                             return_usage: bool = False, reasoning_effort: Optional[str] = None,
                             replay_key_value: Optional[Any] = None,
                             max_tokens: Optional[int] = None):
    """
    Make a structured LLM API call using provider-specific structured output APIs.

    Args:
        model: Model name
        messages: List of message dictionaries
        pydantic_model: Pydantic model class for response format
        system_prompt: Optional system prompt
        verbose: Enable verbose logging
        provider: Optional pre-created provider (for efficiency)
        return_usage: If True, return (result, usage_dict) tuple

    Returns:
        If return_usage=False: Parsed Pydantic model instance
        If return_usage=True: Tuple of (parsed model, usage dict or None)
    """
    # Use provided provider or create new one
    if provider is None:
        from .llm_providers.factory import get_llm_provider
        provider = get_llm_provider(model)

    # Log which provider we're using
    provider_name = type(provider).__name__
    logging.debug(f"Using {provider_name} for structured output with model {model}")

    # Prepare messages (system prompt already in messages if needed)
    if system_prompt and messages[0]['role'] != 'system':
        messages = [{'role': 'system', 'content': system_prompt}] + messages

    try:
        # Use provider's structured output method
        if return_usage:
            generate_kwargs = {
                "messages": messages,
                "pydantic_model": pydantic_model,
                "temperature": 0.0,
                "return_usage": True,
                "max_tokens": max_tokens,
            }
            if getattr(provider, "uses_replay_fixtures", False):
                generate_kwargs["replay_key_value"] = replay_key_value
            if hasattr(provider, "supports_reasoning_effort") and provider.supports_reasoning_effort():
                generate_kwargs["reasoning_effort"] = reasoning_effort
            result, token_usage = await provider.generate_structured(**generate_kwargs)

            # Convert TokenUsage object to dict for easier handling
            usage_dict = None
            if token_usage:
                usage_dict = _token_usage_to_dict(token_usage)

            if verbose:
                logging.debug(f"Structured output response: {result}")
                if usage_dict:
                    logging.debug(f"Token usage: {usage_dict}")

            return result, usage_dict
        else:
            generate_kwargs = {
                "messages": messages,
                "pydantic_model": pydantic_model,
                "temperature": 0.0,
                "max_tokens": max_tokens,
            }
            if getattr(provider, "uses_replay_fixtures", False):
                generate_kwargs["replay_key_value"] = replay_key_value
            if hasattr(provider, "supports_reasoning_effort") and provider.supports_reasoning_effort():
                generate_kwargs["reasoning_effort"] = reasoning_effort
            result = await provider.generate_structured(**generate_kwargs)

            if verbose:
                logging.debug(f"Structured output response: {result}")

            return result

    except Exception as e:
        logging.error(f"Structured output API call failed for {provider_name}: {e}")
        raise

def setup_enrichment_logging(verbose: bool):
    """Set up basic logging to both console and file"""
    # Clear existing handlers
    logging.getLogger().handlers.clear()
    
    # Set up basic logging format
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # Console handler - only show INFO and above
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    
    # File handler - show everything if verbose
    file_handler = logging.FileHandler('/tmp/doctrail.log', mode='w')
    file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_handler.setFormatter(formatter)
    
    # Configure root logger
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(console)
    logger.addHandler(file_handler)

async def process_batch(results, prompt, model, pbar, input_cols, parsed_input_cols, output_cols, db_path, table,
                       enrichment_config, output_schema=None, system_prompt=None, overwrite=False, config=None, truncate=False, verbose=False, output_table=None, key_column=DEFAULT_KEY_COLUMN, enrichment_strategy=None, suppress_progress_messages=False, project: Optional[str] = None, separate_output_db: bool = False, run_id: Optional[str] = None, query_hash: Optional[str] = None, dedupe_scope: str = "query"):
    """Process a batch of rows with the LLM"""
    if verbose:
        logging.info(f"Processing batch of {len(results)} rows")

    # Deduplicate input rows by key_column to avoid processing same document twice
    # (e.g., same PDF attached to multiple Zotero items)
    seen_keys = set()
    deduped_results = []
    duplicate_count = 0
    for row in results:
        key = row.get(key_column, 'NO_KEY')
        if key not in seen_keys:
            seen_keys.add(key)
            deduped_results.append(row)
        else:
            duplicate_count += 1

    if duplicate_count > 0:
        if verbose:
            logging.info(f"Deduplicated input: {len(results)} -> {len(deduped_results)} rows ({duplicate_count} duplicates by {key_column})")
        elif not suppress_progress_messages:
            print(f"Deduplicated: removed {duplicate_count} duplicate rows (by {key_column})")

    results = deduped_results

    # Get or create prompt_id for tracking prompt versions
    # prompt_id IS the hash — deterministic, same prompt = same ID
    enrichment_name = enrichment_config.get('name', 'unknown')
    prompt_id = get_or_create_prompt_id(db_path, enrichment_name, prompt, system_prompt, model)
    logging.debug(f"Using prompt_id: {prompt_id[:8]} for enrichment: {enrichment_name}")
    # Skip rows that already have successful outputs unless overwrite is True.
    # Failed attempts remain in the audit trail but should be retried by default.
    skipped_rows = []
    if not overwrite:
        skipped_rows = plan_existing_enrichment_skips(
            db_path=db_path,
            rows=results,
            enrichment_name=enrichment_name,
            model=model,
            prompt_id=prompt_id,
            key_column=key_column,
            dedupe_scope=dedupe_scope,
            query_hash=query_hash,
            output_table=output_table,
            output_cols=output_cols,
            separate_output_db=separate_output_db,
            source_table=table,
        )

    if skipped_rows and not suppress_progress_messages:
        if verbose:
            logging.info(f"⏭️  Skipping {len(skipped_rows)} rows with existing data. Use --overwrite to update these rows.")
        else:
            print(f"⏭️  Skipping {len(skipped_rows)} rows (already have data)")
        # Don't update progress bar for skipped rows - only count actual processing
    
    if overwrite and len(results) > 0:
        existing_data_count = 0
        enrichment_name = enrichment_config.get('name', 'unknown')
        candidate_keys = [
            str(row.get(key_column))
            for row in results
            if row.get(key_column) not in (None, 'NO_KEY')
        ]
        existing_data_count = len(
            get_successfully_enriched_keys(
                db_path,
                key_values=candidate_keys,
                enrichment_name=enrichment_name,
                model=model,
                prompt_id=prompt_id,
                query_hash=query_hash if dedupe_scope == "query" and bool(query_hash) else None,
            )
        )

        if existing_data_count > 0:
            if verbose:
                logging.info(f"Overwriting {existing_data_count} rows that have existing data")
            else:
                print(f"Processing {existing_data_count} rows (overwriting existing data)")
    
    rows_to_process = filter_unskipped_input_rows(
        results,
        skipped_rows,
        key_column=key_column,
    )

    # Filter out rows with blank/insufficient input content. This is intentionally
    # configurable because many review tasks operate on short evidence snippets.
    min_input_chars = int(
        enrichment_config.get(
            "min_input_chars",
            (config or {}).get("min_input_chars", 1),
        )
    )
    insufficient_rows = []
    sufficient_rows = []

    for row in rows_to_process:
        total_input_chars = 0
        for col, _ in parsed_input_cols:
            if col not in ['rowid', 'sha1', key_column]:
                val = row.get(col, '')
                if val:
                    total_input_chars += len(str(val))

        if total_input_chars < min_input_chars:
            key_value = row.get(key_column, 'NO_KEY')
            insufficient_rows.append({
                'rowid': row.get('rowid', 'NO_ROWID'),
                'key_value': key_value,
                'original': f"insufficient input ({total_input_chars} chars)",
                'updated': None,
                'error': f"Skipped: only {total_input_chars} chars in input columns (minimum: {min_input_chars})"
            })
        else:
            sufficient_rows.append(row)

    if insufficient_rows and not suppress_progress_messages:
        if verbose:
            logging.warning(f"Skipping {len(insufficient_rows)} rows with insufficient input content (<{min_input_chars} chars)")
            for row in insufficient_rows[:3]:  # Show first 3 examples
                kv = row['key_value']
                kv_prefix = _key_prefix(kv)
                logging.warning(f"   - {kv_prefix}: {row['error']}")
        else:
            print(f"Skipping {len(insufficient_rows)} rows (insufficient input content)")

    rows_to_process = sufficient_rows

    if verbose:
        logging.info(f"Found {len(rows_to_process)} rows to process")
    else:
        if rows_to_process and not suppress_progress_messages:
            print(f"Processing {len(rows_to_process)} rows...")

    if not rows_to_process:
        if verbose:
            logging.warning("No rows left to process!")
        elif not suppress_progress_messages:
            print("All rows already processed.")
        return skipped_rows + insufficient_rows

    # Set up concurrency limits for API and DB access
    default_concurrency = 4 if model.startswith("openai-compatible/") else DEFAULT_API_SEMAPHORE_LIMIT
    concurrency = int(enrichment_config.get("concurrency", (config or {}).get("concurrency", default_concurrency)))
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    semaphore = asyncio.Semaphore(concurrency)
    db_semaphore = asyncio.Semaphore(DEFAULT_DB_SEMAPHORE_LIMIT)  # Limit database writes to prevent locks
    processed_results = []
    reasoning_effort = enrichment_config.get("reasoning_effort")
    default_max_tokens = 512 if model.startswith("openai-compatible/") else None
    max_tokens = enrichment_config.get("max_tokens", (config or {}).get("max_tokens", default_max_tokens))
    if max_tokens is not None:
        max_tokens = int(max_tokens)
        if max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")
    context_window = enrichment_config.get("context_window", (config or {}).get("context_window"))
    if context_window is not None:
        context_window = int(context_window)
        if context_window < 1:
            raise ValueError("context_window must be at least 1")
    
    # Create provider once and reuse for all requests (much more efficient)
    llm_provider = None
    if enrichment_strategy and enrichment_strategy.pydantic_model:
        from .llm_providers.factory import get_llm_provider
        llm_provider = get_llm_provider(model, enrichment_name=enrichment_name)
        logging.debug(f"Created reusable {type(llm_provider).__name__} for {model}")

    pack_settings = _get_pack_settings(
        enrichment_config=enrichment_config,
        output_cols=output_cols,
        pydantic_model=getattr(enrichment_strategy, "pydantic_model", None),
    )
    if pack_settings and enrichment_config["name"] in TRANSLATION_ENRICHMENTS:
        raise ValueError("pack_size is not supported for translation enrichments")

    run_writer = EnrichmentRunWriter(db_path)

    async def persist_result(result: Dict, projection_output_fields: Optional[List[str]] = None) -> None:
        """Persist one result to audit and enrichments from the same normalized projection."""
        if not result:
            return

        await asyncio.to_thread(
            run_writer.persist,
            persist_func=persist_enrichment_result,
            key_value=result['key_value'],
            enrichment_name=enrichment_config['name'],
            updated=result.get('updated'),
            model=model,
            enrichment_id=result.get('enrichment_id'),
            prompt_id=prompt_id,
            full_prompt=result.get('full_prompt'),
            usage=result.get('usage'),
            run_id=run_id,
            query_hash=query_hash,
            project=project,
            overwrite=overwrite,
            projection_output_fields=projection_output_fields,
            raw_json=result.get('raw_json'),
            error=result.get('error'),
        )
    
    async def process_and_save(row):
        # NEW: Schema-driven structured output approach
        if enrichment_strategy and enrichment_strategy.pydantic_model:
            try:
                result = await process_row_structured(
                    row=row,
                    input_cols=input_cols,
                    parsed_input_cols=parsed_input_cols,
                    prompt=prompt,
                    model=model,
                    semaphore=semaphore,
                    pbar=pbar,
                    pydantic_model=enrichment_strategy.pydantic_model,
                    system_prompt=system_prompt,
                    truncate=truncate,
                    verbose=verbose,
                    provider=llm_provider,
                    key_column=key_column,
                    reasoning_effort=reasoning_effort,
                    config=config,
                    table=table,
                    max_tokens=max_tokens,
                    context_window=context_window,
                )

            except Exception as e:
                logging.error(f"Error in schema-driven processing: {e}")
                # Fall back to legacy processing
                pass
            else:
                if result:  # Store ALL results, including failures/nulls for audit trail
                    async with db_semaphore:
                        await persist_result(result)
                return result
        
        # LEGACY: Existing hardcoded processing logic
        if enrichment_config['name'] in TRANSLATION_ENRICHMENTS and enrichment_config['name'] == 'translate_to_english_by_line':
            result = await process_translation(
                row=row,
                input_cols=[(col, None) for col in input_cols],
                prompt=prompt,
                model=model,
                semaphore=semaphore,
                pbar=pbar,
                output_cols=output_cols,
                output_schema=output_schema,
                system_prompt=system_prompt,
                truncate=truncate,
                key_column=key_column
            )
            
            # ALWAYS store to _enrichment_audit for audit trail, even for failures
            if result:
                async with db_semaphore:
                    await persist_result(result)
        elif enrichment_config['name'] in TRANSLATION_ENRICHMENTS and enrichment_config['name'] == 'translate_to_english':
            # Full document translation (simpler, more reliable)
            # Validate: Gemini should only have one output column
            if model.startswith('gemini') and len(output_cols) > 1:
                raise ValueError(f"Gemini model {model} can only output to one column for translation, but got {len(output_cols)} columns: {output_cols}")
            
            result = await process_row(
                row=row,
                input_cols=input_cols,
                parsed_input_cols=parsed_input_cols,  # Pass the new parsed columns
                prompt=prompt,
                model=model,
                semaphore=semaphore,
                pbar=pbar,
                output_col=output_cols[0],  # Only use first output column
                output_schema=output_schema,
                system_prompt=system_prompt,
                config=config,
                truncate=truncate,
                verbose=verbose,
                key_column=key_column,
                reasoning_effort=reasoning_effort,
                table=table,
                max_tokens=max_tokens,
                context_window=context_window,
            )

            # ALWAYS store to _enrichment_audit for audit trail, even for failures
            if result:
                async with db_semaphore:
                    await persist_result(result, projection_output_fields=output_cols)
            return result
        else:
            result = await process_row(
                row=row,
                input_cols=input_cols,
                parsed_input_cols=parsed_input_cols,  # Pass the new parsed columns
                prompt=prompt,
                model=model,
                semaphore=semaphore,
                pbar=pbar,
                output_col=output_cols[0],
                output_schema=output_schema,
                system_prompt=system_prompt,
                config=config,
                truncate=truncate,
                verbose=verbose,
                key_column=key_column,
                reasoning_effort=reasoning_effort,
                table=table,
                max_tokens=max_tokens,
                context_window=context_window,
            )

            # ALWAYS store to _enrichment_audit for audit trail, even for failures
            if result:
                async with db_semaphore:
                    await persist_result(result, projection_output_fields=output_cols)
        return result

    async def process_pack_and_save(pack_rows: List[Dict]):
        result_rows = await process_rows_packed_structured(
            rows=pack_rows,
            parsed_input_cols=parsed_input_cols,
            prompt=prompt,
            model=model,
            semaphore=semaphore,
            pbar=pbar,
            pydantic_model=enrichment_strategy.pydantic_model,
            system_prompt=system_prompt,
            truncate=truncate,
            verbose=verbose,
            provider=llm_provider,
            key_column=key_column,
            reasoning_effort=reasoning_effort,
            config=config,
            table=table,
            pack_settings=pack_settings,
            max_tokens=max_tokens,
            context_window=context_window,
        )
        for result in result_rows:
            if result:
                async with db_semaphore:
                    await persist_result(result)
        return result_rows

    if pack_settings:
        task_inputs = _chunk_rows(rows_to_process, pack_settings["pack_size"])
        tasks = [process_pack_and_save(pack_rows) for pack_rows in task_inputs]
    else:
        task_inputs = rows_to_process
        tasks = [process_and_save(row) for row in rows_to_process]
    try:
        print(f"Starting {len(tasks)} parallel API calls with semaphore limit of {concurrency}")
        start_time = asyncio.get_event_loop().time()
        processed_results = await asyncio.gather(*tasks)
        end_time = asyncio.get_event_loop().time()
        print(f"Completed {len(tasks)} API calls in {end_time - start_time:.1f} seconds ({end_time - start_time:.1f}/{len(tasks)} = {(end_time - start_time)/len(tasks):.1f}s per call average)")

        if pack_settings:
            flattened_results = []
            for pack_result in processed_results:
                flattened_results.extend(pack_result)
            processed_results = flattened_results

        # Calculate and display token usage summary
        total_input_tokens = 0
        total_cached_input_tokens = 0
        total_cache_creation_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        results_with_usage = 0
        for r in processed_results:
            if r and r.get('usage'):
                usage = r['usage']
                total_input_tokens += usage.get('input_tokens', 0) or 0
                total_cached_input_tokens += usage.get('cached_input_tokens', 0) or 0
                total_cache_creation_input_tokens += usage.get('cache_creation_input_tokens', 0) or 0
                total_output_tokens += usage.get('output_tokens', 0) or 0
                total_cost += usage.get('estimated_cost', 0) or 0
                results_with_usage += 1

        if results_with_usage > 0:
            print(f"Token usage: {total_input_tokens:,} input + {total_output_tokens:,} output = {total_input_tokens + total_output_tokens:,} total")
            if total_cache_creation_input_tokens or total_cached_input_tokens:
                print(
                    "Prompt cache: "
                    f"{total_cache_creation_input_tokens:,} created + "
                    f"{total_cached_input_tokens:,} read"
                )
            print(f"Estimated cost: ${total_cost:.4f} ({results_with_usage} row results tracked)")

        # Run WAL checkpoint periodically to prevent WAL file from growing too large
        # Do this every 1000 processed rows
        total_processed = len([r for r in processed_results if r and r.get('updated')])
        if total_processed > 0 and total_processed % 1000 == 0:
            logging.info(f"Running WAL checkpoint after {total_processed} rows...")
            await asyncio.to_thread(checkpoint_wal, db_path)

        all_results = processed_results + skipped_rows + insufficient_rows
        # Attach prompt_id to results so callers can create prompt-filtered views
        for r in all_results:
            if r:
                r['_prompt_id'] = prompt_id
        return all_results
    finally:
        await asyncio.to_thread(run_writer.close)


async def process_rows_packed_structured(
    *,
    rows: List[Dict[str, Any]],
    parsed_input_cols: List[Tuple[str, Optional[int]]],
    prompt: str,
    model: str,
    semaphore: asyncio.Semaphore,
    pbar: tqdm,
    pydantic_model: Type[BaseModel],
    pack_settings: Dict[str, Any],
    system_prompt: Optional[str] = None,
    truncate: bool = False,
    verbose: bool = False,
    provider=None,
    key_column: str = DEFAULT_KEY_COLUMN,
    reasoning_effort: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    table: Optional[str] = None,
    max_tokens: Optional[int] = None,
    context_window: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Process several rows in one structured call, then unpack back to row-level results."""
    async with semaphore:
        rendered_rows = [
            _render_row_prompt_content(
                row=row,
                parsed_input_cols=parsed_input_cols,
                prompt=prompt,
                model=model,
                key_column=key_column,
                truncate=truncate,
                config=config,
                table=table,
                include_schema_instructions=False,
                verbose=verbose,
                context_window=context_window,
            )
            for row in rows
        ]
        full_prompt_content = _build_packed_prompt(
            rendered_rows=rendered_rows,
            response_mode=pack_settings["response_mode"],
        )
        messages = [{"role": "user", "content": full_prompt_content}]
        packed_model = _build_packed_response_model(
            base_model=pydantic_model,
            response_mode=pack_settings["response_mode"],
        )
        usage_dict = None

        try:
            result, usage_dict = await call_llm_structured(
                model,
                messages,
                packed_model,
                system_prompt,
                verbose,
                provider,
                return_usage=True,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
            )
            result_dict = result.model_dump(mode="json")
            raw_json = result.model_dump_json()

            if pack_settings["response_mode"] == "exhaustive":
                for item in getattr(result, "items", []):
                    nested_result = getattr(item, "result", None)
                    if nested_result is None:
                        continue
                    if hasattr(nested_result, "apply_conversions"):
                        nested_result.apply_conversions(nested_result)
                    if hasattr(nested_result, "validate_languages"):
                        nested_result.validate_languages(nested_result)
                result_dict = result.model_dump(mode="json")
                raw_json = result.model_dump_json()

            row_results = _unpack_packed_results(
                rows=rows,
                packed_result=result_dict,
                key_column=key_column,
                response_mode=pack_settings["response_mode"],
                boolean_output_field=pack_settings.get("boolean_output_field"),
                full_prompt_content=full_prompt_content,
                raw_json=raw_json,
                usage=usage_dict,
            )
            pbar.update(len(rows))
            return row_results

        except Exception as exc:
            logging.error(f"Error processing packed rows: {exc}")
            pbar.update(len(rows))
            return _build_pack_error_results(
                rows,
                key_column=key_column,
                error=str(exc),
                full_prompt=full_prompt_content,
            )

async def process_row_structured(row: Dict, input_cols: List[str], parsed_input_cols: List[Tuple[str, Optional[int]]],
                               prompt: str, model: str, semaphore: asyncio.Semaphore, pbar: tqdm,
                               pydantic_model: Type[BaseModel], system_prompt: str = None,
                               truncate: bool = False, verbose: bool = False, provider=None,
                               key_column: str = DEFAULT_KEY_COLUMN,
                               reasoning_effort: Optional[str] = None,
                               config: Optional[Dict[str, Any]] = None,
                               table: Optional[str] = None,
                               max_tokens: Optional[int] = None,
                               context_window: Optional[int] = None):
    """Process a single row using structured outputs (OpenAI only)."""
    async with semaphore:
        key_value = row.get(key_column, 'NO_KEY')
        # Generate a unique enrichment_id for this specific LLM call
        row_enrichment_id = str(uuid.uuid4())
        start_time = asyncio.get_event_loop().time()
        kv_prefix = _key_prefix(key_value)
        logging.debug(f"Starting API call for {kv_prefix}")
        try:
            rendered = _render_row_prompt_content(
                row=row,
                parsed_input_cols=parsed_input_cols,
                prompt=prompt,
                model=model,
                key_column=key_column,
                truncate=truncate,
                config=config,
                table=table,
                include_schema_instructions=False,
                verbose=verbose,
                context_window=context_window,
            )
            messages = [{"role": "user", "content": rendered["message_content"]}]
            full_prompt_content = rendered["full_prompt_content"]
            
            # Make structured API call with retry logic for language validation
            max_retries = 2  # Total of 3 attempts (original + 2 retries)
            usage_dict = None  # Track token usage

            for attempt in range(max_retries + 1):
                try:
                    result, usage_dict = await call_llm_structured(
                        model,
                        messages,
                        pydantic_model,
                        system_prompt,
                        verbose,
                        provider,
                        return_usage=True,
                        reasoning_effort=reasoning_effort,
                        replay_key_value=key_value,
                        max_tokens=max_tokens,
                    )

                    # Apply field conversions if the model has them (BEFORE language validation)
                    if hasattr(result, 'apply_conversions'):
                        result.apply_conversions(result)

                    # Validate language requirements if the model has them (AFTER conversions)
                    if hasattr(result, 'validate_languages'):
                        result.validate_languages(result)

                    # Convert Pydantic model to dict for storage
                    # Use mode='json' to properly serialize enums to their values
                    result_dict = result.model_dump(mode='json')

                    # Console progress - only show model response if verbose
                    logging.debug(f"[{kv_prefix}] Structured result: {result_dict}")
                    if attempt > 0:
                        logging.info(f"Language validation passed on attempt {attempt + 1} for rowid {row.get('rowid', 'unknown')}")
                    pbar.update(1)

                    elapsed = asyncio.get_event_loop().time() - start_time
                    logging.debug(f"Completed API call for {kv_prefix} in {elapsed:.1f}s")

                    return {
                        'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                        'rowid': row.get('rowid', 'NO_ROWID'),
                        'key_value': key_value,
                        'original': {},  # Not applicable for structured outputs
                        'updated': result_dict,
                        'raw_json': result.model_dump_json(),
                        'full_prompt': full_prompt_content,
                        'usage': usage_dict  # Include token usage
                    }

                except LanguageValidationError as e:
                    if attempt < max_retries:
                        logging.warning(f"Language validation failed on attempt {attempt + 1} for rowid {row.get('rowid', 'unknown')}: {str(e)[:100]}... Retrying...")
                        # Don't update progress bar yet, we're retrying
                        continue
                    else:
                        # Final attempt failed, log error and continue
                        logging.error(f"Language validation failed after {max_retries + 1} attempts for rowid {row.get('rowid', 'unknown')}: {str(e)}")
                        pbar.update(1)
                        return {
                            'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                            'rowid': row.get('rowid', 'NO_ROWID'),
                            'key_value': key_value,
                            'original': {},
                            'updated': None,
                            'error': f"Language validation failed after {max_retries + 1} attempts: {str(e)}",
                            'raw_json': json.dumps({'error': f"Language validation failed after {max_retries + 1} attempts: {str(e)}"}, ensure_ascii=False),
                            'full_prompt': full_prompt_content,
                            'usage': usage_dict  # Include token usage even on failure
                        }

                except Exception as e:
                    # For non-language validation errors, don't retry
                    raise e

        except Exception as e:
            rowid = row.get('rowid', 'unknown')
            logging.error(f"Error processing rowid {rowid} (key_value: {kv_prefix}): {str(e)}")
            pbar.update(1)
            return {
                'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                'rowid': rowid,
                'key_value': key_value,
                'original': {},
                'updated': None,
                'error': str(e),
                'raw_json': getattr(e, 'raw_json', None) or json.dumps({'error': str(e)}, ensure_ascii=False),
                'full_prompt': full_prompt_content if 'full_prompt_content' in locals() else None,
                'usage': (
                    _token_usage_to_dict(e.usage)
                    if getattr(e, 'usage', None)
                    else (usage_dict if 'usage_dict' in locals() else None)
                )
            }

async def process_row(row: Dict, input_cols: List[str], parsed_input_cols: List[Tuple[str, Optional[int]]], prompt: str,
                     model: str, semaphore: asyncio.Semaphore, pbar: tqdm,
                     output_col: str, output_schema = None,
                     system_prompt: str = None, config: Dict = None, truncate: bool = False, verbose: bool = False,
                     key_column: str = DEFAULT_KEY_COLUMN,
                     reasoning_effort: Optional[str] = None,
                     table: Optional[str] = None,
                     max_tokens: Optional[int] = None,
                     context_window: Optional[int] = None):
    async with semaphore:
        key_value = row.get(key_column, 'NO_KEY')
        # Generate a unique enrichment_id for this specific LLM call
        row_enrichment_id = str(uuid.uuid4())
        kv_prefix = _key_prefix(key_value)
        try:
            rendered = _render_row_prompt_content(
                row=row,
                parsed_input_cols=parsed_input_cols,
                prompt=prompt,
                model=model,
                key_column=key_column,
                truncate=truncate,
                output_schema=output_schema,
                config=config,
                table=table,
                include_schema_instructions=True,
                verbose=verbose,
                context_window=context_window,
            )
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            full_prompt_content = rendered["full_prompt_content"]
            messages.append({"role": "user", "content": rendered["message_content"]})
            
            # Keep the plain call signature stable when no reasoning override is set.
            call_kwargs = {}
            if reasoning_effort is not None:
                call_kwargs["reasoning_effort"] = reasoning_effort
            if max_tokens is not None:
                call_kwargs["max_tokens"] = max_tokens

            result = await call_llm(
                model,
                messages,
                system_prompt,
                verbose,
                **call_kwargs,
            )
            
            # Validate result against schema if provided
            validated_result = result
            if output_schema and config:
                try:
                    validated_result = validate_with_schema(config, output_schema, result)
                    # Convert back to string for storage
                    if not isinstance(validated_result, str):
                        validated_result = str(validated_result)
                except SchemaValidationError as e:
                    logging.warning(f"[{kv_prefix}] Schema validation failed: {e}")
                    # Return error instead of invalid result
                    pbar.update(1)
                    return {
                        'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                        'rowid': row.get('rowid', 'NO_ROWID'),
                        'key_value': key_value,
                        'original': row.get(output_col, ''),
                        'updated': None,
                        'error': f"Schema validation failed: {e}",
                        'full_prompt': full_prompt_content
                    }

            # Console progress - only show model response if verbose
            logging.debug(f"[{kv_prefix}] {validated_result[:30]}..." if len(validated_result) > 30 else f"[{kv_prefix}] {validated_result}")
            pbar.update(1)

            return {
                'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                'rowid': row.get('rowid', 'NO_ROWID'),
                'key_value': key_value,
                'original': row.get(output_col, ''),
                'updated': validated_result,
                'full_prompt': full_prompt_content
            }

        except Exception as e:
            rowid = row.get('rowid', 'unknown')
            logging.error(f"Error processing rowid {rowid} (key_value: {kv_prefix}): {str(e)}")
            pbar.update(1)
            return {
                'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                'rowid': rowid,
                'key_value': key_value,
                'original': row.get(output_col, ''),
                'updated': None,
                'error': str(e),
                'full_prompt': full_prompt_content if 'full_prompt_content' in locals() else None
            }

def apply_slice(value, slice_):
    if slice_ is None:
        return value
    return value[slice_] if isinstance(value, str) else value

async def process_enrichment(
    results: List[Dict],
    enrichment_config: Dict,
    model: str,
    pbar: tqdm,
    db_path: str,
    table: str,
    source_db_path: Optional[str] = None,
    overwrite: bool = False,
    config: Dict = None,
    truncate: bool = False,
    verbose: bool = False,
    output_table: str = None,
    key_column: str = DEFAULT_KEY_COLUMN,
    enrichment_strategy: EnrichmentStrategy = None,
    is_multi_model: bool = False,
    project: Optional[str] = None,
    run_id: Optional[str] = None,
    query_hash: Optional[str] = None,
    dedupe_scope: str = "query",
):
    """Process a single enrichment task.

    db_path: database where enrichment results are written.
    source_db_path: database where source rows were read from (may differ from db_path
        when --output-db is used). Used only for logging; reads have already happened.

    The normalized tables (`_enrichment_audit`, `_enrichments`, `_enrichment_runs`)
    are the source of truth. Wide review surfaces are derived later as SQLite views.
    """
    logging.info(f"Starting enrichment '{enrichment_config['name']}'")
    logging.info(f"Model: {model}, Rows: {len(results)}, Overwrite: {overwrite}")
    if source_db_path and source_db_path != db_path:
        logging.info(f"Source: {source_db_path} -> Output: {db_path}")

    # Ensure normalized storage tables exist once before processing.
    ensure_enrichment_audit_table(db_path)
    ensure_enrichments_table(db_path)
    
    try:
        prompt = resolve_enrichment_prompt(enrichment_config, config or {})
        if 'append_file' in enrichment_config:
            logging.info(f"Appended content from file: {enrichment_config['append_file']}")
    except FileNotFoundError as e:
        append_file_path = enrichment_config.get('append_file')
        logging.error(f"append_file not found: {append_file_path}")
        raise ValueError(f"append_file not found: {append_file_path}") from e
    except Exception as e:
        logging.error(f"Error reading append_file: {e}")
        raise
    
    system_prompt = enrichment_config.get('system_prompt')
    output_schema = enrichment_config.get('schema')
    
    # Handle single or multiple output columns
    output_cols = enrichment_config.get('output_columns')
    if not output_cols:
        output_col = enrichment_config.get('output_column')
        output_cols = [output_col] if output_col else (
            enrichment_strategy.output_columns if enrichment_strategy else []
        )
    if isinstance(output_cols, str):
        output_cols = [output_cols]
    output_cols = [column for column in (output_cols or []) if column]

    # Get input columns from config and validate
    input_config = enrichment_config.get('input', {})
    if not input_config or 'input_columns' not in input_config:
        raise ValueError(f"Enrichment '{enrichment_config['name']}' must specify input_columns in its input configuration")
    
    input_cols_raw = input_config.get('input_columns')
    if not input_cols_raw:
        raise ValueError(f"Enrichment '{enrichment_config['name']}' has empty input_columns")
    
    if isinstance(input_cols_raw, str):
        input_cols_raw = [input_cols_raw]
    
    # Parse input columns with character limits (new feature!)
    parsed_input_cols = parse_input_columns_with_limits(input_cols_raw)
    
    # Keep backward compatibility - extract just column names for existing functions
    input_cols = [col_name for col_name, _ in parsed_input_cols]
    
    if verbose:
        logging.info(f"Processing enrichment '{enrichment_config['name']}' with input columns: {input_cols}")
        logging.info(f"Truncate mode: {truncate}")
    
    processed_results = await process_batch(
        results=results,
        prompt=prompt,
        model=model,
        pbar=pbar,
        input_cols=input_cols,
        parsed_input_cols=parsed_input_cols,  # Pass the new parsed columns
        output_cols=output_cols,
        db_path=db_path,
        table=table,
        enrichment_config=enrichment_config,
        output_schema=output_schema,
        system_prompt=system_prompt,
        overwrite=overwrite,
        config=config,
        truncate=truncate,
        verbose=verbose,
        output_table=output_table,
        key_column=key_column,
        enrichment_strategy=enrichment_strategy,
        suppress_progress_messages=is_multi_model,
        project=project,
        separate_output_db=(source_db_path is not None and source_db_path != db_path),
        run_id=run_id,
        query_hash=query_hash,
        dedupe_scope=dedupe_scope,
    )
    
    return processed_results

async def process_translation(row: Dict, input_cols: List[Tuple[str, Optional[slice]]], prompt: str,
                            model: str, semaphore: asyncio.Semaphore, pbar: tqdm,
                            output_cols: List[str], output_schema: Optional[Type[BaseModel]] = None,
                            system_prompt: str = None, chunk_size: int = 3, truncate: bool = False,
                            key_column: str = 'sha1'):
    async with semaphore:
        key_value = row.get(key_column, 'NO_KEY')
        # Generate a unique enrichment_id for this specific LLM call
        row_enrichment_id = str(uuid.uuid4())
        try:
            # Get content from specified input columns instead of hardcoding
            content = ""
            for col, slice_ in input_cols:
                if col in row:
                    content += row[col].strip() + "\n"

            content = content.strip()
            if not content:
                return {
                    'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                    'rowid': row['rowid'],
                    'key_value': key_value,
                    'original': {col: row.get(col, '') for col in output_cols},
                    'updated': {
                        'zh_json': '{}',
                        'en_json': '{}',
                        'english_translation': ''
                    }
                }
            
            # Split into lines and create zh_json
            lines = content.split('\n')
            zh_json = {str(i): line.strip() for i, line in enumerate(lines) if line.strip()}
            
            # Process translations in chunks
            all_translations = {}
            chunk_size = 3  # Small chunks for better translation quality
            
            # Create API semaphore for parallel processing
            api_semaphore = asyncio.Semaphore(DEFAULT_API_SEMAPHORE_LIMIT)  # Use full concurrent capacity
            
            async def process_chunk(chunk_start: int):
                async with api_semaphore:
                    chunk_end = min(chunk_start + chunk_size, len(lines))
                    
                    # Create dynamic model for this chunk's line numbers
                    fields = {
                        str(i): (str, ...) for i in range(chunk_start, chunk_end)
                    }
                    ChunkTranslation = create_model('ChunkTranslation', **fields)
                    
                    # Prepare numbered chunk text
                    chunk_lines = lines[chunk_start:chunk_end]
                    numbered_chunk = "\n".join(f"{i}\t{line}" 
                                             for i, line in enumerate(chunk_lines, start=chunk_start))
                    
                    client = get_openai_client()

                    try:
                        response = await client.beta.chat.completions.parse(
                            model=model,
                            messages=[
                                {"role": "system", "content": "You are a precise Chinese to English translator."},
                                {"role": "user", "content": f"Translate these numbered lines:\n\n{numbered_chunk}"}
                            ],
                            response_format=ChunkTranslation  # Dynamic model specific to this chunk!
                        )
                        
                        result = response.choices[0].message.parsed
                        return dict(result)  # Convert to regular dict for storage
                        
                    except Exception as e:
                        logging.warning(f"Chunk translation failed, retrying once: {e}")
                        # Retry once with a small delay
                        try:
                            await asyncio.sleep(2)
                            client = get_openai_client()
                            response = await client.beta.chat.completions.parse(
                                model=model,
                                messages=[
                                    {"role": "system", "content": "You are a precise Chinese to English translator."},
                                    {"role": "user", "content": f"Translate these numbered lines:\n\n{numbered_chunk}"}
                                ],
                                response_format=ChunkTranslation
                            )
                            result = response.choices[0].message.parsed
                            return dict(result)
                        except Exception as retry_e:
                            logging.error(f"Chunk translation failed after retry: {retry_e}")
                            return {str(i): "" for i in range(chunk_start, chunk_end)}
            
            # Process all chunks concurrently
            tasks = [process_chunk(i) for i in range(0, len(lines), chunk_size)]
            chunk_results = await asyncio.gather(*tasks)
            
            # Combine all chunks
            for chunk_result in chunk_results:
                all_translations.update(chunk_result)
            
            # Return properly structured output
            return {
                'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                'rowid': row['rowid'],
                'key_value': key_value,
                'original': {col: row.get(col, '') for col in output_cols},
                'updated': {
                    'zh_json': json.dumps(zh_json, ensure_ascii=False),
                    'en_json': json.dumps(all_translations, ensure_ascii=False),
                    'english_translation': '\n'.join(all_translations.values())
                }
            }

        except Exception as e:
            logging.error(f"Translation error: {str(e)}")
            return {
                'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                'rowid': row.get('rowid', 'unknown'),
                'key_value': key_value,
                'original': {col: row.get(col, '') for col in output_cols},
                'updated': None,
                'error': str(e)
            }
    
