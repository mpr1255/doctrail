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
    get_db_connection, execute_query, _sql_quote, _quote_identifier,
    create_query_hash, create_run_id, start_enrichment_run, finalize_enrichment_run,
    materialize_run_inputs, update_run_item_statuses, create_run_summary_view,
    create_final_run_view, create_run_view, get_or_create_prompt_id,
    update_enrichment_run_status, ensure_enrichment_batch_jobs_table,
    create_enrichment_batch_job, update_enrichment_batch_job,
    list_enrichment_batch_jobs, get_enrichment_run, get_enrichment_batch_job,
    update_run_item_statuses_by_row_order, persist_enrichment_result,
    plan_existing_enrichment_skips,
    filter_unskipped_input_rows,
    ICR_SAMPLES_TABLE,
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


def _build_icr_override_query(original_query: str, key_col: str, key_values: List[Any]) -> str:
    quoted_values = ", ".join(f"'{_sql_quote(str(value))}'" for value in key_values)
    key_col_ref = _quote_identifier(key_col, "key column")
    return (
        f"WITH _icr_base AS ({original_query}) "
        f"SELECT * FROM _icr_base WHERE {key_col_ref} IN ({quoted_values})"
    )


async def run_ingest(
    db_path: str,
    input_dirs: Optional[List[str]] = None,
    table: str = "documents",
    force: bool = False,
    overwrite: bool = False,
    skip_existing: bool = False,
    limit: Optional[int] = None,
    include_pattern: Optional[str] = None,
    exclude_pattern: Optional[str] = None,
    readability: bool = False,
    html_extractor: str = 'default',
    skip_garbage_check: bool = False,
    fulltext: bool = False,
    manifest_path: Optional[str] = None,
    labels: Optional[List[str]] = None,
    pdf_engine: str = 'auto',
    ocr_engine: str = 'auto',
    workers: Optional[int] = None,
    extractor: str = 'auto',
    verbose: bool = False,
    yes: bool = False,  # Skip confirmation prompts
    # Plugin-specific options
    plugin_name: Optional[str] = None,
    plugin_args: Optional[Dict[str, Any]] = None,
    # Zotero-specific options
    zotero_api_key: Optional[str] = None,
    zotero_library_id: Optional[str] = None,
    zotero_library_type: str = 'user',
    zotero_collection: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Ingest documents into database from various sources.

    Args:
        db_path: Path to SQLite database
        input_dirs: List of directories containing documents to ingest
        table: Target table name
        force: Force operation even if schema mismatch detected
        overwrite: Overwrite existing documents
        skip_existing: Skip exact database filepaths before hashing for a fast resume
        limit: Limit number of files to process
        include_pattern: Only process files matching this glob pattern
        exclude_pattern: Skip files matching this glob pattern
        readability: Use readability library for HTML extraction
        html_extractor: HTML extraction method ('default' or 'smart')
        skip_garbage_check: Skip garbage content detection
        fulltext: Create full-text search index after ingestion
        manifest_path: Path to manifest.json for metadata
        labels: Labels to apply to ingested documents
        pdf_engine: PDF extraction engine
        ocr_engine: OCR engine to use when needed
        workers: Number of extraction worker processes
        verbose: Enable detailed logging
        plugin_name: Name of ingestion plugin to use
        plugin_args: Arguments for the plugin
        zotero_api_key: Zotero API key
        zotero_library_id: Zotero library ID
        zotero_library_type: Zotero library type
        zotero_collection: Zotero collection to ingest

    Returns:
        Dict containing:
            - status: "success" or "error"
            - files_processed: Number of files processed
            - files_skipped: Number of files skipped
            - errors: List of errors that occurred
    """
    setup_logging(verbose)

    db_path = os.path.expanduser(db_path)

    # Determine ingestion mode
    if plugin_name:
        # Plugin mode
        from ..plugins import discover_plugins

        available_plugins = discover_plugins()
        plugin_instance = available_plugins.get(plugin_name)

        if not plugin_instance:
            raise ConfigurationError(
                f"Plugin '{plugin_name}' not found. "
                f"Available: {', '.join(available_plugins.keys())}"
            )

        result = await plugin_instance.ingest(
            db_path=db_path,
            config={},
            verbose=verbose,
            overwrite=overwrite,
            limit=limit,
            fulltext=fulltext,
            **(plugin_args or {})
        )

        return {
            'status': 'success',
            'plugin': plugin_name,
            'result': result
        }

    elif zotero_api_key and zotero_library_id:
        # Zotero mode
        from ..plugins.zotero_ingester import process_zotero_ingest

        result = await process_zotero_ingest(
            db_path=db_path,
            api_key=zotero_api_key,
            library_id=zotero_library_id,
            library_type=zotero_library_type,
            collection_name=zotero_collection,
            verbose=verbose,
            fulltext=fulltext
        )

        return {
            'status': 'success',
            'mode': 'zotero',
            'result': result
        }

    elif input_dirs:
        # Local file ingestion mode
        from ..ingest import process_ingest

        total_processed = 0
        all_results = []

        for dir_path in input_dirs:
            if not os.path.exists(dir_path):
                raise ConfigurationError(f"Input directory does not exist: {dir_path}")

            result = await process_ingest(
                db_path=db_path,
                input_dir=dir_path,
                table=table,
                verbose=verbose,
                force=force,
                overwrite=overwrite,
                skip_existing=skip_existing,
                limit=limit,
                include_pattern=include_pattern,
                exclude_pattern=exclude_pattern,
                readability=readability,
                html_extractor=html_extractor,
                skip_garbage_check=skip_garbage_check,
                yes=yes,
                fulltext=fulltext,
                manifest_path=manifest_path,
                labels=labels,
                pdf_engine=pdf_engine,
                ocr_engine=ocr_engine,
                workers=workers,
                extractor=extractor,
            )

            total_processed += 1
            all_results.append(result)

        return {
            'status': 'success',
            'mode': 'local',
            'directories_processed': total_processed,
            'results': all_results
        }

    else:
        raise ConfigurationError(
            "Must specify one of: input_dirs, plugin_name, or zotero credentials"
        )

async def list_enrichments(
    config_path: str,
) -> Dict[str, Any]:
    """
    List all available enrichments from a configuration file.

    Args:
        config_path: Path to YAML configuration file

    Returns:
        Dict containing:
            - status: "success" or "error"
            - enrichments: List of enrichment definitions
            - count: Number of enrichments

    Raises:
        ConfigurationError: If config file is missing or invalid
    """
    # Load configuration
    try:
        config_data = load_config(config_path)
    except FileNotFoundError:
        raise ConfigurationError(f"Configuration file not found: {config_path}")
    except yaml.YAMLError as e:
        raise ConfigurationError(f"Invalid YAML in config file: {e}")

    # Get enrichments
    if 'enrichments' not in config_data or not config_data['enrichments']:
        return {
            'status': 'success',
            'enrichments': [],
            'count': 0
        }

    # Extract enrichment info
    enrichment_list = []
    for enrichment in config_data['enrichments']:
        if not isinstance(enrichment, dict):
            continue  # skip strings or other non-dict items (e.g. bare names in config)
        enrichment_info = {
            'name': enrichment.get('name'),
            'description': enrichment.get('description', 'No description'),
            'model': enrichment.get('model', config_data.get('default_model', 'gpt-4o-mini')),
            'output_column': enrichment.get('output_column'),
            'output_table': enrichment.get('output_table'),
        }
        enrichment_list.append(enrichment_info)

    return {
        'status': 'success',
        'enrichments': enrichment_list,
        'count': len(enrichment_list)
    }

async def run_export(
    config_path: str,
    export_type: str,
    output_dir: Optional[str] = None,
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Export enriched data in various formats.

    Args:
        config_path: Path to YAML configuration file
        export_type: Type of export to run
        output_dir: Optional override for output directory
        verbose: Enable detailed logging

    Returns:
        Dict containing:
            - status: "success" or "error"
            - export_type: Type of export performed
            - output_dir: Directory where files were exported
            - files_exported: Number of files exported
    """
    setup_logging(verbose)

    # Load config
    try:
        with open(config_path, 'r') as f:
            config_data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigurationError(f"Configuration file not found: {config_path}")
    except yaml.YAMLError as e:
        raise ConfigurationError(f"Invalid YAML in config file: {e}")

    # Resolve output directory
    final_output_dir = os.path.expanduser(
        output_dir or config_data.get('output_dir', './exports')
    )

    # Resolve database path
    db_path = os.path.expanduser(config_data['database'])

    # Run export
    from ..export_operations import export_documents

    result = export_documents(
        db_path=db_path,
        config=config_data,
        output_dir=final_output_dir,
        export_name=export_type
    )

    return {
        'status': 'success',
        'export_type': export_type,
        'output_dir': final_output_dir,
        'result': result
    }

def _build_enrichment_query(
    enrichment_config: Dict[str, Any],
    config_data: Dict[str, Any],
    strategy: Any,
    rowid: Optional[int] = None,
    sha1: Optional[str] = None,
    limit: Optional[int] = None,
    overwrite: bool = False,
    where_clause: Optional[str] = None,
    override_query: Optional[str] = None,
) -> str:
    """Build SQL query for enrichment based on filters."""

    # Use override query if provided (CLI --query flag)
    if override_query:
        query = override_query
        logging.info(f"Using override query from CLI")
    else:
        # Get base query from config
        query_name = enrichment_config['input']['query']
        if query_name in config_data.get('sql_queries', {}):
            query = config_data['sql_queries'][query_name]
        else:
            query = query_name  # Use directly if it's raw SQL

    query = query.strip().rstrip(";")

    # Ensure rowid is available for point selection and deterministic ordering.
    if not re.search(r'\browid\b', query, re.IGNORECASE):
        if re.search(r'(SELECT\s+)\*(\s+FROM)', query, re.IGNORECASE):
            query = re.sub(
                r'(SELECT\s+)\*(\s+FROM)',
                r'\1rowid, *\2',
                query,
                flags=re.IGNORECASE
            )
        elif rowid is not None:
            query = re.sub(
                r'\bSELECT\s+(DISTINCT\s+)?',
                lambda match: f"SELECT {match.group(1) or ''}rowid, ",
                query,
                count=1,
                flags=re.IGNORECASE,
            )

    # Handle overwrite mode
    if overwrite and enrichment_config.get('output_columns'):
        output_col = enrichment_config['output_columns'][0]
        normalized_query = ' '.join(query.split())

        # Remove NULL filters
        pattern = rf'WHERE\s+{re.escape(output_col)}\s+IS\s+NULL(?=\s|$)'
        if re.search(pattern, normalized_query, re.IGNORECASE):
            query = re.sub(pattern, 'WHERE 1=1', query, flags=re.IGNORECASE | re.MULTILINE)

        pattern = rf'AND\s+{re.escape(output_col)}\s+IS\s+NULL(?=\s|$)'
        if re.search(pattern, normalized_query, re.IGNORECASE):
            query = re.sub(pattern, '', query, flags=re.IGNORECASE | re.MULTILINE)

    selectors = []
    if rowid is not None:
        selectors.append(f"_doctrail_base.rowid = {int(rowid)}")
    if sha1 is not None:
        escaped_sha1 = str(sha1).replace("'", "''")
        key_column_ref = _quote_identifier(strategy.key_column, "key column")
        selectors.append(f"_doctrail_base.{key_column_ref} = '{escaped_sha1}'")
    if where_clause:
        source_table_ref = _quote_identifier(strategy.input_table, "source table")
        key_column_ref = _quote_identifier(strategy.key_column, "key column")
        selectors.append(
            "EXISTS ("
            f"SELECT 1 FROM {source_table_ref} AS _doctrail_source "
            f"WHERE _doctrail_source.{key_column_ref} = _doctrail_base.{key_column_ref} "
            f"AND ({where_clause})"
            ")"
        )

    if selectors:
        query = (
            "WITH _doctrail_base AS ("
            f"{query}"
            ") "
            "SELECT _doctrail_base.* FROM _doctrail_base "
            "WHERE " + " AND ".join(selectors)
        )

    # Add ORDER BY if not present
    if 'ORDER BY' not in query.upper():
        limit_match = re.search(r'\s+LIMIT\s+\d+', query, re.IGNORECASE)
        if limit_match:
            query = query[:limit_match.start()] + ' ORDER BY rowid' + query[limit_match.start():]
        else:
            query = query.rstrip() + ' ORDER BY rowid'

    # Add LIMIT if specified
    if limit:
        if 'LIMIT' not in query.upper():
            query = f"{query} LIMIT {limit}"
        else:
            query = re.sub(r'LIMIT\s+\d+', f'LIMIT {limit}', query, flags=re.IGNORECASE)

    return query

def _resolve_models(
    enrichment_config: Dict[str, Any],
    config_data: Dict[str, Any],
    model_override: Optional[str] = None,
) -> List[str]:
    """Resolve model names from configuration."""

    if model_override:
        return [model_override]

    models = enrichment_config.get('model', config_data.get('default_model', 'gpt-4o-mini'))
    if isinstance(models, str):
        models = [models]

    # Resolve model references
    resolved_models = []
    models_config = config_data.get('models', {})

    for model_ref in models:
        if model_ref in models_config:
            model_cfg = models_config[model_ref]
            if isinstance(model_cfg, dict):
                actual_model = model_cfg.get('name', model_ref)
                resolved_models.append(actual_model)
            else:
                resolved_models.append(model_cfg)
        else:
            resolved_models.append(model_ref)

    return resolved_models

def _estimate_enrichment_cost(
    enrichment_config: Dict[str, Any],
    results: List[Dict[str, Any]],
    model: str,
    rows_to_process: int,
    execution_mode: str = "sync",
) -> Dict[str, Any]:
    """Estimate cost for enrichment operation."""

    input_columns = enrichment_config['input'].get('input_columns', [])
    sample_row = results[0] if results else {}

    # Parse input columns for sample
    input_columns_sample = {}
    for col in input_columns:
        col_name = col.split(':')[0]
        if '.' in col_name:
            _, col_only = col_name.split('.', 1)
            if col_only in sample_row:
                input_columns_sample[col_name] = sample_row[col_only]
        elif col_name in sample_row:
            input_columns_sample[col_name] = sample_row[col_name]

    prompt_template = enrichment_config.get('prompt', '')
    schema = enrichment_config.get('schema', {})

    total_cost, breakdown = estimate_enrichment_cost(
        model=model,
        prompt_template=prompt_template,
        input_columns_sample=input_columns_sample,
        schema=schema,
        num_rows=len(results),
        rows_to_process=rows_to_process,
        execution_mode=execution_mode,
    )

    return {
        'total_cost': total_cost,
        'breakdown': breakdown
    }

async def run_icr(
    config_path: str,
    enrichment_name: str,
    models: List[str],
    sample_size: Optional[int] = None,
    stratify_by: Optional[str] = None,
    seed: Optional[int] = None,
    db_path: Optional[str] = None,
    overwrite: bool = False,
    skip_cost_check: bool = False,
    cost_threshold: float = 5.0,
    verbose: bool = False,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run intercoder reliability analysis: sample rows, enrich with multiple models,
    and persist the sample for later reporting.

    Args:
        config_path: Path to YAML configuration file (or merged temp config)
        enrichment_name: Name of the enrichment task to run as ICR
        models: List of model identifiers to use as coders
        sample_size: Number of rows to sample (None = all matching rows)
        stratify_by: Field name in enrichments table to stratify sample by
        seed: Random seed for reproducible sampling
        db_path: Database path override
        overwrite: Re-run models that already have codings
        skip_cost_check: Skip cost confirmation
        cost_threshold: Cost threshold for confirmation
        verbose: Verbose logging
        project: Project name to tag enrichments

    Returns:
        Dict with sample_id, models_run, per-model counts, sample_size
    """
    setup_logging(verbose)

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
    else:
        if 'database' not in config_data:
            raise ConfigurationError(ERROR_NO_DATABASE)
        actual_db_path = os.path.expanduser(config_data['database'])

    if not os.path.exists(actual_db_path):
        raise DatabaseError(f"Database file not found: {actual_db_path}")

    # Find the enrichment config
    enrichment_configs = config_data.get('enrichments', [])
    enrichment_config = None
    for ec in enrichment_configs:
        if ec.get('name') == enrichment_name:
            enrichment_config = ec
            break

    if enrichment_config is None:
        available = [e.get('name') for e in enrichment_configs]
        raise EnrichmentError(
            f"Enrichment '{enrichment_name}' not found. "
            f"Available: {', '.join(available)}"
        )

    # Resolve config-level key_column (per-database default)
    from ..constants import DEFAULT_KEY_COLUMN
    config_key_column = config_data.get('key_column', DEFAULT_KEY_COLUMN)

    # Execute the enrichment's SQL query to get candidate rows
    from ..enrichment_config import prepare_enrichment_for_processing
    default_table = config_data.get('default_table', DEFAULT_TABLE_NAME)
    sql_queries = config_data.get('sql_queries', {})
    strategy, config_errors = prepare_enrichment_for_processing(
        enrichment_config, default_table, sql_queries,
        config_key_column=config_key_column
    )
    if config_errors:
        raise ConfigurationError(
            f"Config errors in '{enrichment_name}': " + "; ".join(config_errors)
        )

    query = _build_enrichment_query(enrichment_config, config_data, strategy)
    all_rows = execute_query(actual_db_path, query)

    if not all_rows:
        raise EnrichmentError(f"No rows returned by query for enrichment '{enrichment_name}'")

    # Sample rows
    key_col = strategy.key_column
    if sample_size and sample_size < len(all_rows):
        sampled_rows = _sample_rows(
            rows=all_rows,
            sample_size=sample_size,
            stratify_by=stratify_by,
            db_path=actual_db_path,
            enrichment_name=enrichment_name,
            seed=seed,
            key_column=key_col,
        )
    else:
        sampled_rows = all_rows

    # Persist the sample
    sample_id = _persist_icr_sample(
        db_path=actual_db_path,
        enrichment_name=enrichment_name,
        rows=sampled_rows,
        seed=seed,
        sample_size=len(sampled_rows),
        stratify_by=stratify_by,
        key_column=key_col,
    )

    # Build override_query to restrict enrichment to sampled keys.
    # We wrap the original enrichment query as a CTE so that all JOINs,
    # CASE expressions, and computed columns are preserved — the old approach
    # of SELECT * FROM raw_table lost everything the query computed.
    sha1_list = [row[key_col] for row in sampled_rows if row.get(key_col) is not None]
    if not sha1_list:
        raise EnrichmentError(f"Sampled rows have no '{key_col}' column — ICR requires keyed data (key_column={key_col})")

    original_query = _build_enrichment_query(enrichment_config, config_data, strategy)
    override_query = _build_icr_override_query(original_query, key_col, sha1_list)

    # Set models on the enrichment config so run_enrichment loops over them
    enrichment_config['model'] = models

    # Re-write the merged config with updated enrichment
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
        yaml.dump(config_data, f)
        merged_path = f.name

    try:
        result = await run_enrichment(
            config_path=merged_path,
            enrichments=[enrichment_name],
            db_path=actual_db_path,
            overwrite=overwrite,
            skip_cost_check=skip_cost_check,
            cost_threshold=cost_threshold,
            verbose=verbose,
            override_query=override_query,
            project=project,
        )
    finally:
        try:
            os.unlink(merged_path)
        except Exception:
            pass

    return {
        'status': result.get('status', 'success'),
        'sample_id': sample_id,
        'sample_size': len(sampled_rows),
        'models': models,
        'total_processed': result.get('total_processed', 0),
        'errors': result.get('errors', []),
    }

def _sample_rows(
    rows: List[Dict],
    sample_size: int,
    stratify_by: Optional[str],
    db_path: Optional[str],
    enrichment_name: Optional[str],
    seed: Optional[int],
    key_column: str = "sha1",
) -> List[Dict]:
    """Sample rows with optional stratification by an existing enrichment field."""
    rng = random.Random(seed)

    if not stratify_by or not db_path:
        return rng.sample(rows, min(sample_size, len(rows)))

    # Stratified sampling: look up the stratification field from the enrichments table
    from ..db_operations import get_icr_codings
    key_values = [r[key_column] for r in rows if r.get(key_column) is not None]
    codings = get_icr_codings(
        db_path=db_path,
        field_name=stratify_by,
        enrichment_name=enrichment_name,
        sha1s=key_values,
        key_column=key_column,
    )

    # Build stratum lookup (key_value → most recent value)
    stratum_map: Dict[str, str] = {}
    for c in codings:
        stratum_map[c['key_value']] = c['value']

    # Group rows by stratum
    strata: Dict[str, List[Dict]] = {}
    for row in rows:
        s = stratum_map.get(row.get(key_column), '__unknown__')
        strata.setdefault(s, []).append(row)

    # Proportional allocation
    sampled = []
    total = sum(len(v) for v in strata.values())
    for stratum_name, stratum_rows in strata.items():
        stratum_n = max(1, round(sample_size * len(stratum_rows) / total))
        stratum_n = min(stratum_n, len(stratum_rows))
        sampled.extend(rng.sample(stratum_rows, stratum_n))

    # Trim if over-sampled due to rounding
    if len(sampled) > sample_size:
        sampled = rng.sample(sampled, sample_size)

    return sampled

def _persist_icr_sample(
    db_path: str,
    enrichment_name: str,
    rows: List[Dict],
    seed: Optional[int],
    sample_size: int,
    stratify_by: Optional[str] = None,
    key_column: str = "sha1",
) -> str:
    """Store selected key values in icr_samples table. Returns sample_id."""
    from ..db_operations import ensure_icr_samples_table, get_db_connection
    import sqlite3
    from datetime import datetime

    ensure_icr_samples_table(db_path)
    sample_id = str(uuid.uuid4())
    now = datetime.now().isoformat()

    # If stratify_by, try to load strata values
    stratum_map: Dict[str, str] = {}
    if stratify_by:
        from ..db_operations import get_icr_codings
        key_values = [r[key_column] for r in rows if r.get(key_column) is not None]
        codings = get_icr_codings(db_path, field_name=stratify_by, sha1s=key_values, key_column=key_column)
        for c in codings:
            stratum_map[c['key_value']] = c['value']

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        for row in rows:
            key_val = row.get(key_column)
            if key_val is None:
                continue
            stratum = stratum_map.get(str(key_val))
            cursor.execute(f"""
                INSERT INTO {ICR_SAMPLES_TABLE}
                (sample_id, enrichment_name, key_value, key_column, stratum, seed, sample_size, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (sample_id, enrichment_name, key_val, key_column, stratum, seed, sample_size, now))
        conn.commit()

    logging.info(f"Persisted ICR sample {sample_id[:8]}... ({len(rows)} rows)")
    return sample_id


def _kappa_label_sort_key(value: Any) -> tuple[int, Any]:
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        try:
            return (1, float(value))
        except (TypeError, ValueError):
            return (2, "" if value is None else str(value))


def _cohen_kappa(y1: List[Any], y2: List[Any], *, weighted: bool = False) -> float:
    if len(y1) != len(y2):
        raise ValueError("Cohen's kappa requires equal-length coding vectors")
    if not y1:
        raise ValueError("Cohen's kappa requires at least one coded item")

    labels = sorted(set(y1) | set(y2), key=_kappa_label_sort_key)
    if len(labels) == 1:
        return 1.0

    label_index = {label: idx for idx, label in enumerate(labels)}
    size = len(labels)
    matrix = [[0 for _ in range(size)] for _ in range(size)]
    for a, b in zip(y1, y2):
        matrix[label_index[a]][label_index[b]] += 1

    total = len(y1)
    row_totals = [sum(row) for row in matrix]
    col_totals = [sum(matrix[row][col] for row in range(size)) for col in range(size)]

    if weighted:
        denominator = size - 1
        weights = [
            [abs(row - col) / denominator for col in range(size)]
            for row in range(size)
        ]
    else:
        weights = [
            [0 if row == col else 1 for col in range(size)]
            for row in range(size)
        ]

    observed = sum(
        weights[row][col] * matrix[row][col]
        for row in range(size)
        for col in range(size)
    ) / total
    expected = sum(
        weights[row][col] * row_totals[row] * col_totals[col]
        for row in range(size)
        for col in range(size)
    ) / (total * total)

    if expected == 0:
        return 1.0 if observed == 0 else 0.0
    return 1 - (observed / expected)


async def run_icr_report(
    db_path: str,
    field_name: str,
    enrichment_name: Optional[str] = None,
    models: Optional[List[str]] = None,
    sample_id: Optional[str] = None,
    level_of_measurement: Optional[str] = None,
    key_column: str = "sha1",
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Compute intercoder reliability statistics from enrichment codings.

    Args:
        db_path: Path to database
        field_name: The enrichment field to analyse (e.g. 'hostility_level')
        enrichment_name: Filter by enrichment name
        models: Specific models to compare (None = all models found)
        sample_id: Filter to a specific ICR sample
        level_of_measurement: 'nominal', 'ordinal', or 'interval' (auto-detected if None)
        key_column: Key column name (default 'sha1', configurable per-database)
        verbose: Verbose logging

    Returns:
        Dict with alpha, pairwise kappa, distributions, agreement rates
    """
    setup_logging(verbose)
    db_path = os.path.expanduser(db_path)

    if not os.path.exists(db_path):
        raise DatabaseError(f"Database not found: {db_path}")

    # Optional: filter keys by sample_id
    key_filter = None
    if sample_id:
        from ..db_operations import get_db_connection
        import sqlite3
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            # Use key_value column (migrated from sha1)
            cursor.execute(f"PRAGMA table_info({ICR_SAMPLES_TABLE})")
            columns = [row[1] for row in cursor.fetchall()]
            key_col_name = 'key_value' if 'key_value' in columns else 'sha1'
            cursor.execute(
                f"SELECT DISTINCT {key_col_name} FROM {ICR_SAMPLES_TABLE} WHERE sample_id = ?",
                (sample_id,)
            )
            key_filter = [row[0] for row in cursor.fetchall()]
            if not key_filter:
                raise EnrichmentError(f"No rows found for sample_id '{sample_id}'")

    # Get all codings
    from ..db_operations import get_icr_codings
    codings = get_icr_codings(
        db_path=db_path,
        field_name=field_name,
        enrichment_name=enrichment_name,
        models=models,
        sha1s=key_filter,
        key_column=key_column,
    )

    if not codings:
        raise EnrichmentError(
            f"No codings found for field '{field_name}'"
            + (f" (enrichment: {enrichment_name})" if enrichment_name else "")
        )

    # Discover models present
    discovered_models = sorted(set(c['model'] for c in codings))
    if len(discovered_models) < 2:
        raise EnrichmentError(
            f"ICR requires at least 2 coders, found {len(discovered_models)}: {discovered_models}"
        )

    # Build reliability matrix: rows = items (key_value), cols = coders (models)
    # Value = coding value
    from collections import defaultdict
    items: Dict[str, Dict[str, str]] = defaultdict(dict)
    for c in codings:
        items[c['key_value']][c['model']] = c['value']

    # Only keep items coded by all models
    complete_items = {
        key_value: vals for key_value, vals in items.items()
        if len(vals) == len(discovered_models)
    }

    if not complete_items:
        raise EnrichmentError(
            "No items have codings from all models. "
            f"Models found: {discovered_models}"
        )

    # Auto-detect measurement level
    all_values = [v for vals in complete_items.values() for v in vals.values()]
    if level_of_measurement is None:
        # If all values are integers (or string-ints), treat as ordinal
        try:
            [int(v) for v in all_values]
            level_of_measurement = 'ordinal'
        except (ValueError, TypeError):
            level_of_measurement = 'nominal'

    # Convert values for numeric levels
    if level_of_measurement in ('ordinal', 'interval'):
        def to_num(v):
            try:
                return int(v)
            except (ValueError, TypeError):
                try:
                    return float(v)
                except (ValueError, TypeError):
                    return None
    else:
        to_num = None

    # Compute Krippendorff's alpha
    alpha = None
    alpha_error = None
    try:
        import krippendorff
        import numpy as np

        label_map = None
        if not to_num:
            label_map = {
                label: idx
                for idx, label in enumerate(sorted(set(all_values), key=_kappa_label_sort_key))
            }

        # Build matrix: coders x items
        sorted_keys = sorted(complete_items.keys())
        matrix = []
        for model in discovered_models:
            row = []
            for key_value in sorted_keys:
                val = complete_items[key_value].get(model)
                if to_num:
                    val = to_num(val)
                else:
                    val = label_map[val]
                row.append(val)
            matrix.append(row)

        reliability_data = np.array(matrix, dtype=float)
        alpha = krippendorff.alpha(
            reliability_data=reliability_data,
            level_of_measurement=level_of_measurement,
        )
    except ImportError:
        alpha_error = "krippendorff package not installed"
    except Exception as e:
        alpha_error = str(e)

    # Pairwise Cohen's kappa + agreement rates
    pairwise = {}
    sorted_keys = sorted(complete_items.keys())
    for i, m1 in enumerate(discovered_models):
        for m2 in discovered_models[i + 1:]:
            y1 = [complete_items[key_value][m1] for key_value in sorted_keys]
            y2 = [complete_items[key_value][m2] for key_value in sorted_keys]

            # Agreement rate
            agree = sum(1 for a, b in zip(y1, y2) if a == b)
            rate = agree / len(y1) if y1 else 0

            pair_key = f"{m1} vs {m2}"
            pair_result = {
                'agreement_rate': round(rate, 4),
                'agree': agree,
                'total': len(y1),
            }

            try:
                kappa = _cohen_kappa(y1, y2, weighted=level_of_measurement == 'ordinal')
                pair_result['kappa'] = round(kappa, 4)
            except Exception as e:
                pair_result['kappa_error'] = str(e)

            pairwise[pair_key] = pair_result

    # Per-model distributions
    distributions: Dict[str, Dict[str, int]] = {}
    for model in discovered_models:
        dist: Dict[str, int] = defaultdict(int)
        for key_value in sorted_keys:
            val = complete_items[key_value].get(model, '__missing__')
            dist[val] += 1
        distributions[model] = dict(sorted(dist.items()))

    result = {
        'field_name': field_name,
        'level_of_measurement': level_of_measurement,
        'models': discovered_models,
        'n_items': len(complete_items),
        'n_items_total': len(items),
        'alpha': round(alpha, 4) if alpha is not None else None,
        'alpha_error': alpha_error,
        'pairwise': pairwise,
        'distributions': distributions,
    }

    if sample_id:
        result['sample_id'] = sample_id

    return result

__all__ = [

    'run_ingest',

    'list_enrichments',

    'run_export',

    '_build_enrichment_query',

    '_resolve_models',

    '_estimate_enrichment_cost',

    'run_icr',

    '_sample_rows',

    '_persist_icr_sample',

    'run_icr_report',

]
