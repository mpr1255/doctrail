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
    ENRICHMENT_AUDIT_TABLE,
    ENRICHMENT_RUN_ITEMS_TABLE,
)
from ..llm_operations import process_enrichment
from ..core_utils import load_config, parse_input_columns_with_limits, apply_column_limits
from ..schema_managers import get_schema_prompt_instructions
from ..utils.logging_config import setup_logging
from ..utils.cost_estimation import (
    estimate_enrichment_cost, format_cost_estimate,
    should_confirm_cost, validate_model, get_model_validation_error
)
from ..utils.progress import create_progress_bar
from ..llm.token_utils import truncate_input_for_model

from .shared import *


async def _reconcile_openai_batch_job(
    db_path: str,
    job_row: Dict[str, Any],
    run_row: Dict[str, Any],
) -> Dict[str, int]:
    """Download and reconcile one completed provider batch job into normalized storage."""
    metadata = _load_run_metadata(run_row)
    job_metadata = _load_json_object(job_row.get("metadata"))

    enrichment_config = metadata.get("enrichment_config") or {}
    if not enrichment_config:
        raise EnrichmentError(f"Run {run_row['run_id']} is missing enrichment_config metadata")

    from ..enrichment_config import prepare_enrichment_for_processing

    strategy, config_errors = prepare_enrichment_for_processing(
        enrichment_config,
        metadata.get("input_table") or run_row.get("source_name") or DEFAULT_TABLE_NAME,
        config_key_column=run_row.get("key_column"),
    )
    if config_errors:
        raise EnrichmentError(
            f"Batch reconciliation config errors for run {run_row['run_id']}: {', '.join(config_errors)}"
        )
    projection_output_fields = [field for field in (strategy.output_columns or []) if field]

    provider = _get_batch_provider(run_row["model"])
    provider_name = job_row.get("provider") or _get_batch_provider_name(provider)
    nullable_pydantic_model = None
    if getattr(strategy, "pydantic_model", None) is not None and enrichment_config.get("schema"):
        from ..pydantic_schema import create_pydantic_model_from_schema

        nullable_pydantic_model = create_pydantic_model_from_schema(
            enrichment_config["schema"],
            f"{strategy.pydantic_model.__name__}BatchNullable",
            all_fields_optional=True,
        )

    row_start = int(job_metadata.get("start_row_order", 0))
    row_end = int(job_metadata.get("end_row_order", -1))
    rows_by_order: Dict[int, Dict[str, Any]] = {}
    with get_db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT row_order, key_value, row_json
            FROM {ENRICHMENT_RUN_ITEMS_TABLE}
            WHERE run_id = ? AND row_order BETWEEN ? AND ?
            ORDER BY row_order
        """, (run_row["run_id"], row_start, row_end))
        for row in cursor.fetchall():
            row_payload = json.loads(row["row_json"]) if row["row_json"] else {}
            rows_by_order[int(row["row_order"])] = {
                "key_value": row["key_value"],
                "row": row_payload,
            }

    row_statuses: Dict[int, str] = {}
    seen_orders: set[int] = set()

    async def _store_row_error(row_order: int, custom_id: str, payload: Dict[str, Any]) -> None:
        item = rows_by_order[row_order]
        key_value, full_prompt, _ = _render_batch_messages(
            row=item["row"],
            prompt_text=metadata.get("effective_prompt", enrichment_config.get("prompt", "")),
            system_prompt_text=metadata.get("effective_system_prompt", enrichment_config.get("system_prompt", "")),
            input_columns=enrichment_config.get("input", {}).get("input_columns", []),
            key_column=run_row["key_column"],
            model=run_row["model"],
            truncate=bool(metadata.get("truncate", False)),
            output_schema=enrichment_config.get("schema"),
            config_data={},
            structured_output=getattr(strategy, "pydantic_model", None) is not None,
        )
        await _persist_batch_result(
            db_path=db_path,
            run_row=run_row,
            project=run_row.get("project"),
            enrichment_name=run_row["enrichment_name"],
            prompt_id=run_row.get("prompt_id"),
            query_hash=run_row.get("query_hash"),
            key_value=key_value,
            updated=None,
            raw_json=json.dumps(_json_safe(payload), ensure_ascii=False),
            full_prompt=full_prompt,
            usage=None,
            enrichment_id=create_query_hash(f"{run_row['run_id']}|{custom_id}|error"),
            overwrite=bool(metadata.get("overwrite", False)),
        )

    if provider_name == "openai":
        output_file_id = job_row.get("output_file_id")
        if output_file_id:
            output_text = await provider.client.files.retrieve_content(output_file_id)
            for raw_line in output_text.splitlines():
                if not raw_line.strip():
                    continue
                line = json.loads(raw_line)
                custom_id = line.get("custom_id")
                if not custom_id:
                    continue
                _, row_order = _parse_batch_custom_id(custom_id, run_row["run_id"])
                if row_order not in rows_by_order:
                    continue

                item = rows_by_order[row_order]
                key_value, full_prompt, _ = _render_batch_messages(
                    row=item["row"],
                    prompt_text=metadata.get("effective_prompt", enrichment_config.get("prompt", "")),
                    system_prompt_text=metadata.get("effective_system_prompt", enrichment_config.get("system_prompt", "")),
                    input_columns=enrichment_config.get("input", {}).get("input_columns", []),
                    key_column=run_row["key_column"],
                    model=run_row["model"],
                    truncate=bool(metadata.get("truncate", False)),
                    output_schema=enrichment_config.get("schema"),
                    config_data={},
                    structured_output=getattr(strategy, "pydantic_model", None) is not None,
                )

                response_block = line.get("response") or {}
                status_code = response_block.get("status_code", 200)
                if status_code >= 400:
                    await _store_row_error(row_order, custom_id, response_block.get("body") or line)
                    row_statuses[row_order] = "error"
                    seen_orders.add(row_order)
                    continue

                try:
                    updated, usage_obj, raw_json = provider.parse_batch_chat_response(
                        response_block.get("body") or {},
                        getattr(strategy, "pydantic_model", None),
                        nullable_pydantic_model=nullable_pydantic_model,
                    )
                except Exception as exc:
                    await _store_row_error(
                        row_order,
                        custom_id,
                        {
                            "error": str(exc),
                            "response": response_block.get("body") or line,
                        },
                    )
                    row_statuses[row_order] = "error"
                    seen_orders.add(row_order)
                    continue
                usage = None
                if usage_obj:
                    usage = {
                        "input_tokens": usage_obj.input_tokens,
                        "output_tokens": usage_obj.output_tokens,
                        "cached_input_tokens": getattr(usage_obj, "cached_input_tokens", 0) or 0,
                        "cache_creation_input_tokens": getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
                        "estimated_cost": usage_obj.estimate_cost(),
                    }
                await _persist_batch_result(
                    db_path=db_path,
                    run_row=run_row,
                    project=run_row.get("project"),
                    enrichment_name=run_row["enrichment_name"],
                    prompt_id=run_row.get("prompt_id"),
                    query_hash=run_row.get("query_hash"),
                    key_value=key_value,
                    updated=updated,
                    raw_json=raw_json,
                    full_prompt=full_prompt,
                    usage=usage,
                    enrichment_id=create_query_hash(f"{run_row['run_id']}|{custom_id}|success"),
                    overwrite=bool(metadata.get("overwrite", False)),
                    projection_output_fields=projection_output_fields,
                )
                row_statuses[row_order] = "processed"
                seen_orders.add(row_order)

        error_file_id = job_row.get("error_file_id")
        if error_file_id:
            error_text = await provider.client.files.retrieve_content(error_file_id)
            for raw_line in error_text.splitlines():
                if not raw_line.strip():
                    continue
                line = json.loads(raw_line)
                custom_id = line.get("custom_id")
                if not custom_id:
                    continue
                _, row_order = _parse_batch_custom_id(custom_id, run_row["run_id"])
                if row_order not in rows_by_order or row_order in seen_orders:
                    continue
                await _store_row_error(row_order, custom_id, line.get("error") or line)
                row_statuses[row_order] = "error"
                seen_orders.add(row_order)
    elif provider_name == "anthropic":
        results_decoder = await provider.client.messages.batches.results(job_row["provider_batch_id"])
        async for line in results_decoder:
            custom_id = _extract_batch_attr(line, "custom_id")
            if not custom_id:
                continue
            _, row_order = _parse_batch_custom_id(custom_id, run_row["run_id"])
            if row_order not in rows_by_order:
                continue

            result_block = _extract_batch_attr(line, "result") or {}
            result_type = _extract_batch_attr(result_block, "type")
            if result_type != "succeeded":
                error_payload = {"type": result_type}
                if result_type == "errored":
                    error_payload = _extract_batch_attr(result_block, "error") or error_payload
                await _store_row_error(row_order, custom_id, error_payload)
                row_statuses[row_order] = "error"
                seen_orders.add(row_order)
                continue

            item = rows_by_order[row_order]
            key_value, full_prompt, _ = _render_batch_messages(
                row=item["row"],
                prompt_text=metadata.get("effective_prompt", enrichment_config.get("prompt", "")),
                system_prompt_text=metadata.get("effective_system_prompt", enrichment_config.get("system_prompt", "")),
                input_columns=enrichment_config.get("input", {}).get("input_columns", []),
                key_column=run_row["key_column"],
                model=run_row["model"],
                truncate=bool(metadata.get("truncate", False)),
                output_schema=enrichment_config.get("schema"),
                config_data={},
                structured_output=getattr(strategy, "pydantic_model", None) is not None,
            )

            message_payload = _extract_batch_attr(result_block, "message") or {}
            try:
                updated, usage_obj, raw_json = provider.parse_batch_message_response(
                    message_payload,
                    getattr(strategy, "pydantic_model", None),
                    nullable_pydantic_model=nullable_pydantic_model,
                )
            except Exception as exc:
                await _store_row_error(
                    row_order,
                    custom_id,
                    {
                        "error": str(exc),
                        "response": message_payload,
                    },
                )
                row_statuses[row_order] = "error"
                seen_orders.add(row_order)
                continue

            usage = None
            if usage_obj:
                usage = {
                    "input_tokens": usage_obj.input_tokens,
                    "output_tokens": usage_obj.output_tokens,
                    "cached_input_tokens": getattr(usage_obj, "cached_input_tokens", 0) or 0,
                    "cache_creation_input_tokens": getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
                    "estimated_cost": usage_obj.estimate_cost(),
                }
            await _persist_batch_result(
                db_path=db_path,
                run_row=run_row,
                project=run_row.get("project"),
                enrichment_name=run_row["enrichment_name"],
                prompt_id=run_row.get("prompt_id"),
                query_hash=run_row.get("query_hash"),
                key_value=key_value,
                updated=updated,
                raw_json=raw_json,
                full_prompt=full_prompt,
                usage=usage,
                enrichment_id=create_query_hash(f"{run_row['run_id']}|{custom_id}|success"),
                overwrite=bool(metadata.get("overwrite", False)),
                projection_output_fields=projection_output_fields,
            )
            row_statuses[row_order] = "processed"
            seen_orders.add(row_order)
    elif provider_name == "gemini":
        batch_obj = await provider.get_batch_job(job_row["provider_batch_id"])
        dest = _extract_batch_attr(batch_obj, "dest") or {}
        output_file_id = job_row.get("output_file_id") or _extract_batch_attr(dest, "file_name")
        if output_file_id:
            output_text = await provider.download_batch_result_file(output_file_id)
            response_lines = [
                json.loads(raw_line)
                for raw_line in output_text.splitlines()
                if raw_line.strip()
            ]
        else:
            response_lines = _extract_batch_attr(dest, "inlined_responses") or []

        for index, line in enumerate(response_lines):
            metadata_block = _extract_batch_attr(line, "metadata") or {}
            custom_id = _extract_batch_attr(line, "key") or _extract_batch_attr(metadata_block, "key")
            if custom_id:
                _, row_order = _parse_batch_custom_id(custom_id, run_row["run_id"])
            else:
                row_order = row_start + index
                custom_id = _batch_custom_id(run_row["run_id"], row_order, provider_name)

            if row_order not in rows_by_order or row_order in seen_orders:
                continue

            error_payload = _extract_batch_attr(line, "error")
            if error_payload:
                if not isinstance(error_payload, dict):
                    error_payload = {"error": error_payload}
                await _store_row_error(row_order, custom_id, error_payload)
                row_statuses[row_order] = "error"
                seen_orders.add(row_order)
                continue

            item = rows_by_order[row_order]
            key_value, full_prompt, _ = _render_batch_messages(
                row=item["row"],
                prompt_text=metadata.get("effective_prompt", enrichment_config.get("prompt", "")),
                system_prompt_text=metadata.get("effective_system_prompt", enrichment_config.get("system_prompt", "")),
                input_columns=enrichment_config.get("input", {}).get("input_columns", []),
                key_column=run_row["key_column"],
                model=run_row["model"],
                truncate=bool(metadata.get("truncate", False)),
                output_schema=enrichment_config.get("schema"),
                config_data={},
                structured_output=getattr(strategy, "pydantic_model", None) is not None,
            )

            response_payload = _extract_batch_attr(line, "response") or {}
            if not response_payload:
                await _store_row_error(
                    row_order,
                    custom_id,
                    {"error": "Gemini batch response item was missing a response payload"},
                )
                row_statuses[row_order] = "error"
                seen_orders.add(row_order)
                continue

            try:
                updated, usage_obj, raw_json = provider.parse_batch_generate_content_response(
                    response_payload,
                    getattr(strategy, "pydantic_model", None),
                    nullable_pydantic_model=nullable_pydantic_model,
                )
            except Exception as exc:
                await _store_row_error(
                    row_order,
                    custom_id,
                    {
                        "error": str(exc),
                        "response": response_payload,
                    },
                )
                row_statuses[row_order] = "error"
                seen_orders.add(row_order)
                continue

            usage = None
            if usage_obj:
                usage = {
                    "input_tokens": usage_obj.input_tokens,
                    "output_tokens": usage_obj.output_tokens,
                    "cached_input_tokens": getattr(usage_obj, "cached_input_tokens", 0) or 0,
                    "cache_creation_input_tokens": getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
                    "estimated_cost": usage_obj.estimate_cost(),
                }
            await _persist_batch_result(
                db_path=db_path,
                run_row=run_row,
                project=run_row.get("project"),
                enrichment_name=run_row["enrichment_name"],
                prompt_id=run_row.get("prompt_id"),
                query_hash=run_row.get("query_hash"),
                key_value=key_value,
                updated=updated,
                raw_json=raw_json,
                full_prompt=full_prompt,
                usage=usage,
                enrichment_id=create_query_hash(f"{run_row['run_id']}|{custom_id}|success"),
                overwrite=bool(metadata.get("overwrite", False)),
                projection_output_fields=projection_output_fields,
            )
            row_statuses[row_order] = "processed"
            seen_orders.add(row_order)
    else:
        raise EnrichmentError(f"Unsupported batch provider '{provider_name}'")

    for row_order in sorted(rows_by_order):
        if row_order in seen_orders:
            continue
        custom_id = _batch_custom_id(run_row["run_id"], row_order, provider_name)
        await _store_row_error(
            row_order,
            custom_id,
            {"error": "Batch job finished without returning output for this request"},
        )
        row_statuses[row_order] = "error"

    if row_statuses:
        update_run_item_statuses_by_row_order(db_path, run_row["run_id"], row_statuses)

    processed_count = sum(1 for status in row_statuses.values() if status == "processed")
    error_count = sum(1 for status in row_statuses.values() if status == "error")
    update_enrichment_batch_job(
        db_path,
        int(job_row["id"]),
        completed_count=processed_count,
        failed_count=error_count,
        reconciled_at=datetime.now().isoformat(),
    )
    return {"processed": processed_count, "errors": error_count}

def _finalize_batch_run_if_ready(db_path: str, run_id: str) -> Optional[Dict[str, Any]]:
    """Finalize a batch-backed run once all shard jobs are terminal and reconciled."""
    run_row = get_enrichment_run(db_path, run_id)
    if not run_row:
        return None

    jobs = list_enrichment_batch_jobs(db_path, run_id=run_id)
    if not jobs:
        return None

    if any(not _job_batch_is_terminal(job) for job in jobs):
        return None
    if any(not job.get("reconciled_at") for job in jobs):
        return None

    counts = _summarize_run_item_statuses(db_path, run_id)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT
                COALESCE(SUM(input_tokens), 0),
                COALESCE(SUM(cached_input_tokens), 0),
                COALESCE(SUM(cache_creation_input_tokens), 0),
                COALESCE(SUM(output_tokens), 0),
                COALESCE(SUM(estimated_cost), 0.0)
            FROM {ENRICHMENT_AUDIT_TABLE}
            WHERE run_id = ?
        """, (run_id,))
        (
            audit_input_tokens,
            audit_cached_input_tokens,
            audit_cache_creation_input_tokens,
            audit_output_tokens,
            audit_estimated_cost,
        ) = cursor.fetchone()

    provider_usage = _summarize_provider_batch_usage(jobs)
    if provider_usage["job_count_with_usage"] == len(jobs) and provider_usage["job_count_with_usage"] > 0:
        input_tokens = provider_usage["input_tokens"]
        cached_input_tokens = provider_usage["cached_input_tokens"]
        cache_creation_input_tokens = provider_usage["cache_creation_input_tokens"]
        output_tokens = provider_usage["output_tokens"]
        estimated_cost = provider_usage["estimated_cost"]
    else:
        input_tokens = audit_input_tokens
        cached_input_tokens = audit_cached_input_tokens
        cache_creation_input_tokens = audit_cache_creation_input_tokens
        output_tokens = audit_output_tokens
        estimated_cost = audit_estimated_cost

    request_outcomes = _summarize_batch_request_outcomes(jobs)
    if counts["candidate"] > 0 or counts["error"] > 0:
        if (
            counts["processed"] == 0
            and request_outcomes["canceled"] > 0
            and request_outcomes["errored"] == 0
            and request_outcomes["expired"] == 0
        ):
            final_status = "cancelled"
        elif (
            counts["processed"] == 0
            and request_outcomes["expired"] > 0
            and request_outcomes["errored"] == 0
            and request_outcomes["canceled"] == 0
        ):
            final_status = "expired"
        elif (
            counts["processed"] == 0
            and request_outcomes["errored"] > 0
            and request_outcomes["canceled"] == 0
            and request_outcomes["expired"] == 0
        ):
            final_status = "failed"
        else:
            final_status = "completed_with_errors"
    else:
        final_status = "completed"

    finalize_enrichment_run(
        db_path,
        run_id,
        status=final_status,
        total_rows=sum(counts.values()),
        processed_rows=counts["processed"],
        skipped_rows=counts["skipped"],
        insufficient_rows=counts["insufficient"],
        success_count=counts["processed"],
        error_count=counts["error"] + counts["candidate"],
        input_tokens=input_tokens or 0,
        cached_input_tokens=cached_input_tokens or 0,
        cache_creation_input_tokens=cache_creation_input_tokens or 0,
        output_tokens=output_tokens or 0,
        estimated_cost=estimated_cost or 0.0,
    )
    return {
        "run_id": run_id,
        "status": final_status,
        "processed_rows": counts["processed"],
        "error_count": counts["error"] + counts["candidate"],
    }

async def poll_batch_runs(
    *,
    db_path: str,
    run_id: Optional[str] = None,
    watch: bool = False,
    interval_seconds: float = 5.0,
) -> Dict[str, Any]:
    """Poll provider batch jobs, reconcile completed outputs, and finalize ready runs."""
    touched_runs: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []

    while True:
        jobs = list_enrichment_batch_jobs(db_path, run_id=run_id)
        if not jobs:
            raise EnrichmentError("No batch jobs found.")

        pending_jobs = [
            job for job in jobs
            if not _job_batch_is_terminal(job) or not job.get("reconciled_at")
        ]

        providers: Dict[str, Any] = {}
        for job in pending_jobs:
            run_row = get_enrichment_run(db_path, job["run_id"])
            if not run_row:
                errors.append(f"Missing run for batch job {job['id']}")
                continue

            provider = providers.get(run_row["model"])
            if provider is None:
                provider = _get_batch_provider(run_row["model"])
                providers[run_row["model"]] = provider

            try:
                provider_name = job.get("provider") or _get_batch_provider_name(provider)
                metadata_update = _load_json_object(job.get("metadata"))

                if provider_name == "openai":
                    batch_obj = await provider.client.batches.retrieve(job["provider_batch_id"])
                    request_counts = _extract_batch_attr(batch_obj, "request_counts")
                    request_count_data = _batch_request_counts_to_dict(request_counts)
                    completed_count = _extract_batch_attr(request_counts, "completed", job.get("completed_count", 0))
                    failed_count = _extract_batch_attr(request_counts, "failed", job.get("failed_count", 0))
                    batch_status = _extract_batch_attr(batch_obj, "status", job["status"])
                    completed_at = _extract_batch_attr(batch_obj, "completed_at")
                    usage_payload = _extract_batch_attr(batch_obj, "usage")
                    provider_usage = provider.build_token_usage(usage_payload, batch_pricing=True)
                    if provider_usage is not None:
                        metadata_update["provider_usage"] = {
                            "input_tokens": provider_usage.input_tokens,
                            "cached_input_tokens": provider_usage.cached_input_tokens,
                            "cache_creation_input_tokens": getattr(provider_usage, "cache_creation_input_tokens", 0) or 0,
                            "output_tokens": provider_usage.output_tokens,
                            "estimated_cost": provider_usage.estimate_cost(),
                        }
                        metadata_update["provider_usage_fetched_at"] = datetime.now().isoformat()
                    metadata_update["request_counts"] = request_count_data
                    metadata_update["provider_status_fetched_at"] = datetime.now().isoformat()
                    output_file_id = _extract_batch_attr(batch_obj, "output_file_id")
                    error_file_id = _extract_batch_attr(batch_obj, "error_file_id")
                elif provider_name == "anthropic":
                    batch_obj = await provider.client.messages.batches.retrieve(job["provider_batch_id"])
                    request_counts = _extract_batch_attr(batch_obj, "request_counts")
                    request_count_data = _batch_request_counts_to_dict(request_counts)
                    completed_count = int(request_count_data.get("succeeded", job.get("completed_count", 0)) or 0)
                    failed_count = int(
                        (request_count_data.get("errored", 0) or 0)
                        + (request_count_data.get("canceled", 0) or 0)
                        + (request_count_data.get("expired", 0) or 0)
                    )
                    batch_status = _extract_batch_attr(batch_obj, "processing_status", job["status"])
                    completed_at = _extract_batch_attr(batch_obj, "ended_at")
                    metadata_update["request_counts"] = request_count_data
                    metadata_update["results_url"] = _extract_batch_attr(batch_obj, "results_url")
                    metadata_update["provider_status_fetched_at"] = datetime.now().isoformat()
                    output_file_id = None
                    error_file_id = None
                elif provider_name == "gemini":
                    batch_obj = await provider.get_batch_job(job["provider_batch_id"])
                    batch_stats = _extract_batch_attr(batch_obj, "batch_stats") or {}
                    request_count_data = provider.build_batch_request_counts(batch_stats)
                    completed_count = int(request_count_data.get("succeeded", job.get("completed_count", 0)) or 0)
                    failed_count = int(
                        (request_count_data.get("errored", 0) or 0)
                        + (request_count_data.get("canceled", 0) or 0)
                        + (request_count_data.get("expired", 0) or 0)
                    )
                    batch_status = _extract_batch_attr(batch_obj, "state", job["status"])
                    completed_at = _extract_batch_attr(batch_obj, "completed_at")
                    metadata_update["batch_stats"] = batch_stats
                    metadata_update["request_counts"] = request_count_data
                    metadata_update["provider_status_fetched_at"] = datetime.now().isoformat()
                    dest = _extract_batch_attr(batch_obj, "dest") or {}
                    output_file_id = _extract_batch_attr(dest, "file_name")
                    error_file_id = None
                else:
                    raise EnrichmentError(f"Unsupported batch provider '{provider_name}'")

                update_enrichment_batch_job(
                    db_path,
                    int(job["id"]),
                    provider_batch_id=_extract_batch_attr(batch_obj, "id"),
                    output_file_id=output_file_id,
                    error_file_id=error_file_id,
                    status=batch_status,
                    completed_count=completed_count,
                    failed_count=failed_count,
                    metadata=metadata_update,
                    completed_at=str(completed_at) if completed_at is not None else None,
                )
                update_enrichment_run_status(db_path, run_row["run_id"], batch_status)

                refreshed_job = get_enrichment_batch_job(db_path, int(job["id"]))
                if (
                    refreshed_job
                    and _job_batch_is_terminal(refreshed_job)
                    and not refreshed_job.get("reconciled_at")
                ):
                    update_enrichment_run_status(db_path, run_row["run_id"], "reconciling")
                    await _reconcile_openai_batch_job(db_path, refreshed_job, run_row)
            except Exception as exc:
                logging.error("Batch polling failed for job %s: %s", job.get("provider_batch_id"), exc)
                errors.append(str(exc))

        jobs = list_enrichment_batch_jobs(db_path, run_id=run_id)
        run_ids = sorted({job["run_id"] for job in jobs})
        for touched_run_id in run_ids:
            finalized = _finalize_batch_run_if_ready(db_path, touched_run_id)
            if finalized:
                touched_runs[touched_run_id] = finalized

        still_pending = [
            job for job in jobs
            if not _job_batch_is_terminal(job) or not job.get("reconciled_at")
        ]
        if not watch or not still_pending:
            break
        await asyncio.sleep(interval_seconds)

    run_summaries = list(touched_runs.values())
    success_count = sum(int(summary.get("processed_rows", 0) or 0) for summary in run_summaries)
    error_count = sum(int(summary.get("error_count", 0) or 0) for summary in run_summaries)
    if errors:
        status = "partial"
    elif error_count:
        status = "error"
    else:
        status = "success"

    return {
        "status": status,
        "run_summaries": run_summaries,
        "errors": errors,
        "pending_jobs": len([
            job for job in list_enrichment_batch_jobs(db_path, run_id=run_id)
            if not _job_batch_is_terminal(job) or not job.get("reconciled_at")
        ]),
        "success_count": success_count,
        "error_count": error_count,
    }

async def cancel_batch_runs(
    *,
    db_path: str,
    run_id: str,
) -> Dict[str, Any]:
    """Cancel all active provider batch jobs for a run."""
    jobs = [
        job for job in list_enrichment_batch_jobs(db_path, run_id=run_id)
        if not _job_batch_is_terminal(job)
    ]
    if not jobs:
        raise EnrichmentError(f"No active batch jobs found for run {run_id}")

    cancelled = 0
    providers: Dict[str, Any] = {}
    for job in jobs:
        run_row = get_enrichment_run(db_path, job["run_id"])
        if not run_row:
            continue
        provider = providers.get(run_row["model"])
        if provider is None:
            provider = _get_batch_provider(run_row["model"])
            providers[run_row["model"]] = provider
        provider_name = job.get("provider") or _get_batch_provider_name(provider)
        metadata_update = None
        if provider_name == "openai":
            batch_obj = await provider.client.batches.cancel(job["provider_batch_id"])
            cancel_status = _extract_batch_attr(batch_obj, "status", "cancelling")
        elif provider_name == "anthropic":
            batch_obj = await provider.client.messages.batches.cancel(job["provider_batch_id"])
            cancel_status = _extract_batch_attr(batch_obj, "processing_status", "canceling")
        elif provider_name == "gemini":
            batch_obj = await provider.cancel_batch_job(job["provider_batch_id"])
            cancel_status = _extract_batch_attr(batch_obj, "state", "BATCH_STATE_CANCELLED")
            batch_stats = _extract_batch_attr(batch_obj, "batch_stats") or {}
            metadata_update = _load_json_object(job.get("metadata"))
            metadata_update["batch_stats"] = batch_stats
            metadata_update["request_counts"] = provider.build_batch_request_counts(batch_stats)
            metadata_update["provider_status_fetched_at"] = datetime.now().isoformat()
        else:
            raise EnrichmentError(f"Unsupported batch provider '{provider_name}'")
        update_enrichment_batch_job(
            db_path,
            int(job["id"]),
            status=cancel_status,
            metadata=metadata_update,
        )
        cancelled += 1

    update_enrichment_run_status(db_path, run_id, "cancelling")
    return {"status": "success", "cancelled_jobs": cancelled, "run_id": run_id}

__all__ = [

    '_reconcile_openai_batch_job',

    '_finalize_batch_run_if_ready',

    'poll_batch_runs',

    'cancel_batch_runs',

]
