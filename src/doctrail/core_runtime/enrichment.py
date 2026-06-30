import os
import logging
import asyncio
import re
import json
import tempfile
import sqlite3
import random
import uuid
import sys
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime
import difflib

import click
import yaml

from ..constants import (
    DEFAULT_TABLE_NAME, DEFAULT_MODEL, ERROR_NO_ENRICHMENTS,
    ERROR_NO_DATABASE, ERROR_ENRICHMENT_NOT_FOUND
)
from ..db_operations import (
    get_db_connection, execute_query, execute_query_optimized,
    _quote_identifier,
    create_query_hash, create_run_id, start_enrichment_run, finalize_enrichment_run,
    materialize_run_inputs, update_run_item_statuses, create_run_summary_view,
    create_final_run_view, create_run_view, get_or_create_prompt_id, compute_prompt_id,
    update_enrichment_run_status, ensure_enrichment_batch_jobs_table,
    create_enrichment_batch_job, update_enrichment_batch_job,
    list_enrichment_batch_jobs, get_enrichment_run, get_enrichment_batch_job,
    update_run_item_statuses_by_row_order, persist_enrichment_result,
    plan_existing_enrichment_skips,
    filter_unskipped_input_rows,
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

VALID_DEDUPE_SCOPES = {"prompt", "query", "enrichment"}
DEDUPE_SCOPE_ALIASES = {"name": "enrichment"}


def _normalize_dedupe_scope(scope: Optional[str]) -> str:
    normalized = (scope or "query").strip().lower()
    normalized = DEDUPE_SCOPE_ALIASES.get(normalized, normalized)
    if normalized not in VALID_DEDUPE_SCOPES:
        valid = "', '".join(sorted(VALID_DEDUPE_SCOPES | set(DEDUPE_SCOPE_ALIASES)))
        raise EnrichmentError(f"dedupe_scope must be one of '{valid}'")
    return normalized


def _confirm_cost_or_raise(cost_info: Dict[str, Any], cost_threshold: float) -> None:
    """Require explicit confirmation when estimated cost exceeds the configured threshold."""
    total_cost = float(cost_info.get('total_cost', 0.0) or 0.0)
    if not should_confirm_cost(total_cost, cost_threshold):
        return

    breakdown = cost_info.get('breakdown')
    if breakdown:
        print(format_cost_estimate(breakdown))

    message = (
        f"Estimated cost ${total_cost:.2f} exceeds threshold ${cost_threshold:.2f}."
    )
    if not sys.stdin.isatty():
        raise EnrichmentError(
            f"{message} Re-run with --skip-cost-check or raise --cost-threshold to continue."
        )

    if not click.confirm(f"\n{message} Continue?", default=False):
        raise EnrichmentError("Cancelled due to estimated cost.")


def _aggregate_run_counts(run_records: List[Dict[str, Any]]) -> Tuple[int, int]:
    """Return summed row-level success and error counts from completed runs."""
    success_count = sum(int(record.get("success_count", 0) or 0) for record in run_records)
    error_count = sum(int(record.get("error_count", 0) or 0) for record in run_records)
    return success_count, error_count


def _preflight_model_for_enrichment(model_name: str, enrichment_name: str) -> None:
    """Run provider-specific checks that can fail before row processing starts."""
    if model_name != "replay" and not model_name.startswith("replay/"):
        return

    from ..llm_providers.factory import get_llm_provider

    provider = get_llm_provider(model_name, enrichment_name=enrichment_name)
    preflight = getattr(provider, "preflight_enrichment", None)
    if callable(preflight):
        preflight(enrichment_name)


async def run_enrichment(
    config_path: str,
    enrichments: List[str],
    db_path: Optional[str] = None,
    output_db_path: Optional[str] = None,
    model: Optional[str] = None,
    limit: Optional[int] = None,
    rowid: Optional[int] = None,
    sha1: Optional[str] = None,
    overwrite: bool = False,
    batch_size: Optional[int] = None,
    truncate: bool = False,
    skip_cost_check: bool = False,
    cost_threshold: float = 5.0,
    dry_run: bool = False,
    verbose: bool = False,
    progress_callback: Optional[callable] = None,
    where_clause: Optional[str] = None,
    override_query: Optional[str] = None,
    project: Optional[str] = None,
    dedupe_scope: Optional[str] = None,
    materialize_inputs: bool = True,
    execution_mode: str = "sync",
    allow_column_collision: bool = False,
) -> Dict[str, Any]:
    """
    Run enrichment tasks on database content using LLM processing.

    Args:
        config_path: Path to YAML configuration file
        enrichments: List of enrichment task names to run
        db_path: Optional override for database path from config
        model: Optional model override for all enrichments
        limit: Limit number of rows to process
        rowid: Process only specific row by rowid
        sha1: Process only specific row by sha1 hash
        overwrite: Overwrite existing data in output columns
        batch_size: Override batch size for processing
        truncate: Truncate long inputs to fit model context window
        skip_cost_check: Skip cost estimation and confirmation
        cost_threshold: Cost threshold for confirmation prompt
        dry_run: Preview without calling model or updating database
        verbose: Enable detailed logging
        progress_callback: Optional callback function for progress updates
        where_clause: Optional SQL WHERE predicate applied to the enrichment query as an outer filter
        override_query: Optional SQL query to override the config query (dynamic row selection)
        project: Optional project name to tag enrichments for filtering
        dedupe_scope: How append-mode dedupe works. CLI/API value overrides per-enrichment YAML.
        materialize_inputs: Persist exact input row snapshots for each run
        execution_mode: sync or batch. The legacy name openai-batch is still accepted.
        allow_column_collision: Allow enrichment fields that match source column names

    Returns:
        Dict containing:
            - status: "success" or "error"
            - enrichments_run: List of enrichment names that were executed
            - results: List of enrichment results
            - errors: List of any errors that occurred

    Raises:
        ConfigurationError: If config file is missing or invalid
        DatabaseError: If database file doesn't exist
        EnrichmentError: If enrichment task fails
    """
    setup_logging(verbose)

    if dedupe_scope is not None:
        dedupe_scope = _normalize_dedupe_scope(dedupe_scope)
    execution_mode = _normalize_execution_mode(execution_mode)
    if execution_mode not in {"sync", "batch"}:
        raise EnrichmentError("execution_mode must be 'sync' or 'batch'")

    # Load configuration
    try:
        config_data = load_config(config_path)
    except FileNotFoundError:
        raise ConfigurationError(f"Configuration file not found: {config_path}")
    except yaml.YAMLError as e:
        raise ConfigurationError(f"Invalid YAML in config file: {e}")

    # Resolve database path
    if db_path:
        actual_db_path = os.path.expanduser(db_path)
        logging.info(f"Database path overridden: {actual_db_path}")
    else:
        if 'database' not in config_data:
            raise ConfigurationError(ERROR_NO_DATABASE)
        actual_db_path = os.path.expanduser(config_data['database'])
        logging.info(f"Using database from config: {actual_db_path}")

    # Resolve output database path (defaults to source database)
    if output_db_path:
        actual_output_db_path = os.path.expanduser(output_db_path)
        os.makedirs(os.path.dirname(os.path.abspath(actual_output_db_path)), exist_ok=True)
        logging.info(f"Enrichments will be written to: {actual_output_db_path}")
    else:
        actual_output_db_path = actual_db_path

    # Check source database exists
    if not os.path.exists(actual_db_path):
        raise DatabaseError(
            f"Database file not found: {actual_db_path}\n"
            f"Run 'ingest' command first to create the database."
        )

    # Override model if specified
    if model:
        config_data['default_model'] = model
        logging.info(f"Model overridden: {model}")

    # Override batch size if specified
    if batch_size:
        config_data['batch_size'] = batch_size
        logging.info(f"Batch size overridden: {batch_size}")

    # Validate point selectors
    if rowid is not None and sha1 is not None:
        raise EnrichmentError("Cannot specify both rowid and sha1. Use only one point selector.")

    # Validate enrichments exist
    if 'enrichments' not in config_data or not config_data['enrichments']:
        raise ConfigurationError(ERROR_NO_ENRICHMENTS)

    # Find matching enrichment configs
    enrichment_configs = [e for e in config_data['enrichments'] if e['name'] in enrichments]

    if not enrichment_configs:
        available = [e['name'] for e in config_data['enrichments']]
        # Find closest matches
        suggestions = []
        for requested in enrichments:
            closest = difflib.get_close_matches(requested, available, n=1, cutoff=0.4)
            if closest:
                suggestions.append(f"{requested} → {closest[0]}")

        error_msg = f"Enrichment(s) not found: {', '.join(enrichments)}\n"
        if suggestions:
            error_msg += f"Did you mean? {suggestions[0]}\n"
        error_msg += f"Available enrichments: {', '.join(available)}"
        raise EnrichmentError(error_msg)

    # Process enrichments
    all_results = []
    enrichments_run = []
    errors = []
    last_key_column = None  # Track key_column for project view creation
    last_input_table = 'documents'  # Track input table for view creation
    command_started_at = datetime.now().isoformat()
    run_records: List[Dict[str, Any]] = []

    # Resolve config-level key_column
    from ..constants import DEFAULT_KEY_COLUMN as _DEFAULT_KEY_COLUMN
    config_key_column = config_data.get('key_column', _DEFAULT_KEY_COLUMN)

    try:
        from ..enrichment_config import prepare_enrichment_for_processing

        for enrichment_config in enrichment_configs:
            enrichment_name = enrichment_config['name']

            # Prepare enrichment configuration
            default_table = config_data.get('default_table', DEFAULT_TABLE_NAME)
            sql_queries = config_data.get('sql_queries', {})
            strategy, config_errors = prepare_enrichment_for_processing(
                enrichment_config, default_table, sql_queries,
                config_key_column=config_key_column
            )

            if config_errors:
                error_msg = f"Configuration errors in enrichment '{enrichment_name}':\n"
                error_msg += "\n".join(f"  - {error}" for error in config_errors)
                raise ConfigurationError(error_msg)

            config_dedupe_scope = (
                enrichment_config.get("dedupe_scope")
                or enrichment_config.get("dedupe-scope")
            )
            effective_dedupe_scope = _normalize_dedupe_scope(dedupe_scope or config_dedupe_scope)

            # Track key_column and input_table for view creation
            last_key_column = strategy.key_column
            last_input_table = strategy.input_table

            pack_size = enrichment_config.get("pack_size")
            if _is_batch_execution_mode(execution_mode) and pack_size not in (None, 0, 1):
                raise EnrichmentError(
                    "pack_size is currently supported only with execution_mode=sync"
                )

            # Build query
            query = _build_enrichment_query(
                enrichment_config, config_data, strategy,
                rowid=rowid, sha1=sha1, limit=limit, overwrite=overwrite,
                where_clause=where_clause,
                override_query=override_query
            )

            input_columns = enrichment_config.get('input', {}).get('input_columns', [])

            # The SQL query selects candidate row keys. The prompt payload is
            # fetched separately from input_columns so scope queries can stay
            # cheap even on databases with large full-text columns.
            results = execute_query_optimized(
                actual_db_path,
                query,
                input_columns,
                key_column=strategy.key_column,
                default_table=strategy.input_table,
            )
            total_rows = len(results)

            # Check for column name collisions between enrichment fields and source columns
            if results and strategy.output_columns:
                source_columns = set(results[0].keys())
                try:
                    table_ref = _quote_identifier(strategy.input_table, "table name")
                    with get_db_connection(actual_db_path) as conn:
                        source_columns.update(
                            row[1] for row in conn.execute(f"PRAGMA table_info({table_ref})").fetchall()
                        )
                except Exception:
                    logging.debug(
                        "Could not inspect source columns for collision detection",
                        exc_info=True,
                    )
                enrichment_fields = set(strategy.output_columns)
                collisions = source_columns & enrichment_fields
                if collisions and not allow_column_collision:
                    collision_list = ', '.join(sorted(collisions))
                    raise EnrichmentError(
                        f"Enrichment field name(s) collide with source table columns: {collision_list}\n"
                        f"This causes views to shadow enrichment results with source values.\n"
                        f"Fix: rename the field(s) in your schema, or use --allow-column-collision to proceed.\n"
                        f"If you proceed, source columns will appear as '<name>_input' in views."
                    )
                if collisions and allow_column_collision:
                    collision_list = ', '.join(sorted(collisions))
                    logging.warning(
                        f"Column collision allowed: {collision_list} — source columns will appear as '<name>_input' in views"
                    )

            if progress_callback:
                progress_callback({
                    'enrichment': enrichment_name,
                    'total_rows': total_rows,
                    'status': 'querying'
                })

            prompt_text = _resolve_enrichment_prompt(enrichment_config, config_data)
            system_prompt_text = enrichment_config.get('system_prompt', '')
            planning_models = _resolve_models(enrichment_config, config_data, model)
            planning_model = planning_models[0]
            output_columns = enrichment_config.get('output_columns')
            if not output_columns:
                output_column = enrichment_config.get('output_column')
                output_columns = [output_column] if output_column else strategy.output_columns
            if isinstance(output_columns, str):
                output_columns = [output_columns]
            output_columns = [column for column in (output_columns or []) if column]
            prompt_id = compute_prompt_id(enrichment_name, prompt_text, system_prompt_text)
            query_hash = create_query_hash(query)
            separate_output_db = actual_output_db_path != actual_db_path

            planning_skipped_rows: List[Dict[str, Any]] = []
            # Enrichment scope is model-agnostic so it can always be planned
            # once up front. Prompt scope is per-model: planning it here with
            # models[0] would starve every later model in a multi-model run,
            # so it is only pre-planned for single-model runs and otherwise
            # left to process_enrichment's per-model planner.
            outer_plannable = (
                effective_dedupe_scope == "enrichment"
                or (effective_dedupe_scope == "prompt" and len(planning_models) == 1)
            )
            if output_columns and not overwrite and outer_plannable:
                planning_skipped_rows = plan_existing_enrichment_skips(
                    actual_output_db_path,
                    rows=results,
                    enrichment_name=enrichment_name,
                    model=planning_model,
                    prompt_id=prompt_id,
                    key_column=strategy.key_column,
                    dedupe_scope=effective_dedupe_scope,
                    query_hash=query_hash,
                    output_table=strategy.output_table,
                    output_cols=output_columns,
                    separate_output_db=separate_output_db,
                    source_table=strategy.input_table,
                )
                rows_to_process = filter_unskipped_input_rows(
                    results,
                    planning_skipped_rows,
                    key_column=strategy.key_column,
                )
                actual_total = len(rows_to_process)
            else:
                rows_to_process = results
                actual_total = len(results)

            if dry_run:
                dry_models = planning_models

                already_processed = 0
                dry_rows_to_process = rows_to_process
                if results and not overwrite:
                    try:
                        if effective_dedupe_scope in {"prompt", "enrichment"} and planning_skipped_rows:
                            already_processed = len(planning_skipped_rows)
                        else:
                            dry_skipped_rows = plan_existing_enrichment_skips(
                                actual_output_db_path,
                                rows=results,
                                enrichment_name=enrichment_name,
                                model=planning_model,
                                prompt_id=prompt_id,
                                key_column=strategy.key_column,
                                dedupe_scope=effective_dedupe_scope,
                                query_hash=query_hash,
                                output_table=strategy.output_table,
                                output_cols=output_columns,
                                separate_output_db=separate_output_db,
                                source_table=strategy.input_table,
                            )
                            already_processed = len(dry_skipped_rows)
                            dry_rows_to_process = filter_unskipped_input_rows(
                                results,
                                dry_skipped_rows,
                                key_column=strategy.key_column,
                            )
                    except Exception:
                        already_processed = 0  # DB might not exist yet
                        dry_rows_to_process = results

                would_process = len(dry_rows_to_process)

                # Build sample input preview (first unprocessed row)
                sample_input = {}
                input_cols = enrichment_config.get('input', {}).get('input_columns', [])
                sample_row = dry_rows_to_process[0] if dry_rows_to_process else (results[0] if results else None)
                if sample_row:
                    for col_spec in input_cols:
                        col_name = col_spec.split(':')[0] if isinstance(col_spec, str) else col_spec
                        val = sample_row.get(col_name, '')
                        if val:
                            val_str = str(val)
                            # Apply truncation spec if present (e.g., "content:4000")
                            if isinstance(col_spec, str) and ':' in col_spec:
                                limit_chars = int(col_spec.split(':')[1])
                                val_str = val_str[:limit_chars]
                            sample_input[col_name] = val_str[:300] + ('...' if len(val_str) > 300 else '')

                # Schema field info
                schema_fields = []
                if strategy.schema_dict and isinstance(strategy.schema_dict, dict):
                    for fname, fdef in strategy.schema_dict.items():
                        if isinstance(fdef, dict):
                            if 'enum' in fdef:
                                schema_fields.append(f"{fname} (enum: {fdef['enum']})")
                            elif 'enum_list' in fdef:
                                schema_fields.append(f"{fname} (enum_list: {fdef['enum_list']})")
                            else:
                                schema_fields.append(f"{fname} ({fdef.get('type', 'string')})")
                        else:
                            schema_fields.append(str(fname))

                all_results.append({
                    'enrichment': enrichment_name,
                    'db_path': actual_db_path,
                    'key_column': strategy.key_column,
                    'models': dry_models,
                    'total_rows': total_rows,
                    'already_processed': already_processed,
                    'would_process': would_process,
                    'min_input_chars': enrichment_config.get('min_input_chars', config_data.get('min_input_chars', 1)),
                    'query_hash': query_hash[:8],
                    'dedupe_scope': effective_dedupe_scope,
                    'schema_fields': schema_fields,
                    'sample_input': sample_input,
                    'prompt_preview': (prompt_text[:200] + '...' if len(prompt_text) > 200 else prompt_text),
                    'dry_run': True
                })
                enrichments_run.append(enrichment_name)
                continue

            # Resolve models
            models = _resolve_models(enrichment_config, config_data, model)

            if not overwrite and total_rows > 0 and actual_total == 0:
                print("All rows already processed!")
                enrichments_run.append(enrichment_name)
                if progress_callback:
                    progress_callback({
                        'enrichment': enrichment_name,
                        'status': 'completed',
                        'results': 0
                    })
                continue

            if overwrite and actual_total > 0:
                print(f"Overwrite mode: reprocessing {actual_total} rows.")

            # Validate models
            for model_name in models:
                if not validate_model(model_name, execution_mode=execution_mode):
                    error = (
                        get_model_validation_error(model_name, execution_mode=execution_mode)
                        or f"Model '{model_name}' is not supported."
                    )
                    raise EnrichmentError(error)

            # Process with each model
            for model_idx, current_model in enumerate(models):
                batch_provider = None
                if _is_batch_execution_mode(execution_mode):
                    batch_provider = _get_batch_provider(current_model)
                    _validate_batch_schema_compatibility(
                        provider=batch_provider,
                        enrichment_name=enrichment_name,
                        model=current_model,
                        pydantic_model=getattr(strategy, "pydantic_model", None),
                    )
                else:
                    _preflight_model_for_enrichment(current_model, enrichment_name)

                current_prompt_id = get_or_create_prompt_id(
                    actual_output_db_path,
                    enrichment_name,
                    prompt_text,
                    system_prompt_text,
                    current_model,
                )
                current_run_id = create_run_id(
                    enrichment_name=enrichment_name,
                    model=current_model,
                    prompt_id=current_prompt_id,
                    query_hash=query_hash,
                    command_started_at=command_started_at,
                )

                # Cost estimation and confirmation happen before a run record is
                # created so rejected work does not leave a dangling running run.
                if not skip_cost_check:
                    cost_info = _estimate_enrichment_cost(
                        enrichment_config,
                        rows_to_process or results,
                        current_model,
                        actual_total,
                        execution_mode=execution_mode,
                    )

                    if progress_callback:
                        progress_callback({
                            'enrichment': enrichment_name,
                            'model': current_model,
                            'cost_estimate': cost_info,
                            'status': 'cost_check'
                        })

                    _confirm_cost_or_raise(cost_info, cost_threshold)

                start_enrichment_run(
                    actual_output_db_path,
                    run_id=current_run_id,
                    command_started_at=command_started_at,
                    enrichment_name=enrichment_name,
                    model=current_model,
                    prompt_id=current_prompt_id,
                    query_sql=query,
                    query_hash=query_hash,
                    key_column=strategy.key_column,
                    source_name=strategy.input_table,
                    dedupe_scope=effective_dedupe_scope,
                    project=project,
                    materialized_inputs=materialize_inputs,
                    metadata={
                        "output_table": strategy.output_table,
                        "input_table": strategy.input_table,
                        "execution_mode": execution_mode,
                        "effective_prompt": prompt_text,
                        "effective_system_prompt": system_prompt_text,
                        "reasoning_effort": enrichment_config.get("reasoning_effort"),
                        "enrichment_config": enrichment_config,
                        "truncate": truncate or enrichment_config.get('truncate', False),
                        "overwrite": overwrite,
                    },
                    status="submitted" if _is_batch_execution_mode(execution_mode) else "running",
                )
                materialize_run_inputs(
                    actual_output_db_path,
                    current_run_id,
                    rows_to_process,
                    strategy.key_column,
                    enabled=materialize_inputs,
                )

                # Create progress bar or use callback
                pbar_desc = (
                    f"{enrichment_name} [{current_model}]" if len(models) > 1
                    else enrichment_name
                )

                if _is_batch_execution_mode(execution_mode):
                    try:
                        batch_jobs = await _submit_batch_jobs(
                            db_path=actual_output_db_path,
                            run_id=current_run_id,
                            rows_to_process=rows_to_process,
                            enrichment_config=enrichment_config,
                            config_data=config_data,
                            model=current_model,
                            key_column=strategy.key_column,
                            prompt_text=prompt_text,
                            system_prompt_text=system_prompt_text,
                            truncate=truncate or enrichment_config.get('truncate', False),
                            pydantic_model=getattr(strategy, "pydantic_model", None),
                            provider=batch_provider,
                        )
                    except Exception:
                        if rows_to_process:
                            update_run_item_statuses_by_row_order(
                                actual_output_db_path,
                                current_run_id,
                                {row_order: "error" for row_order in range(len(rows_to_process))},
                            )
                        finalize_enrichment_run(
                            actual_output_db_path,
                            current_run_id,
                            status='failed',
                            total_rows=len(rows_to_process),
                            processed_rows=0,
                            skipped_rows=0,
                            insufficient_rows=0,
                            success_count=0,
                            error_count=len(rows_to_process),
                            input_tokens=0,
                            output_tokens=0,
                            estimated_cost=0.0,
                        )
                        raise

                    run_records.append({
                        'run_id': current_run_id,
                        'enrichment_name': enrichment_name,
                        'model': current_model,
                        'prompt_id': current_prompt_id,
                        'query_hash': query_hash,
                        'batch_jobs': batch_jobs,
                    })
                    enrichments_run.append(enrichment_name)
                    if progress_callback:
                        progress_callback({
                            'enrichment': enrichment_name,
                            'model': current_model,
                            'status': 'submitted',
                            'batch_jobs': len(batch_jobs),
                        })
                    continue

                progress_bar = create_progress_bar(
                    total=actual_total,
                    desc=pbar_desc,
                    verbose=verbose
                )

                # Process enrichment
                try:
                    with progress_bar as pbar:
                        model_results = await process_enrichment(
                            results=rows_to_process,
                            enrichment_config=enrichment_config,
                            model=current_model,
                            pbar=pbar,
                            db_path=actual_output_db_path,
                            source_db_path=actual_db_path,
                            table=strategy.input_table,
                            overwrite=overwrite,
                            config=config_data,
                            truncate=truncate or enrichment_config.get('truncate', False),
                            verbose=verbose,
                            output_table=strategy.output_table,
                            key_column=strategy.key_column,
                            enrichment_strategy=strategy,
                            is_multi_model=len(models) > 1,
                            project=project,
                            run_id=current_run_id,
                            query_hash=query_hash,
                            dedupe_scope=effective_dedupe_scope,
                        )

                        all_results.extend(model_results)
                        row_statuses: Dict[str, str] = {}
                        processed_count = 0
                        skipped_count = 0
                        insufficient_count = 0
                        error_count = 0
                        input_tokens = 0
                        cached_input_tokens = 0
                        cache_creation_input_tokens = 0
                        output_tokens = 0
                        estimated_cost = 0.0

                        for r in model_results:
                            if not r:
                                continue
                            key_value = r.get('key_value')
                            if key_value is None:
                                key_value = r.get(strategy.key_column)

                            if r.get('updated') is not None:
                                row_status = 'processed'
                                processed_count += 1
                            elif r.get('error') and str(r.get('error', '')).startswith('Skipped:'):
                                row_status = 'insufficient'
                                insufficient_count += 1
                            elif r.get('original') and 'insufficient input' in str(r.get('original')):
                                row_status = 'insufficient'
                                insufficient_count += 1
                            elif r.get('error'):
                                row_status = 'error'
                                error_count += 1
                            else:
                                row_status = 'skipped'
                                skipped_count += 1

                            if key_value is not None:
                                row_statuses[str(key_value)] = row_status

                            usage = r.get('usage') or {}
                            input_tokens += usage.get('input_tokens', 0) or 0
                            cached_input_tokens += usage.get('cached_input_tokens', 0) or 0
                            cache_creation_input_tokens += usage.get('cache_creation_input_tokens', 0) or 0
                            output_tokens += usage.get('output_tokens', 0) or 0
                            estimated_cost += usage.get('estimated_cost', 0) or 0.0

                        if row_statuses:
                            update_run_item_statuses(actual_output_db_path, current_run_id, row_statuses)

                        finalize_enrichment_run(
                            actual_output_db_path,
                            current_run_id,
                            status='completed_with_errors' if error_count else 'completed',
                            total_rows=len(rows_to_process),
                            processed_rows=processed_count,
                            skipped_rows=skipped_count,
                            insufficient_rows=insufficient_count,
                            success_count=processed_count,
                            error_count=error_count,
                            input_tokens=input_tokens,
                            cached_input_tokens=cached_input_tokens,
                            cache_creation_input_tokens=cache_creation_input_tokens,
                            output_tokens=output_tokens,
                            estimated_cost=estimated_cost,
                        )
                        run_records.append({
                            'run_id': current_run_id,
                            'enrichment_name': enrichment_name,
                            'model': current_model,
                            'prompt_id': current_prompt_id,
                            'query_hash': query_hash,
                            'success_count': processed_count,
                            'error_count': error_count,
                        })
                except Exception:
                    finalize_enrichment_run(
                        actual_output_db_path,
                        current_run_id,
                        status='failed',
                        total_rows=len(rows_to_process),
                        processed_rows=0,
                        skipped_rows=0,
                        insufficient_rows=0,
                        success_count=0,
                        error_count=len(rows_to_process),
                        input_tokens=0,
                        output_tokens=0,
                        estimated_cost=0.0,
                    )
                    raise

                enrichments_run.append(enrichment_name)

                if progress_callback:
                    progress_callback({
                        'enrichment': enrichment_name,
                        'model': current_model,
                        'status': 'completed',
                        'results': len(model_results)
                    })

    except asyncio.CancelledError:
        logging.info("Processing cancelled.")
        raise
    except Exception as e:
        errors.append(str(e))
        logging.error(f"Error processing enrichments: {e}")
        raise EnrichmentError(f"Enrichment failed: {e}") from e

    # Skip view creation in dry-run mode
    if dry_run:
        return {
            'status': 'success',
            'enrichments_run': enrichments_run,
            'results': all_results,
            'errors': errors,
            'total_processed': 0,
            'success_count': 0,
            'error_count': 0,
        }

    if _is_batch_execution_mode(execution_mode):
        success_count, error_count = _aggregate_run_counts(run_records)
        return {
            'status': 'submitted',
            'enrichments_run': enrichments_run,
            'results': all_results,
            'errors': errors,
            'total_processed': 0,
            'success_count': success_count,
            'error_count': error_count,
            'run_artifacts': run_records,
        }

    # Get views config from YAML if present
    views_config = config_data.get('views', {})
    priority_columns = views_config.get('priority_columns', None)
    successful_results = [r for r in all_results if r and r.get('updated') is not None]

    # When using a separate output db, store the source path for auto-ATTACH
    separate_dbs = actual_output_db_path != actual_db_path
    source_for_views = actual_db_path if separate_dbs else None
    if separate_dbs:
        from ..db_operations import store_source_db_path
        store_source_db_path(actual_output_db_path, actual_db_path)

    # Create enrichments view for easier querying (in output db)
    if not errors and successful_results:
        try:
            from ..db_operations import create_enrichments_views
            create_enrichments_views(
                actual_output_db_path,
                source_table=last_input_table,
                priority_columns=priority_columns,
                source_db_path=source_for_views,
                key_column=last_key_column or 'sha1',
            )
            logging.debug(f"Created {last_input_table}_enriched view")
        except Exception as e:
            logging.debug(f"Could not create enrichments view: {e}")

    # Create project-specific view if project was specified
    project_view = None
    if project and not errors and successful_results:
        try:
            from ..db_operations import create_project_view
            project_view = create_project_view(
                actual_output_db_path,
                project,
                key_column=last_key_column or 'sha1',
                priority_columns=priority_columns,
                source_db_path=source_for_views
            )
            logging.info(f"Created project view: {project_view}")
        except Exception as e:
            logging.warning(f"Could not create project view: {e}")

    run_artifacts: List[Dict[str, Any]] = []
    run_views: Dict[str, Dict[str, Any]] = {}
    if not errors and run_records:
        for record in run_records:
            artifact = dict(record)
            view_label = record['enrichment_name']
            if sum(1 for r in run_records if r['enrichment_name'] == record['enrichment_name']) > 1:
                view_label = f"{record['enrichment_name']} [{record['model']}]"

            try:
                summary_view = create_run_summary_view(
                    actual_output_db_path,
                    record['run_id'],
                )
                artifact['summary_view'] = summary_view
            except Exception as e:
                logging.warning(f"Could not create summary view for run {record['run_id'][:8]}: {e}")

            try:
                view_name = create_run_view(
                    actual_output_db_path,
                    run_id=record['run_id'],
                    documents_table=last_input_table,
                    key_column=last_key_column or 'sha1',
                    priority_columns=priority_columns,
                    source_db_path=source_for_views,
                )
                artifact['view_name'] = view_name
            except Exception as e:
                logging.warning(f"Could not create run view for run {record['run_id'][:8]}: {e}")

            try:
                final_view = create_final_run_view(
                    actual_output_db_path,
                    record['run_id'],
                    priority_columns=priority_columns,
                )
                artifact['final_view'] = final_view
            except Exception as e:
                logging.warning(f"Could not create final view for run {record['run_id'][:8]}: {e}")

            run_artifacts.append(artifact)
            run_views[view_label] = {
                key: artifact[key]
                for key in ('run_id', 'view_name', 'summary_view', 'final_view', 'model')
                if key in artifact
            }

    success_count, error_count = _aggregate_run_counts(run_records)
    result = {
        'status': 'success' if not errors and error_count == 0 else 'error',
        'enrichments_run': enrichments_run,
        'results': all_results,
        'errors': errors,
        'total_processed': len(all_results),
        'success_count': success_count,
        'error_count': error_count,
    }

    if project_view:
        result['project_view'] = project_view
    if run_views:
        result['run_views'] = run_views
    if run_artifacts:
        result['run_artifacts'] = run_artifacts
    completed_models = sorted({record.get('model') for record in run_records if record.get('model')})
    if len(completed_models) > 1:
        result['multi_model_view_notice'] = (
            "Note: the default enriched view collapses model outputs with latest-write-wins semantics. "
            "Use `doctrail view pivot <name> -e <enrichment> --by-model` for per-model columns."
        )

    return result

__all__ = [

    'run_enrichment',

]
