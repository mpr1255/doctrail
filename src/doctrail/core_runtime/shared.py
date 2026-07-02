import os
import logging
import asyncio
import re
import json
import tempfile
import sqlite3
import random
import uuid
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime
import difflib

import yaml

from ..constants import (
    DEFAULT_TABLE_NAME, DEFAULT_MODEL, ERROR_NO_ENRICHMENTS,
    ERROR_NO_DATABASE, ERROR_ENRICHMENT_NOT_FOUND
)
from ..db_operations import (
    get_db_connection, execute_query,
    create_query_hash, create_run_id, start_enrichment_run, finalize_enrichment_run,
    materialize_run_inputs, update_run_item_statuses, create_run_summary_view,
    create_final_run_view, create_run_view, get_or_create_prompt_id,
    update_enrichment_run_status, ensure_enrichment_batch_jobs_table,
    create_enrichment_batch_job, update_enrichment_batch_job,
    list_enrichment_batch_jobs, get_enrichment_run, get_enrichment_batch_job,
    update_run_item_statuses_by_row_order, persist_enrichment_result,
    plan_existing_enrichment_skips,
    filter_unskipped_input_rows,
    ENRICHMENT_RUN_ITEMS_TABLE,
)
from ..llm_operations import process_enrichment
from ..core_utils import (
    load_config,
    parse_input_columns_with_limits,
    apply_column_limits,
    resolve_enrichment_prompt as _resolve_enrichment_prompt,
)
from ..schema_managers import get_schema_prompt_instructions
from ..utils.logging_config import setup_logging
from ..utils.cost_estimation import (
    estimate_enrichment_cost, format_cost_estimate,
    should_confirm_cost, validate_model, get_model_validation_error
)
from ..utils.progress import create_progress_bar
from ..llm.token_utils import truncate_input_for_model

BATCH_PROVIDER_CONFIGS = {
    "openai": {
        "endpoint": "/v1/chat/completions",
        "completion_window": "24h",
        "max_requests": 50_000,
        "max_request_bytes": 180 * 1024 * 1024,
        "terminal_statuses": {"completed", "failed", "expired", "cancelled"},
        "active_statuses": {"submitted", "validating", "in_progress", "finalizing"},
    },
    "anthropic": {
        "endpoint": "/v1/messages",
        "completion_window": "24h",
        "max_requests": 100_000,
        "max_request_bytes": 256 * 1024 * 1024,
        "terminal_statuses": {"ended"},
        "active_statuses": {"submitted", "in_progress", "canceling"},
    },
    "gemini": {
        "endpoint": "/v1beta/models/{model}:batchGenerateContent",
        "completion_window": "24h",
        "max_requests": 100_000,
        "max_request_bytes": 2_000_000_000,
        "terminal_statuses": {
            "BATCH_STATE_SUCCEEDED",
            "BATCH_STATE_FAILED",
            "BATCH_STATE_CANCELLED",
            "BATCH_STATE_EXPIRED",
        },
        "active_statuses": {"BATCH_STATE_PENDING", "BATCH_STATE_RUNNING"},
    },
}


def _find_active_matching_batch_job(
    *,
    db_path: str,
    provider_name: str,
    provider_config: Dict[str, Any],
    enrichment_name: str,
    model: str,
    prompt_id: str,
    query_hash: str,
    dedupe_scope: str,
    current_run_id: str,
) -> Optional[Dict[str, Any]]:
    """Return an active provider job for the same batch identity, if one exists."""
    active_statuses = sorted(provider_config["active_statuses"])
    for job in list_enrichment_batch_jobs(db_path, statuses=active_statuses):
        if job.get("run_id") == current_run_id:
            continue
        if job.get("provider") != provider_name or job.get("model") != model:
            continue

        run = get_enrichment_run(db_path, job["run_id"])
        if not run:
            continue
        if (
            run.get("enrichment_name") == enrichment_name
            and run.get("model") == model
            and run.get("prompt_id") == prompt_id
            and run.get("query_hash") == query_hash
            and run.get("dedupe_scope") == dedupe_scope
        ):
            return {"job": job, "run": run}
    return None


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

    blocks: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": full_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if final_input_text:
        blocks.append({"type": "text", "text": final_input_text})
    return blocks


class DoctrailError(Exception):
    """Base exception for Doctrail operations."""
    pass

class ConfigurationError(DoctrailError):
    """Configuration errors (missing files, invalid YAML, etc.)"""
    pass

class EnrichmentError(DoctrailError):
    """Enrichment operation errors."""
    pass

class DatabaseError(DoctrailError):
    """Database operation errors."""
    pass

def _batch_custom_id(run_id: str, row_order: int, provider: str = "openai") -> str:
    """Create a stable custom_id for a batch request line."""
    if provider in {"anthropic", "gemini"}:
        return f"row_{row_order}"
    return f"{run_id}:{row_order}"

def _parse_batch_custom_id(custom_id: str, expected_run_id: Optional[str] = None) -> Tuple[str, int]:
    """Parse a batch custom_id back into run_id and row_order."""
    if custom_id.startswith("row_"):
        return expected_run_id or "", int(custom_id.split("_", 1)[1])
    if ":" not in custom_id:
        raise ValueError(f"Invalid batch custom_id: {custom_id}")
    run_id, row_order_text = custom_id.rsplit(":", 1)
    if expected_run_id and run_id != expected_run_id:
        raise ValueError(f"Batch custom_id run mismatch: {custom_id}")
    return run_id, int(row_order_text)

def _extract_batch_attr(value: Any, attr: str, default: Any = None) -> Any:
    """Read an attribute from either a dict or an object."""
    if isinstance(value, dict):
        return value.get(attr, default)
    return getattr(value, attr, default)

def _load_json_object(value: Any) -> Dict[str, Any]:
    """Parse a dict-shaped JSON payload, returning an empty dict on failure."""
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}

def _json_safe(value: Any) -> Any:
    """Recursively coerce SDK objects into JSON-serializable structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(sub_value) for key, sub_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(mode="json"))
        except TypeError:
            return _json_safe(model_dump())

    dict_method = getattr(value, "dict", None)
    if callable(dict_method):
        return _json_safe(dict_method())

    value_dict = getattr(value, "__dict__", None)
    if isinstance(value_dict, dict) and value_dict:
        return {
            str(key): _json_safe(sub_value)
            for key, sub_value in value_dict.items()
            if not str(key).startswith("_")
        }

    return str(value)


def _batch_template_context(config_data: Dict[str, Any], model: str) -> Dict[str, str]:
    """Build scalar prompt variables from execution context and YAML config."""
    context = {"model": model}
    if config_data and config_data.get("default_table"):
        context["table"] = str(config_data["default_table"])
    for key, value in (config_data or {}).items():
        if key.startswith("_") or isinstance(value, (dict, list, tuple, set)):
            continue
        if value is None:
            continue
        context.setdefault(key, str(value))
    return context

def _batch_request_counts_to_dict(request_counts: Any) -> Dict[str, int]:
    """Normalize provider batch request counts into a plain dict."""
    if not request_counts:
        return {}

    normalized: Dict[str, int] = {}
    for key in (
        "total",
        "completed",
        "failed",
        "processing",
        "succeeded",
        "errored",
        "canceled",
        "expired",
    ):
        value = _extract_batch_attr(request_counts, key)
        if value is None:
            continue
        normalized[key] = int(value)
    return normalized

def _summarize_batch_request_outcomes(jobs: List[Dict[str, Any]]) -> Dict[str, int]:
    """Aggregate request-level outcome counts across provider batch jobs."""
    totals = {
        "succeeded": 0,
        "errored": 0,
        "canceled": 0,
        "expired": 0,
    }

    for job in jobs:
        metadata = _load_json_object(job.get("metadata"))
        request_counts = metadata.get("request_counts")
        if isinstance(request_counts, dict):
            for key in totals:
                totals[key] += int(request_counts.get(key, 0) or 0)
            continue

        totals["succeeded"] += int(job.get("completed_count", 0) or 0)
        failed_count = int(job.get("failed_count", 0) or 0)
        if not failed_count:
            continue
        if job.get("status") == "cancelled":
            totals["canceled"] += failed_count
        elif job.get("status") == "expired":
            totals["expired"] += failed_count
        else:
            totals["errored"] += failed_count

    return totals

def _summarize_provider_batch_usage(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate provider-reported usage persisted on batch jobs."""
    totals = {
        "job_count_with_usage": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost": 0.0,
    }

    for job in jobs:
        metadata = _load_json_object(job.get("metadata"))
        provider_usage = metadata.get("provider_usage")
        if not isinstance(provider_usage, dict):
            continue
        totals["job_count_with_usage"] += 1
        totals["input_tokens"] += int(provider_usage.get("input_tokens", 0) or 0)
        totals["cached_input_tokens"] += int(provider_usage.get("cached_input_tokens", 0) or 0)
        totals["cache_creation_input_tokens"] += int(provider_usage.get("cache_creation_input_tokens", 0) or 0)
        totals["output_tokens"] += int(provider_usage.get("output_tokens", 0) or 0)
        totals["estimated_cost"] += float(provider_usage.get("estimated_cost", 0.0) or 0.0)

    return totals

def _get_batch_provider_name(provider: Any) -> str:
    """Return the registered batch backend name for a provider instance."""
    from ..llm_providers.openai_provider import OpenAIProvider
    from ..llm_providers.anthropic_provider import AnthropicProvider
    from ..llm_providers.gemini_provider import GeminiProvider

    if isinstance(provider, OpenAIProvider):
        return "openai"
    if isinstance(provider, AnthropicProvider):
        return "anthropic"
    if isinstance(provider, GeminiProvider):
        return "gemini"
    raise EnrichmentError(f"Unsupported batch provider: {type(provider).__name__}")

def _normalize_execution_mode(execution_mode: str) -> str:
    """Normalize legacy execution mode names to the current public contract."""
    return "batch" if execution_mode == "openai-batch" else execution_mode

def _is_batch_execution_mode(execution_mode: str) -> bool:
    return _normalize_execution_mode(execution_mode) == "batch"

def _get_batch_provider(model: str):
    """Return a direct provider that supports Doctrail's batch submission flow."""
    from ..llm_providers.factory import get_llm_provider

    provider = get_llm_provider(model)
    provider_name = _get_batch_provider_name(provider)
    if not getattr(provider, "supports_batch", lambda: False)():
        raise EnrichmentError(
            f"Batch mode is only supported for direct OpenAI, Anthropic, and Gemini models. Got: {model}"
        )
    if provider_name not in BATCH_PROVIDER_CONFIGS:
        raise EnrichmentError(f"Unsupported batch provider '{provider_name}' for model {model}")
    return provider

def _get_openai_batch_provider(model: str):
    """Backward-compatible alias for the old OpenAI-named batch helper."""
    return _get_batch_provider(model)

def _get_batch_schema_compatibility_issues(
    provider: Any,
    pydantic_model: Optional[Any],
) -> List[Dict[str, Any]]:
    """Ask the provider for any batch structured-output compatibility issues."""
    if pydantic_model is None:
        return []

    issue_getter = getattr(provider, "get_batch_schema_compatibility_issues", None)
    if not callable(issue_getter):
        return []

    issues = issue_getter(pydantic_model) or []
    if isinstance(issues, dict):
        return [issues]
    return [issue for issue in issues if isinstance(issue, dict)]

def _validate_batch_schema_compatibility(
    *,
    provider: Any,
    enrichment_name: str,
    model: str,
    pydantic_model: Optional[Any],
) -> None:
    """Surface provider-specific structured-output compatibility issues before batch submission."""
    provider_name = _get_batch_provider_name(provider)
    for issue in _get_batch_schema_compatibility_issues(provider, pydantic_model):
        level = str(issue.get("level", "warning")).lower()
        message = issue.get("message") or "Unknown provider schema compatibility issue."
        formatted = (
            f"{provider_name} batch schema compatibility for enrichment '{enrichment_name}' "
            f"[{model}]: {message}"
        )
        if level == "error":
            raise EnrichmentError(formatted)
        logging.warning(formatted)

def _job_batch_terminal_statuses(job: Dict[str, Any]) -> set[str]:
    """Return the terminal statuses for a persisted batch job."""
    provider_name = job.get("provider") or "openai"
    config = BATCH_PROVIDER_CONFIGS.get(provider_name, BATCH_PROVIDER_CONFIGS["openai"])
    return set(config["terminal_statuses"])

def _job_batch_is_terminal(job: Dict[str, Any]) -> bool:
    """Return True when the persisted batch job no longer needs provider polling."""
    return (job.get("status") or "") in _job_batch_terminal_statuses(job)

def _render_batch_messages(
    *,
    row: Dict[str, Any],
    prompt_text: str,
    system_prompt_text: str,
    input_columns: List[str],
    key_column: str,
    model: str,
    truncate: bool,
    output_schema: Optional[Any],
    config_data: Dict[str, Any],
    structured_output: bool,
) -> Tuple[str, str, List[Dict[str, str]]]:
    """Render the exact messages that will be submitted for one row."""
    parsed_input_cols = parse_input_columns_with_limits(input_columns)
    limited_data = apply_column_limits(row, parsed_input_cols)
    templated_prompt = prompt_text
    skip_cols = {"rowid", key_column}

    for col, _ in parsed_input_cols:
        if col in skip_cols:
            continue
        col_value = limited_data.get(col, "")
        if f"{{{col}}}" in templated_prompt:
            templated_prompt = templated_prompt.replace(f"{{{col}}}", str(col_value))
        if "." in col:
            _, column_only = col.split(".", 1)
            if f"{{{column_only}}}" in templated_prompt:
                templated_prompt = templated_prompt.replace(f"{{{column_only}}}", str(col_value))

    for var_name, var_value in _batch_template_context(config_data, model).items():
        placeholder = f"{{{var_name}}}"
        if placeholder in templated_prompt:
            templated_prompt = templated_prompt.replace(placeholder, var_value)

    full_prompt = templated_prompt
    if output_schema and config_data and not structured_output:
        schema_instructions = get_schema_prompt_instructions(config_data, output_schema)
        if schema_instructions:
            full_prompt = templated_prompt + "\n\n" + schema_instructions

    input_text = "\n".join(
        f"{col}: {limited_data.get(col, '')}"
        for col, _ in parsed_input_cols
        if col not in skip_cols
    )
    if truncate:
        final_input_text, _ = truncate_input_for_model(full_prompt, input_text, model)
    else:
        final_input_text = input_text

    full_prompt_content = full_prompt
    if final_input_text:
        full_prompt_content = full_prompt + "\n\n" + final_input_text
    message_content = _anthropic_cached_user_content(
        model,
        full_prompt,
        final_input_text,
        full_prompt_content,
    )

    messages: List[Dict[str, Any]] = []
    if system_prompt_text:
        messages.append({"role": "system", "content": system_prompt_text})
    messages.append({"role": "user", "content": message_content})

    return str(row.get(key_column, "")), full_prompt_content, messages

def _load_run_metadata(run_row: Dict[str, Any]) -> Dict[str, Any]:
    """Parse JSON metadata stored on a run row."""
    metadata = run_row.get("metadata")
    if not metadata:
        return {}
    if isinstance(metadata, dict):
        return metadata
    try:
        return json.loads(metadata)
    except json.JSONDecodeError:
        return {}

def _summarize_run_item_statuses(db_path: str, run_id: str) -> Dict[str, int]:
    """Count materialized run items by status."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT status, COUNT(*)
            FROM {ENRICHMENT_RUN_ITEMS_TABLE}
            WHERE run_id = ?
            GROUP BY status
        """, (run_id,))
        counts = {status: count for status, count in cursor.fetchall()}
    return {
        "candidate": counts.get("candidate", 0),
        "processed": counts.get("processed", 0),
        "skipped": counts.get("skipped", 0),
        "insufficient": counts.get("insufficient", 0),
        "error": counts.get("error", 0),
    }

async def _persist_batch_result(
    *,
    db_path: str,
    run_row: Dict[str, Any],
    project: Optional[str],
    enrichment_name: str,
    prompt_id: str,
    query_hash: str,
    key_value: str,
    updated: Any,
    raw_json: str,
    full_prompt: str,
    usage: Optional[Dict[str, Any]],
    enrichment_id: str,
    overwrite: bool,
    projection_output_fields: Optional[List[str]] = None,
) -> None:
    """Persist one reconciled batch result into audit and normalized enrichments."""
    await asyncio.to_thread(
        persist_enrichment_result,
        db_path=db_path,
        key_value=key_value,
        enrichment_name=enrichment_name,
        updated=updated,
        model=run_row["model"],
        enrichment_id=enrichment_id,
        prompt_id=prompt_id,
        full_prompt=full_prompt,
        usage=usage,
        run_id=run_row["run_id"],
        query_hash=query_hash,
        project=project,
        overwrite=overwrite,
        raw_json=raw_json,
        projection_output_fields=projection_output_fields,
    )

async def _submit_batch_jobs(
    *,
    db_path: str,
    run_id: str,
    rows_to_process: List[Dict[str, Any]],
    enrichment_config: Dict[str, Any],
    config_data: Dict[str, Any],
    model: str,
    key_column: str,
    prompt_text: str,
    system_prompt_text: str,
    truncate: bool,
    pydantic_model: Optional[Any],
    provider: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Submit one or more provider-native batch jobs for a run."""
    provider = provider or _get_batch_provider(model)
    provider_name = _get_batch_provider_name(provider)
    provider_config = BATCH_PROVIDER_CONFIGS[provider_name]
    input_columns = enrichment_config.get("input", {}).get("input_columns", [])
    output_schema = enrichment_config.get("schema")
    structured_output = pydantic_model is not None

    current_run = get_enrichment_run(db_path, run_id)
    active_match = _find_active_matching_batch_job(
        db_path=db_path,
        provider_name=provider_name,
        provider_config=provider_config,
        enrichment_name=enrichment_config["name"],
        model=model,
        prompt_id=(current_run or {}).get("prompt_id") or "",
        query_hash=(current_run or {}).get("query_hash") or "",
        dedupe_scope=(current_run or {}).get("dedupe_scope") or "query",
        current_run_id=run_id,
    )
    if active_match:
        job = active_match["job"]
        run = active_match["run"]
        raise EnrichmentError(
            "Matching provider batch already active: "
            f"run_id={run['run_id']} provider_batch_id={job.get('provider_batch_id')} "
            f"status={job.get('status')}. Poll it with "
            f"`doctrail batch watch --run-id {run['run_id']}`."
        )

    shards: List[Dict[str, Any]] = []
    current_requests: List[Any] = []
    current_bytes = 0
    shard_start = 0

    for row_order, row in enumerate(rows_to_process):
        _, _, messages = _render_batch_messages(
            row=row,
            prompt_text=prompt_text,
            system_prompt_text=system_prompt_text,
            input_columns=input_columns,
            key_column=key_column,
            model=model,
            truncate=truncate,
            output_schema=output_schema,
            config_data=config_data,
            structured_output=structured_output,
        )
        if provider_name == "openai":
            request_payload = {
                "custom_id": _batch_custom_id(run_id, row_order, provider_name),
                "method": "POST",
                "url": provider_config["endpoint"],
                "body": provider.build_batch_chat_request(
                    messages=messages,
                    pydantic_model=pydantic_model,
                    temperature=0.0,
                    reasoning_effort=enrichment_config.get("reasoning_effort"),
                ),
            }
        elif provider_name == "anthropic":
            request_payload = {
                "custom_id": _batch_custom_id(run_id, row_order, provider_name),
                "params": provider.build_batch_message_request(
                    messages=messages,
                    pydantic_model=pydantic_model,
                    temperature=0.0,
                ),
            }
        elif provider_name == "gemini":
            request_payload = provider.build_batch_generate_content_file_request(
                key=_batch_custom_id(run_id, row_order, provider_name),
                messages=messages,
                pydantic_model=pydantic_model,
                temperature=0.0,
            )
        else:
            raise EnrichmentError(f"Unsupported batch provider '{provider_name}'")

        request_bytes = len(json.dumps(request_payload, ensure_ascii=False).encode("utf-8"))

        if request_bytes > provider_config["max_request_bytes"]:
            raise EnrichmentError(
                f"Single batch request exceeded the {provider_name} batch request size limit for row {row_order}"
            )

        if current_requests and (
            len(current_requests) >= provider_config["max_requests"]
            or current_bytes + request_bytes > provider_config["max_request_bytes"]
        ):
            shards.append({
                "start_row_order": shard_start,
                "end_row_order": row_order - 1,
                "requests": list(current_requests),
                "bytes": current_bytes,
            })
            current_requests = []
            current_bytes = 0
            shard_start = row_order

        current_requests.append(request_payload)
        current_bytes += request_bytes

    if current_requests:
        shards.append({
            "start_row_order": shard_start,
            "end_row_order": shard_start + len(current_requests) - 1,
            "requests": list(current_requests),
            "bytes": current_bytes,
        })

    ensure_enrichment_batch_jobs_table(db_path)
    created_jobs: List[Dict[str, Any]] = []
    for shard_index, shard in enumerate(shards):
        if provider_name == "openai":
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as handle:
                    handle.write("\n".join(
                        json.dumps(request, ensure_ascii=False)
                        for request in shard["requests"]
                    ))
                    handle.write("\n")
                    temp_path = handle.name

                with open(temp_path, "rb") as file_handle:
                    uploaded_file = await provider.client.files.create(file=file_handle, purpose="batch")

                batch_obj = await provider.client.batches.create(
                    completion_window=provider_config["completion_window"],
                    endpoint=provider_config["endpoint"],
                    input_file_id=_extract_batch_attr(uploaded_file, "id"),
                    metadata={
                        "run_id": run_id[:64],
                        "shard_index": str(shard_index),
                        "enrichment": enrichment_config["name"][:64],
                    },
                )

                created_jobs.append(create_enrichment_batch_job(
                    db_path,
                    run_id=run_id,
                    provider=provider_name,
                    endpoint=provider_config["endpoint"],
                    model=model,
                    input_file_id=_extract_batch_attr(uploaded_file, "id"),
                    provider_batch_id=_extract_batch_attr(batch_obj, "id"),
                    status=_extract_batch_attr(batch_obj, "status", "submitted"),
                    request_count=len(shard["requests"]),
                    input_file_bytes=shard["bytes"],
                    metadata={
                        "shard_index": shard_index,
                        "start_row_order": shard["start_row_order"],
                        "end_row_order": shard["end_row_order"],
                    },
                ))
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
            continue

        if provider_name == "anthropic":
            batch_obj = await provider.client.messages.batches.create(
                requests=shard["requests"],
            )
            created_jobs.append(create_enrichment_batch_job(
                db_path,
                run_id=run_id,
                provider=provider_name,
                endpoint=provider_config["endpoint"],
                model=model,
                input_file_id=f"inline:{run_id}:{shard_index}",
                provider_batch_id=_extract_batch_attr(batch_obj, "id"),
                status=_extract_batch_attr(batch_obj, "processing_status", "in_progress"),
                request_count=len(shard["requests"]),
                input_file_bytes=shard["bytes"],
                metadata={
                    "shard_index": shard_index,
                    "start_row_order": shard["start_row_order"],
                    "end_row_order": shard["end_row_order"],
                    "request_counts": _batch_request_counts_to_dict(
                        _extract_batch_attr(batch_obj, "request_counts")
                    ),
                    "results_url": _extract_batch_attr(batch_obj, "results_url"),
                },
            ))
            continue

        if provider_name == "gemini":
            display_name = f"doctrail-{enrichment_config['name'][:32]}-{run_id[:8]}-{shard_index}"
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as handle:
                    handle.write("\n".join(
                        json.dumps(request, ensure_ascii=False)
                        for request in shard["requests"]
                    ))
                    handle.write("\n")
                    temp_path = handle.name

                uploaded_file = await provider.upload_batch_requests_file(
                    temp_path,
                    display_name=display_name,
                )
                input_file_id = (
                    _extract_batch_attr(uploaded_file, "name")
                    or _extract_batch_attr(uploaded_file, "id")
                )
                batch_obj = await provider.create_batch_job(
                    input_file_id,
                    display_name=display_name,
                )
                created_jobs.append(create_enrichment_batch_job(
                    db_path,
                    run_id=run_id,
                    provider=provider_name,
                    endpoint=provider_config["endpoint"].format(model=model),
                    model=model,
                    input_file_id=input_file_id,
                    provider_batch_id=_extract_batch_attr(batch_obj, "name"),
                    status=_extract_batch_attr(batch_obj, "state", "BATCH_STATE_PENDING"),
                    request_count=len(shard["requests"]),
                    input_file_bytes=shard["bytes"],
                    metadata={
                        "display_name": display_name,
                        "input_mode": "file",
                        "shard_index": shard_index,
                        "start_row_order": shard["start_row_order"],
                        "end_row_order": shard["end_row_order"],
                        "batch_stats": _extract_batch_attr(batch_obj, "batch_stats") or {},
                    },
                ))
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
            continue

        raise EnrichmentError(f"Unsupported batch provider '{provider_name}'")

    return created_jobs

__all__ = [

    'DoctrailError',

    'ConfigurationError',

    'EnrichmentError',

    'DatabaseError',

    '_resolve_enrichment_prompt',

    '_batch_custom_id',

    '_parse_batch_custom_id',

    '_extract_batch_attr',

    '_load_json_object',

    '_json_safe',

    '_batch_request_counts_to_dict',

    '_summarize_batch_request_outcomes',

    '_summarize_provider_batch_usage',

    '_get_batch_provider_name',

    '_normalize_execution_mode',

    '_is_batch_execution_mode',

    '_get_batch_provider',

    '_get_openai_batch_provider',

    '_get_batch_schema_compatibility_issues',

    '_validate_batch_schema_compatibility',

    '_job_batch_terminal_statuses',

    '_job_batch_is_terminal',

    '_render_batch_messages',

    '_load_run_metadata',

    '_summarize_run_item_statuses',

    '_persist_batch_result',

    '_submit_batch_jobs',

    'BATCH_PROVIDER_CONFIGS',

]
