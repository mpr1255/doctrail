import sys
import logging
import os
import sqlite3
from pathlib import Path
import tempfile
import platform
import socket
import re

import click
import yaml
import asyncio
from typing import List, Optional, Dict
from .constants import (
    SPINNER_CHARS, ERROR_NO_ENRICHMENTS, ERROR_NO_DATABASE,
    ERROR_ENRICHMENT_NOT_FOUND, DEFAULT_TABLE_NAME, DEFAULT_MODEL,
    LOG_FILE_PATH, SUCCESS_ENRICHMENT
)
from .db_operations import (
    get_db_connection, ensure_output_table, ensure_output_column, execute_query, execute_query_optimized
)
from .llm_operations import process_enrichment
from .core_utils import load_pydantic_model, parse_input_cols, load_config
from .utils.logging_config import setup_logging
from tqdm import tqdm
import threading
import time
import json
from datetime import datetime
from .ingest import process_ingest
from .plugins.zotero_ingester import process_zotero_ingest
from .utils.dependency_check import verify_dependencies
from .utils.cost_estimation import estimate_enrichment_cost, format_cost_estimate, should_confirm_cost, validate_model, get_supported_models, get_models_with_structured_output
from .utils.progress import create_progress_bar, SpinnerTqdm

CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])

# SpinnerTqdm is now imported from utils.progress

@click.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.option('--skip-requirements', is_flag=True, help='Skip system requirements check')
@click.pass_context
def cli(ctx, skip_requirements):
    """SQLite database enrichment tool."""
    # Store skip_requirements in context for subcommands
    ctx.ensure_object(dict)
    ctx.obj['skip_requirements'] = skip_requirements
    
    # Only show help if no command was provided
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(0)

def show_main_help():
    """Show the main help message."""
    click.echo("""
Usage: doctrail.py COMMAND [OPTIONS]

Commands:
  enrich    Enrich database content using LLM processing
  export    Export processed documents
  ingest    Ingest new documents into database

Examples:
  doctrail.py enrich --config config.yml --enrichments task1 task2 task3
  doctrail.py enrich --config config.yml --enrichments task1,task2,task3  
  doctrail.py enrich --config config.yml --enrichments task1 --enrichments task2
  doctrail.py enrich --config config.yml --enrichments compensation_type --model gpt-4o-mini --limit 10
  doctrail.py enrich --config config.yml --enrichments task1 --rowid 150 --overwrite
  doctrail.py enrich --config config.yml --enrichments task1 --sha1 5d8eaa7f2296c6db --overwrite
  doctrail.py export --config config.yml --export-type parallel-translation
  doctrail.py ingest --db-path ./database.db --input-dir ./docs
  doctrail.py ingest --db-path ./database.db --input-dir ./docs1 --input-dir ./docs2

Required Arguments by Command:
  enrich:
    --config CONFIG      Path to YAML config file
    --enrichments TASKS  Enrichment tasks (space-separated, comma-separated, or multiple flags)
    
  export:
    --config CONFIG      Path to YAML config file
    --export-type TYPE   Type of export (e.g., parallel-translation)
    
  ingest:
    --input-dir DIR     Directory containing documents to ingest (can be specified multiple times)
    AND EITHER:
    --db-path PATH     Path to SQLite database file
    OR
    --config CONFIG    Path to YAML config file

Optional Arguments by Command:
  enrich:
    --model MODEL      Override model for all enrichments (e.g., gpt-4o-mini)
    --limit N          Limit number of rows to process (cannot use with --rowid or --sha1)
    --rowid N          Process only specific row by rowid (cannot use with --limit or --sha1)
    --sha1 HASH        Process only specific row by sha1 (cannot use with --limit or --rowid)
    --db-path PATH     Override database path from config
    --overwrite        Overwrite existing data in output columns
    --verbose          Enable detailed logging
    --batch-size N     Override batch size for processing

  ingest:
    --table NAME       Target table name (default: documents)
    --force            Force operation even if schema mismatch detected
    --verbose          Enable detailed logging
    --plugin NAME      Use a custom ingestion plugin
    --cache-db PATH    [Plugin: doi_connector] Path to cache database
    --project NAME     [Plugin: doi_connector] Project name (REQUIRED - use "ALL" for all)
    --collection NAME  [Plugin: zotero_literature] Zotero collection name (REQUIRED)
    --api-key KEY      [Plugin: zotero_literature] Zotero API key
    --user-id ID       [Plugin: zotero_literature] Zotero user ID

Run 'doctrail.py COMMAND --help' for more information on a command.
""")


# Update the error handling for both commands
@cli.command()
@click.option('--config', required=True, help='Path to the configuration YAML file')
@click.option('--enrichments', required=True, multiple=True, help='Enrichment task names to run. Can specify multiple times: --enrichments task1 --enrichments task2, or space-separated: --enrichments task1 task2 task3')
@click.option('--limit', type=int, help='Limit number of rows to process')
@click.option('--overwrite', is_flag=True, help='Overwrite existing data in output columns')
@click.option('--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--log-updates', is_flag=True, help='Log updates to a file')
@click.option('--export', is_flag=True, help='Export documents')
@click.option('--output-dir', help='Output directory for exported documents')
@click.option('--formats', help='Comma-separated list of output formats')
@click.option('--table', help='Comma-separated list of tables to process. Use "all" for all tables')
@click.option('--model', help='Override the default model for all enrichments')
@click.option('--db-path', help='Override the database path from config')
@click.option('--batch-size', type=int, help='Override batch size for processing')
@click.option('--rowid', type=int, help='Process only a specific row by rowid')
@click.option('--sha1', help='Process only a specific row by sha1 hash')
@click.option('--truncate', is_flag=True, help='Truncate long inputs to fit model context window instead of failing')
@click.option('--skip-cost-check', is_flag=True, help='Skip cost estimation and confirmation')
@click.option('--cost-threshold', type=float, default=5.0, help='Cost threshold for confirmation prompt (default: $5.00)')
@click.pass_context
def enrich(ctx, config: str, enrichments: tuple, limit: Optional[int], overwrite: bool, 
        verbose: bool, log_updates: bool, export: bool, output_dir: str, 
        formats: str, table: Optional[str], model: Optional[str], 
        db_path: Optional[str], batch_size: Optional[int], rowid: Optional[int],
        sha1: Optional[str], truncate: bool, skip_cost_check: bool, cost_threshold: float):
    """Enrich database content using LLM processing."""
    
    if not config:
        from .utils.simple_error_handler import handle_cli_error
        handle_cli_error(click.UsageError("Missing required option '--config'"))
        ctx.exit(1)
    
    # Load config first to check for verbose setting
    # Use temporary minimal loader just to check verbose setting
    try:
        with open(config, 'r') as f:
            # Create a minimal YAML loader that ignores !import tags
            class MinimalLoader(yaml.SafeLoader):
                pass
            MinimalLoader.add_constructor('!import', lambda loader, node: None)
            config_data = yaml.load(f, Loader=MinimalLoader)
    except Exception as e:
        # If minimal loading fails, just use defaults
        config_data = {}
    
    # Use config verbose setting if CLI flag wasn't explicitly set
    # Check if --verbose was passed explicitly by looking at context
    ctx = click.get_current_context()
    verbose_was_passed = 'verbose' in ctx.params and ctx.params['verbose']
    
    if not verbose_was_passed and config_data.get('verbose', False):
        verbose = True
    
    # Set up logging based on final verbose value
    setup_logging(verbose)
    
    if not enrichments:
        raise click.BadParameter("--enrichments required")
    try:
        return asyncio.run(_async_cli(ctx, config, enrichments, limit, overwrite, verbose, log_updates, table, model, db_path, batch_size, rowid, sha1, truncate, skip_cost_check, cost_threshold))
    except KeyboardInterrupt:
        # Graceful shutdown message already printed by signal handler
        click.echo("\n✋ Enrichment interrupted by user.", err=True)
        click.echo("💡 Run the same command again to continue where you left off.", err=True)
        return 1  # Exit with error code

async def _async_cli(ctx, config: str, enrichments: tuple, limit: Optional[int], overwrite: bool, verbose: bool, log_updates: bool, table: Optional[str], model: Optional[str], db_path: Optional[str], batch_size: Optional[int], rowid: Optional[int], sha1: Optional[str], truncate: bool, skip_cost_check: bool, cost_threshold: float):
    # Set up logging based on verbosity
    setup_logging(verbose)
    results = [] 
    
    # Flag to track if we've been interrupted
    interrupted = False
    
    try:
        # Load configuration using the utility function that handles schemas
        config_data = load_config(config)
    except FileNotFoundError:
        raise click.UsageError(f"Configuration file not found: {config}")
    except yaml.YAMLError as e:
        raise click.UsageError(f"Invalid YAML in config file: {e}")
    
    # Get database configuration, with CLI override
    if db_path:  # CLI --db-path overrides config
        config_data['database'] = db_path
        actual_db_path = os.path.expanduser(db_path)
        logging.info(f"Database path overridden by CLI: {actual_db_path}")
    else:
        if 'database' not in config_data:
            raise click.UsageError(ERROR_NO_DATABASE)
        actual_db_path = os.path.expanduser(config_data['database'])
        logging.info(f"🗄️ Using database from config: {actual_db_path}")
    
    # Check if database exists
    if not os.path.exists(actual_db_path):
        raise click.UsageError(f"""Database file not found: {actual_db_path}

💡 To fix this:
   1. Create a database by running the 'ingest' command first, or
   2. Update the 'database' path in your config file to point to an existing database

Example:
   doctrail ingest --input-dir /path/to/documents --db-path {actual_db_path}""")
    
    # Set db_path for the rest of the function
    db_path = actual_db_path
    
    # Override model if specified via CLI
    if model:
        config_data['default_model'] = model
        logging.info(f"Default model overridden by CLI: {model}")
    
    # Override batch size if specified via CLI
    if batch_size:
        config_data['batch_size'] = batch_size
        logging.info(f"Batch size overridden by CLI: {batch_size}")
    
    # Validate that only one of limit, rowid, or sha1 is specified
    specified_filters = sum([limit is not None, rowid is not None, sha1 is not None])
    if specified_filters > 1:
        raise click.UsageError("Cannot specify multiple filters. Use only ONE of: --limit, --rowid, or --sha1.")
    
    # Process enrichments parameter (can be multiple values or comma-separated)
    requested_enrichments = []
    for enrichment_arg in enrichments:
        # Support both comma-separated and space-separated values
        if ',' in enrichment_arg:
            requested_enrichments.extend([e.strip() for e in enrichment_arg.split(',')])
        else:
            requested_enrichments.extend(enrichment_arg.split())
    
    # Remove duplicates while preserving order
    seen = set()
    requested_enrichments = [x for x in requested_enrichments if not (x in seen or seen.add(x))]
    
    
    # Validate enrichments section exists
    if 'enrichments' not in config_data:
        raise click.UsageError(ERROR_NO_ENRICHMENTS)
    
    if not config_data['enrichments']:
        raise click.UsageError("No enrichments defined in config file")
    
    # Find matching enrichment configs
    enrichment_configs = [e for e in config_data['enrichments'] if e['name'] in requested_enrichments]
    
    if not enrichment_configs:
        available = [e['name'] for e in config_data['enrichments']]
        
        # Find closest matches for each requested enrichment
        import difflib
        suggestions = []
        for requested in requested_enrichments:
            closest = difflib.get_close_matches(requested, available, n=1, cutoff=0.4)
            if closest:
                suggestions.append(f"{requested} → {closest[0]}")
        
        # Build error message
        error_parts = []
        
        # If we have suggestions, show them first
        if suggestions:
            error_parts.append(click.style("Did you mean?", fg='yellow', bold=True))
            error_parts.append(click.style(suggestions[0], fg='green'))
        
        # Show available enrichments
        error_parts.append("\nAvailable enrichments: " + ', '.join(available))
        
        # Add help hint
        error_parts.append(click.style("\nFor full help: doctrail enrich --help", fg='cyan', dim=True))
        
        raise click.UsageError('\n'.join(error_parts))
    
    try:
        # Import schema-driven configuration
        from .enrichment_config import prepare_enrichment_for_processing
        
        for enrichment_config in enrichment_configs:
            # NEW: Use schema-driven configuration system
            default_table = config_data.get('default_table', DEFAULT_TABLE_NAME)
            strategy, config_errors = prepare_enrichment_for_processing(enrichment_config, default_table)
            
            if config_errors:
                error_msg = f"Configuration errors in enrichment '{enrichment_config['name']}':\n"
                error_msg += "\n".join(f"  - {error}" for error in config_errors)
                raise click.UsageError(error_msg)
            
            # Validate schema if present
            if strategy.pydantic_model:
                from .pydantic_schema import create_pydantic_model_from_schema, SchemaConversionError
                
                try:
                    # Try to create the model to catch any schema issues
                    test_model = strategy.pydantic_model
                    logging.debug(f"Schema validation passed for enrichment '{enrichment_config['name']}'")
                except Exception as e:
                    error_msg = f"\n❌ Schema validation failed for enrichment '{enrichment_config['name']}':\n\n"
                    error_msg += f"  {str(e)}\n\n"
                    
                    # Check for common issues and provide helpful suggestions
                    if "__unique_items__" in str(e):
                        error_msg += "💡 Fix: Remove 'unique_items' from enum_list fields. Deduplication happens automatically.\n"
                    elif "Extra inputs are not permitted" in str(e):
                        error_msg += "💡 This usually means an unsupported field parameter was used in the schema.\n"
                    elif "number" in str(e).lower():
                        error_msg += "💡 Tip: Use 'integer' for whole numbers, not 'number' (which is float).\n"
                    
                    error_msg += "\n📖 See schema documentation: ./doctrail.py schema --help\n"
                    raise click.UsageError(error_msg)
            
            # Determine which table(s) to process
            tables_to_process = []
            if table:
                if table.lower() == 'all':
                    tables_to_process = list(config_data.get('tables', {}).keys())
                else:
                    tables_to_process = [t.strip() for t in table.split(',')]
            
            if not tables_to_process:
                # Use input table from strategy
                tables_to_process = [strategy.input_table]
            
            for table_name in tables_to_process:
                # Override input table if specified via CLI
                if table:
                    strategy.input_table = table_name
                
                # Use the determined table name for processing
                enrichment_config = enrichment_config.copy()
                enrichment_config['table'] = strategy.input_table
                
                # Use table's base_query if available
                if 'tables' in config_data and strategy.input_table in config_data['tables']:
                    base_query = config_data['tables'][strategy.input_table]['base_query']
                else:
                    query_name = enrichment_config['input']['query']
                    if query_name in config_data['sql_queries']:
                        base_query = config_data['sql_queries'][query_name]
                    else:
                        base_query = query_name  # Use directly if it's a raw SQL query
                 
                # Show task info (always show this)
                print(f"\n{'='*50}")
                print(f"🚀 Starting enrichment task: {enrichment_config['name']}")
                if verbose:
                    print(f"📝 Description: {enrichment_config.get('description', 'No description provided')}")
                    from .enrichment_config import get_storage_summary
                    print(f"📊 Storage: {get_storage_summary(strategy)}")
                
                # Use strategy for output configuration
                output_columns = strategy.output_columns
                output_table = strategy.output_table
                key_column = strategy.key_column
                
                if strategy.storage_mode == "separate_table":
                    # Import here to avoid circular imports
                    from .db_operations import ensure_output_table
                    
                    # Ensure the output table exists (derived tables support multiple models)
                    ensure_output_table(db_path, output_table, key_column, output_columns, is_derived_table=True)
                    logging.info(f"📊 Using separate output table: {output_table} (keyed by {key_column})")
                    
                    # For separate output tables, we don't need to check NULL filters in the source query
                    target_table_for_updates = output_table
                else:
                    # Traditional mode - update the source table
                    target_table_for_updates = strategy.input_table
                    
                    # Ensure output column exists in source table
                    from .db_operations import ensure_output_column
                    ensure_output_column(db_path, strategy.input_table, output_columns[0])
            
            # If overwrite mode and using same table, remove NULL filter to process all rows
            if overwrite and output_columns and not output_table:
                output_col = output_columns[0] if isinstance(output_columns, list) else output_columns
                # Remove WHERE clause filtering on output column being NULL
                # First, normalize whitespace to handle multiline queries
                normalized_query = ' '.join(base_query.split())
                
                # Pattern to match WHERE column IS NULL (more flexible)
                pattern = rf'WHERE\s+{re.escape(output_col)}\s+IS\s+NULL(?=\s|$)'
                if re.search(pattern, normalized_query, re.IGNORECASE):
                    # Replace with WHERE 1=1 to maintain query structure
                    base_query = re.sub(pattern, 'WHERE 1=1', base_query, flags=re.IGNORECASE | re.MULTILINE)
                    logging.debug(f"Overwrite mode: Removed NULL filter for {output_col}")
                
                # Also handle AND conditions
                pattern = rf'AND\s+{re.escape(output_col)}\s+IS\s+NULL(?=\s|$)'
                if re.search(pattern, normalized_query, re.IGNORECASE):
                    base_query = re.sub(pattern, '', base_query, flags=re.IGNORECASE | re.MULTILINE)
                    logging.debug(f"Overwrite mode: Removed AND NULL filter for {output_col}")
            
            # If APPEND mode and using same table, add NULL filter to skip rows with existing values
            elif not overwrite and output_columns and not output_table:
                output_col = output_columns[0] if isinstance(output_columns, list) else output_columns
                normalized_query = ' '.join(base_query.split())
                
                # Check if NULL filter already exists
                has_null_filter = bool(re.search(rf'{re.escape(output_col)}\s+IS\s+NULL', normalized_query, re.IGNORECASE))
                
                if not has_null_filter:
                    # Add NULL filter to skip rows that already have values
                    if 'WHERE' in base_query.upper():
                        # Query already has WHERE clause, add AND condition
                        base_query = re.sub(r'(\s+WHERE\s+)', rf'\1{output_col} IS NULL AND ', base_query, flags=re.IGNORECASE)
                        logging.debug(f"Append mode: Added NULL filter for {output_col} with AND")
                    else:
                        # Query doesn't have WHERE clause, add one
                        # Insert before ORDER BY if it exists, otherwise at the end
                        if 'ORDER BY' in base_query.upper():
                            # Handle both space and newline before ORDER BY
                            base_query = re.sub(r'(\s*ORDER\s+BY)', rf' WHERE {output_col} IS NULL\n\1', base_query, flags=re.IGNORECASE)
                        else:
                            base_query = base_query.strip() + f' WHERE {output_col} IS NULL'
                        logging.debug(f"Append mode: Added WHERE NULL filter for {output_col}")
            
            # Handle --rowid or --sha1 filter (overrides everything else)
            if rowid is not None or sha1 is not None:
                # Use the table name from enrichment config
                target_table = enrichment_config.get('table', 'documents')
                if rowid is not None:
                    query = f"SELECT rowid, * FROM {target_table} WHERE rowid = {rowid}"
                    print(f"🎯 ROWID MODE: Processing only row {rowid}")
                else:  # sha1 is not None
                    # Include rowid in SELECT for sha1 queries too
                    query = f"SELECT rowid, * FROM {target_table} WHERE sha1 = '{sha1}'"
                    print(f"🎯 SHA1 MODE: Processing only row with sha1 {sha1[:8]}...")
            else:
                # Start with base query
                query = base_query
                
                # First, ensure ORDER BY rowid if not present
                if 'ORDER BY' not in query.upper():
                    # If query has LIMIT, insert ORDER BY before it
                    limit_match = re.search(r'\s+LIMIT\s+\d+', query, re.IGNORECASE)
                    if limit_match:
                        # Insert ORDER BY before LIMIT
                        query = query[:limit_match.start()] + ' ORDER BY rowid' + query[limit_match.start():]
                    else:
                        # No LIMIT yet, just append ORDER BY
                        query = query.rstrip() + ' ORDER BY rowid'
                
                # Then handle LIMIT if specified
                if limit:
                    # Check if LIMIT already exists
                    if 'LIMIT' not in query.upper():
                        query = f"{query} LIMIT {limit}"
                    else:
                        # Replace existing limit
                        query = re.sub(r'LIMIT\s+\d+', f'LIMIT {limit}', query, flags=re.IGNORECASE)
                
            
            # Validate output columns were found
            if not output_columns:
                raise ValueError(f"Enrichment {enrichment_config['name']} must specify either 'output_column' or 'output_columns'")
            
            # Ensure all output columns exist BEFORE running the query
            # Only do this for direct column mode (not for separate output tables)
            if not strategy or strategy.storage_mode == "direct_column":
                for column in output_columns:
                    if column:  # Skip None values
                        ensure_output_column(db_path, enrichment_config['table'], column)
            
            if verbose:
                logging.info(f"🔍 Using query: {query}")
            
            # Show mode (always show this)
            if overwrite:
                print("🔄 OVERWRITE MODE: Will process ALL rows returned by query, replacing existing values")
            else:
                print("➕ APPEND MODE: Will skip rows that already have values")
            
            # Validate input configuration first
            if 'input' not in enrichment_config:
                raise click.BadParameter(f"Enrichment {enrichment_config['name']} missing 'input' configuration")
            
            input_config = enrichment_config['input']
            
            # Ensure query includes rowid for proper processing
            query = ensure_rowid_in_query(query)
            
            # Use optimized query execution if input_columns are specified
            input_columns = input_config.get('input_columns', ['raw_content'])
            if input_columns and len(input_columns) > 0:
                # Use optimized execution that fetches only needed columns
                results = execute_query_optimized(db_path, query, input_columns)
            else:
                # Fall back to standard execution
                results = execute_query(db_path, query)
            # Always show total rows retrieved
            total_rows = len(results)
            print(f"📊 Retrieved {total_rows:,} rows from database")
            
            if verbose:
                logging.info(f"Retrieved {total_rows} rows from database")
            if 'query' not in input_config:
                raise click.BadParameter(f"Enrichment {enrichment_config['name']} missing 'query' in input configuration")
            
            # Validate input columns if specified
            if 'input_columns' in input_config:
                input_columns = input_config['input_columns']
                if not isinstance(input_columns, list):
                    raise click.BadParameter(
                        f"Enrichment {enrichment_config['name']}: input_columns must be a list, got {type(input_columns)}"
                    )
            else:
                logging.warning(f"Enrichment {enrichment_config['name']} doesn't specify input_columns. Will use all columns from query.")
                # Optionally, set a default
                input_config['input_columns'] = ['raw_content']
                logging.info(f"Using default input column: raw_content")
            
            # Log the input configuration for debugging (verbose only)
            if verbose:
                logging.info(f"⚙️ Input configuration for {enrichment_config['name']}:")
                logging.info(f"   Query: {input_config['query']}")
                logging.info(f"   Columns: {input_config.get('input_columns', ['all'])}")
            
            
            # First, calculate how many rows will actually be processed
            output_columns = enrichment_config.get('output_columns', [enrichment_config.get('output_column')])
            if output_columns and not overwrite:
                # Count rows that need processing (don't have data yet)
                rows_to_process = [row for row in results if not row.get(output_columns[0])]
                actual_total = len(rows_to_process)
            else:
                # In overwrite mode, process all rows
                actual_total = len(results)
            
            # Handle model as string or list
            # CLI --model overrides config model
            if model:
                models = [model]  # CLI override is always single model
                logging.info(f"Using CLI model override: {model}")
            else:
                models = enrichment_config.get('model', config_data.get('default_model', 'gpt-4o-mini'))
                if isinstance(models, str):
                    models = [models]  # Convert to list for consistent handling
            
            # Validate all models are supported
            for model_name in models:
                if not validate_model(model_name):
                    supported_models = get_supported_models()
                    raise click.UsageError(
                        f"❌ Model '{model_name}' is not supported.\n\n"
                        f"Supported models:\n" + 
                        "\n".join(f"  - {m}" for m in sorted(supported_models)[:10]) +
                        (f"\n  ... and {len(supported_models)-10} more" if len(supported_models) > 10 else "") +
                        f"\n\nUpdate your config to use a supported model."
                    )
                    
# Removed overly restrictive structured output validation
                # Models can handle JSON output through various mechanisms
            
            # Validate multi-model usage
            if len(models) > 1:
                if strategy.storage_mode != "separate_table":
                    raise click.UsageError(
                        f"❌ Enrichment '{enrichment_config['name']}' specifies multiple models but targets the main table.\n"
                        f"   Multi-model comparison requires a separate output_table.\n"
                        f"   Either:\n"
                        f"   1. Use a single model for main table enrichments\n"
                        f"   2. Add 'output_table: {enrichment_config['name']}_results' to create a derived table"
                    )
            
            # Process with each model
            all_results = []
            for model_idx, model in enumerate(models):
                # Execute enrichment task with the retrieved results  
                if len(models) > 1:
                    pbar_desc = f"🤖 {enrichment_config['name']} [{model}]" if not verbose else f"Processing {enrichment_config['name']} with {model}"
                else:
                    pbar_desc = f"🤖 {enrichment_config['name']}" if not verbose else f"Processing {enrichment_config['name']}"
                
                # Check how many rows this model actually needs to process
                rows_to_process_for_model = len(results)
                
                if output_table and not overwrite:
                    # For derived tables, check what's already done for THIS specific model
                    with get_db_connection(db_path) as conn:
                        cursor = conn.cursor()
                        # Check if output table exists
                        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (output_table,))
                        if cursor.fetchone():
                            # Check if table has model_used column
                            cursor.execute(f"PRAGMA table_info({output_table})")
                            columns_info = cursor.fetchall()
                            column_names = [info[1] for info in columns_info]
                            has_model_column = "model_used" in column_names
                            
                            # Count how many rows this specific model has already processed
                            rows_done = 0
                            for row in results:
                                key_value = row.get(key_column, 'NO_KEY')
                                if has_model_column:
                                    cursor.execute(f"SELECT 1 FROM {output_table} WHERE {key_column} = ? AND model_used = ? LIMIT 1", 
                                                 (key_value, model))
                                else:
                                    cursor.execute(f"SELECT 1 FROM {output_table} WHERE {key_column} = ? LIMIT 1", (key_value,))
                                
                                if cursor.fetchone():
                                    rows_done += 1
                            rows_to_process_for_model = len(results) - rows_done
                elif not output_table and not overwrite:
                    # For direct column mode, count rows with existing data
                    output_col = output_columns[0] if output_columns else None
                    if output_col:
                        rows_to_process_for_model = sum(1 for row in results if not row.get(output_col))
                
                # Skip this model entirely if there's nothing to process
                if rows_to_process_for_model == 0:
                    print(f"✅ {model}: All rows already processed!")
                    continue
                
                # Cost estimation
                if not skip_cost_check:
                    # Get sample row for token counting
                    sample_row = results[0] if results else {}
                    # Parse input columns to get a sample
                    input_columns_sample = {}
                    for col in input_columns:
                        col_name = col.split(':')[0]  # Remove character limit
                        if '.' in col_name:
                            _, col_only = col_name.split('.', 1)
                            if col_only in sample_row:
                                input_columns_sample[col_name] = sample_row[col_only]
                        elif col_name in sample_row:
                            input_columns_sample[col_name] = sample_row[col_name]
                    
                    # Get prompt and schema
                    prompt_template = enrichment_config.get('prompt', '')
                    schema = enrichment_config.get('schema', {})
                    
                    # Estimate cost
                    total_cost, breakdown = estimate_enrichment_cost(
                        model=model,
                        prompt_template=prompt_template,
                        input_columns_sample=input_columns_sample,
                        schema=schema,
                        num_rows=len(results),
                        rows_to_process=rows_to_process_for_model
                    )
                    
                    # Show cost estimate
                    print(format_cost_estimate(breakdown))
                    
                    # Ask for confirmation if cost exceeds threshold
                    if should_confirm_cost(total_cost, cost_threshold):
                        if not click.confirm(f"\n💸 Estimated cost exceeds ${cost_threshold:.2f}. Continue?"):
                            print("❌ Enrichment cancelled by user.")
                            continue
                
                # Use spinner for non-verbose mode
                progress_bar = create_progress_bar(
                    total=rows_to_process_for_model,
                    desc=pbar_desc,
                    verbose=verbose
                )
                
                with progress_bar as pbar:
                    model_results = await process_enrichment(
                        results=results,  # Now we're passing actual database results!
                        enrichment_config=enrichment_config,
                        model=model,
                        pbar=pbar,
                        db_path=db_path,
                        table=strategy.input_table,
                        overwrite=overwrite,
                        config=config_data,
                        truncate=truncate or enrichment_config.get('truncate', False),
                        verbose=verbose,
                        output_table=output_table,
                        key_column=key_column,
                        enrichment_strategy=strategy,
                        is_multi_model=len(models) > 1
                    )
                    all_results.extend(model_results)
            
            results = all_results  # Use combined results for logging
            
            if log_updates:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                log_file = f"updates_{enrichment_config['name']}_{timestamp}.json"
                with open(log_file, 'w') as f:
                    json.dump(results, f, indent=2)
                logging.info(f"Updates logged to {log_file}")
    
    except asyncio.CancelledError:
        # This is triggered by our signal handler
        logging.info("\n🛑 Processing cancelled.")
        logging.info("📊 Progress saved - completed rows are in the database.")
        raise  # Re-raise to trigger the KeyboardInterrupt handling
    
    except Exception as e:
        # Handle other unexpected errors
        logging.error(f"\n❌ Unexpected error: {e}")
        raise

def ensure_output_column(db_path: str, table: str, column: str):
    """Ensure the output column exists in the table"""
    from .db_operations import get_db_connection
    
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # Check if column exists
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        
        if column not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            logging.info(f"Added column {column} to table {table}")
        
        conn.commit()

def get_zotero_config(config_data: Optional[Dict]) -> Dict:
    """Loads Zotero configuration from config file or environment variables."""
    zotero_cfg = {}
    if config_data and 'zotero' in config_data:
        zotero_cfg = config_data['zotero']

    # Override with environment variables if present
    zotero_cfg['api_key'] = os.environ.get('ZOTERO_API_KEY', zotero_cfg.get('api_key'))
    zotero_cfg['library_id'] = os.environ.get('ZOTERO_LIBRARY_ID', zotero_cfg.get('library_id'))
    zotero_cfg['library_type'] = os.environ.get('ZOTERO_LIBRARY_TYPE', zotero_cfg.get('library_type', 'user')) # Default to 'user'

    if not zotero_cfg.get('api_key') or not zotero_cfg.get('library_id'):
        raise click.UsageError(
            "Zotero API Key and Library ID must be provided either in the config file under a 'zotero:' key "
            "or via ZOTERO_API_KEY and ZOTERO_LIBRARY_ID environment variables."
        )
    return zotero_cfg

@cli.command()
@click.option('--config', help='Path to the configuration YAML file (optional, overrides other specific options)')
@click.option('--db-path', help='Path to SQLite database (Required if not in config)')
@click.option('--table', default="documents", help='Default table name for document ingestion (Zotero uses fixed tables)')
@click.option('--verbose', is_flag=True, help='Enable detailed logging')
# Local file ingest options
@click.option('--input-dir', multiple=True, help='Input directory for local document ingestion. Can be specified multiple times (Mutually exclusive with --zotero and --plugin)')
@click.option('--force', is_flag=True, help='Force local ingest even if schema mismatch detected')
@click.option('--overwrite', is_flag=True, help='Overwrite existing documents during local ingest')
@click.option('--limit', type=int, help='Limit number of files to process (for testing)')
@click.option('--include-pattern', help='Only process files matching this glob pattern (e.g., "*_meta.mht")')
@click.option('--exclude-pattern', help='Skip files matching this glob pattern (e.g., "*pristine*,*.json" for multiple patterns)')
@click.option('--readability', is_flag=True, help='Use readability library for cleaner HTML extraction (may miss content)')
@click.option('--html-extractor', type=click.Choice(['default', 'smart']), default='default', help='HTML extraction method: default (preserves all whitespace) or smart (intelligent paragraph handling)')
@click.option('--skip-garbage-check', is_flag=True, help='Skip garbage content detection for HTML files (useful for files with repetitive formatting)')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation prompts and proceed automatically')
@click.option('--fulltext', is_flag=True, help='Create full-text search (FTS) index after ingestion')
@click.option('--manifest', help='Path to manifest.json file containing metadata for files (auto-detects if not specified)')
# Zotero ingest options
@click.option('--zotero', is_flag=True, help='Enable Zotero ingestion mode (Mutually exclusive with --input-dir and --plugin)')
@click.option('--collection', help='Name of the Zotero collection to ingest (Required if --zotero is used)')
# Plugin ingest options
@click.option('--plugin', help='Name of the plugin to use for ingestion (Mutually exclusive with --input-dir and --zotero)')
@click.option('--plugin-dir', help='Directory containing custom plugins (optional)')
# Plugin-specific options (for doi_connector)
@click.option('--cache-db', help='[Plugin: doi_connector] Path to cache.sqlite database')
@click.option('--project', help='[Plugin: doi_connector] Project name to filter by (REQUIRED - use "ALL" for all projects)')
@click.option('--base-path', help='[Plugin: doi_connector] Base path for resolving relative file paths')
# Plugin-specific options (for zotero)
@click.option('--api-key', help='[Plugin: zotero] Zotero API key (or from env/config)')
@click.option('--user-id', help='[Plugin: zotero] Zotero user ID (or from env/config)')
@click.option('--zotero-dir', help='[Plugin: zotero] Path to Zotero data directory (default: ~/Zotero)')
@click.pass_context
def ingest(
    ctx,
    config: Optional[str],
    db_path: Optional[str],
    table: str, # Keep for local ingest, Zotero uses fixed tables
    verbose: bool,
    input_dir: tuple,  # Now a tuple for multiple values
    force: bool,
    overwrite: bool,
    limit: Optional[int],
    include_pattern: Optional[str],
    exclude_pattern: Optional[str],
    readability: bool,
    html_extractor: str,
    skip_garbage_check: bool,
    yes: bool,
    fulltext: bool,
    manifest: Optional[str],
    zotero: bool,
    collection: Optional[str],
    plugin: Optional[str],
    plugin_dir: Optional[str],
    cache_db: Optional[str],
    project: Optional[str],
    base_path: Optional[str],
    api_key: Optional[str],
    user_id: Optional[str],
    zotero_dir: Optional[str]
):
    """
    Ingest documents from local directories, Zotero collection, OR custom plugin.

    Local Mode: Provide --input-dir and --db-path (or config).
                You can specify multiple directories: --input-dir dir1 --input-dir dir2
    Zotero Mode: Provide --zotero, --collection, and --db-path (or config).
                 Zotero credentials should be in the config file or environment variables.
    Plugin Mode: Provide --plugin <plugin_name> and --db-path (or config).
                 Plugin-specific options like --cache-db and --project can be passed directly.
    
    Manifest Support:
    Place a manifest.json in your directory to add metadata during ingestion:
    {
      "file.html": {"url": "https://example.com", "author": "John Doe"}
    }
    Or use --manifest to specify a custom path. See docs/manifest-ingestion.md for details.
    
    Content Extraction:
    --readability: Use readability library for cleaner HTML extraction (may miss content)
    
    Manual Override Files:
    For edge cases where automatic extraction fails, create manual override files:
    - filename--good.txt: High-quality manual transcription (highest priority)
    - filename--ocr.txt: OCR-processed version  
    - filename--manual.txt: Any manually created version
    The tool will automatically use these instead of processing the original file.
    
    File Filtering:
    --include-pattern: Only process files matching this glob pattern (e.g., "*_meta.mht")
    --exclude-pattern: Skip files matching this pattern (e.g., "*pristine*,*.json")
    
    Plugin Examples:
    doctrail ingest --plugin doi_connector --db-path ./literature.db --cache-db=/path/to/cache.sqlite --project=my_project
    doctrail ingest --plugin zotero --db-path ./literature.db --collection "My Research"
    """
    # Check system dependencies - only for ingest command
    skip_requirements = ctx.obj.get('skip_requirements', False)
    if not verify_dependencies(skip_requirements):
        ctx.exit(1)
    
    setup_logging(verbose)
    config_data = None
    final_db_path = db_path # Use db_path from command line first

    if config:
        try:
            with open(config, 'r') as f:
                config_data = yaml.safe_load(f)
            logging.info(f"Loaded configuration from {config}")
            # Config overrides command line db_path if present
            final_db_path = config_data.get('database', final_db_path)
            # Config can set readability if not specified via CLI
            if not readability and config_data.get('readability', False):
                readability = True
                logging.info("Readability enabled via configuration file")
            # Config can set html_extractor if not specified via CLI
            if html_extractor == 'default' and config_data.get('html_extractor'):
                html_extractor = config_data.get('html_extractor')
                logging.info(f"HTML extractor set to '{html_extractor}' via configuration file")
            # Config can set skip_garbage_check if not specified via CLI
            if not skip_garbage_check and config_data.get('skip_garbage_check', False):
                skip_garbage_check = True
                logging.info("Garbage check skipping enabled via configuration file")
        except Exception as e:
            raise click.UsageError(f"Error loading config file '{config}': {e}")

    # --- Mode Validation ---
    modes = sum([bool(input_dir), bool(zotero), bool(plugin)])
    if modes != 1:
        raise click.UsageError("Must provide exactly ONE of: --input-dir (for local files), --zotero (for Zotero), or --plugin (for custom ingesters).")

    # --- DB Path Validation ---
    if not final_db_path:
        raise click.UsageError("Database path must be provided via --db-path or in the config file.")
    final_db_path = os.path.expanduser(final_db_path) # Expand ~

    # --- Zotero Specific Validation ---
    if zotero and not collection:
        raise click.UsageError("--collection is required when using --zotero.")

    # --- Local Ingest Validation ---
    if input_dir:
        for dir_path in input_dir:
            if not os.path.exists(dir_path):
                raise click.UsageError(f"Input directory does not exist: {dir_path}")

    # --- Plugin Mode ---
    if plugin:
        from .plugins import get_plugin, discover_plugins
        from pathlib import Path
        
        # Discover available plugins
        plugin_path = Path(plugin_dir) if plugin_dir else None
        available_plugins = discover_plugins(plugin_path)
        
        # Get the requested plugin
        plugin_instance = available_plugins.get(plugin)
        if not plugin_instance:
            available_names = list(available_plugins.keys())
            raise click.UsageError(
                f"Plugin '{plugin}' not found.\n\n"
                f"Available plugins: {', '.join(available_names) if available_names else 'None'}\n\n"
                f"To use a custom plugin, place it in:\n"
                f"  1. Current directory: ./plugins/{plugin}.py\n"
                f"  2. Specify with --plugin-dir: /path/to/plugins/{plugin}.py"
            )
        
        if verbose:
            logging.info(f"Using plugin: {plugin_instance.name}")
            logging.info(f"Description: {plugin_instance.description}")
        
        # Build plugin arguments from known plugin-specific options
        plugin_args = {}
        if cache_db:
            plugin_args['cache_db'] = cache_db
        if project:
            plugin_args['project'] = project
        if base_path:
            plugin_args['base_path'] = base_path
        if table and table != "documents":  # Pass table if non-default
            plugin_args['table'] = table
        # Zotero literature plugin arguments
        if collection:
            plugin_args['collection'] = collection
        if api_key:
            plugin_args['api_key'] = api_key
        if user_id:
            plugin_args['user_id'] = user_id
        if zotero_dir:
            plugin_args['zotero_dir'] = zotero_dir
        
        if plugin_args and verbose:
            logging.info(f"Plugin arguments: {plugin_args}")
        
        # Run the plugin
        return asyncio.run(plugin_instance.ingest(
            db_path=final_db_path,
            config=config_data or {},
            verbose=verbose,
            overwrite=overwrite,
            limit=limit,
            fulltext=fulltext,
            **plugin_args
        ))

    # --- Logging Parameters ---
    if verbose:
        logging.info(f"Starting ingest operation:")
        logging.info(f"  Database Path: {final_db_path}")
        if input_dir:
            logging.info(f"  Mode: Local Directory")
            if len(input_dir) == 1:
                logging.info(f"  Input Directory: {input_dir[0]}")
            else:
                logging.info(f"  Input Directories: {', '.join(input_dir)}")
            logging.info(f"  Table: {table}") # Use the provided table name for local
            logging.info(f"  Force: {force}")
            logging.info(f"  Overwrite: {overwrite}")
            logging.info(f"  Readability: {readability}")
        elif zotero:
            logging.info(f"  Mode: Zotero API")
            logging.info(f"  Collection: {collection}")
            # Zotero creds fetched below, table names are fixed (e.g., zotero_items, zotero_attachments)

        logging.info(f"  Verbose: {verbose}")

    # --- Dispatch ---
    if input_dir:
        # Process multiple directories
        total_processed = 0
        for idx, dir_path in enumerate(input_dir):
            if verbose:
                if len(input_dir) > 1:
                    logging.info(f"Processing directory {idx+1}/{len(input_dir)}: {dir_path}")
                logging.info(f"Starting local directory ingest process for {dir_path} to {final_db_path}, table '{table}'")
            
            # For multiple directories, only ask for confirmation on the first one
            use_yes = yes or (idx > 0)
            
            result = asyncio.run(process_ingest(
                db_path=final_db_path,
                input_dir=dir_path,
                table=table, # Pass the specified table name
                verbose=verbose,
                force=force,
                overwrite=overwrite,
                limit=limit,
                include_pattern=include_pattern,
                exclude_pattern=exclude_pattern,
                readability=readability,
                html_extractor=html_extractor,
                skip_garbage_check=skip_garbage_check,
                yes=use_yes,
                fulltext=fulltext,
                manifest_path=manifest
            ))
            total_processed += 1
        
        if len(input_dir) > 1:
            logging.info(f"\nCompleted processing {total_processed} directories")
        return result
    elif zotero:
        zotero_cfg = get_zotero_config(config_data)
        if verbose:
            logging.info(f"  Zotero Library ID: {zotero_cfg['library_id']}")
            logging.info(f"  Zotero Library Type: {zotero_cfg['library_type']}")
            logging.info(f"Starting Zotero ingest process for collection '{collection}' into database '{final_db_path}'...")

        # Zotero ingest uses fixed table names, defined within the function
        return asyncio.run(process_zotero_ingest(
            db_path=final_db_path, # Pass DB path
            api_key=zotero_cfg['api_key'],
            library_id=zotero_cfg['library_id'],
            library_type=zotero_cfg['library_type'],
            collection_name=collection,
            verbose=verbose,
            fulltext=fulltext
            # Removed table parameter here, handled internally
        ))
    else:
        # Should be caught by earlier validation
        logging.error("Invalid configuration: No input source specified.")
        raise click.UsageError("Internal error: No input source could be determined.")

@cli.command()
@click.option('--config', required=True, help='Path to the configuration YAML file')
@click.option('--export-type', required=True, help='Type of export to run (e.g., parallel-translation, case-summaries)')
@click.option('--output-dir', required=False, help='Override the default output directory from config')
@click.option('--verbose', is_flag=True, help='Enable verbose logging')
@click.pass_context
def export(ctx, config: str, export_type: str, output_dir: str, verbose: bool):
    """Export documents based on configuration."""
    
    # Set up logging FIRST
    setup_logging(verbose)

    # Load config
    with open(config, 'r') as f:
        config_data = yaml.safe_load(f)
    
    # Use output_dir from command line or config, expand user directory
    final_output_dir = os.path.expanduser(output_dir or config_data.get('output_dir', './exports'))
    
    from .export_operations import export_documents
    export_documents(
        db_path=os.path.expanduser(config_data['database']),
        config=config_data,
        output_dir=final_output_dir,
        export_name=export_type
    )

def validate_input_columns(results: List[dict], input_columns: List[str], enrichment_name: str) -> None:
    """Validate that all required input columns exist in the query results."""
    if not results:
        logging.warning(f"No results to validate columns against for {enrichment_name}")
        return
        
    available_columns = set(results[0].keys())
    missing_columns = [col for col in input_columns if col not in available_columns]
    
    if missing_columns:
        logging.error(f"Missing required input columns for {enrichment_name}: {missing_columns}")
        logging.error(f"Available columns: {sorted(available_columns)}")
        raise click.BadParameter(
            f"Enrichment {enrichment_name} requires columns that don't exist in the query results: {missing_columns}"
        )

def ensure_rowid_in_query(query: str) -> str:
    """
    Ensure that the query includes rowid in the SELECT clause.
    
    This is critical because the enrichment processing code expects rowid to be available
    for database updates. If a query uses 'SELECT *' without explicitly including rowid,
    it won't be available in the results, causing "NO_ROWID" errors.
    
    ## SQL Queries vs Input Columns Relationship
    
    The doctrail system has a two-stage data flow:
    
    1. **SQL Query Stage**: Filters which ROWS to process
       - The SQL query determines which documents/rows from the database will be processed
       - Should include `SELECT rowid, *` or `SELECT rowid, sha1, *` to ensure proper tracking
       - Examples:
         - `SELECT rowid, * FROM documents WHERE language = 'zh'` (only Chinese docs)
         - `SELECT rowid, * FROM documents WHERE processed IS NULL` (unprocessed docs)
         - `SELECT rowid, * FROM documents LIMIT 10` (first 10 docs)
    
    2. **Input Columns Stage**: Filters which DATA from each row goes to the LLM
       - The input_columns configuration determines what data from each selected row is sent to the LLM
       - Can include character limits to truncate long content
       - Examples:
         - `["raw_content"]` - send full content
         - `["raw_content:500"]` - send only first 500 characters
         - `["raw_content:500", "filename"]` - send truncated content + full filename
    
    This separation allows for:
    - Efficient filtering at the database level (SQL WHERE clauses)
    - Content optimization for LLM context limits (character truncation)
    - Processing only relevant documents while controlling LLM input size
    
    Args:
        query: The SQL query to check and modify if needed
        
    Returns:
        Modified query that includes rowid in the SELECT clause
    """
    # Check if query already includes rowid explicitly
    if re.search(r'\browid\b', query, re.IGNORECASE):
        logging.debug("Query already includes rowid explicitly")
        return query
    
    # Look for SELECT * pattern and replace with SELECT rowid, *
    select_star_pattern = r'(SELECT\s+)\*(\s+FROM)'
    if re.search(select_star_pattern, query, re.IGNORECASE):
        modified_query = re.sub(
            select_star_pattern, 
            r'\1rowid, *\2', 
            query, 
            flags=re.IGNORECASE
        )
        logging.debug(f"Modified query to include rowid: {query} -> {modified_query}")
        return modified_query
    
    # If it's not a SELECT * query, we assume rowid is already included or not needed
    logging.debug("Query doesn't use SELECT *, assuming rowid is handled appropriately")
    return query

if __name__ == '__main__':
    try:
        cli()
    except Exception as e:
        logging.error(f"Fatal error: {str(e)}", exc_info=True)
        sys.exit(1)