import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Type

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, create_model
from tqdm import tqdm

from .constants import (
    DEFAULT_API_SEMAPHORE_LIMIT, DEFAULT_DB_SEMAPHORE_LIMIT,
    TRANSLATION_ENRICHMENTS, MAX_RETRY_ATTEMPTS, DEFAULT_KEY_COLUMN
)
from .db_operations import (
    get_db_connection, store_raw_enrichment_response, ensure_enrichment_responses_table,
    update_output_table, update_database, checkpoint_wal, get_or_create_prompt_id
)
from .schema_managers import validate_with_schema, get_schema_prompt_instructions, SchemaValidationError, LanguageValidationError
from .core_utils import parse_input_columns_with_limits, apply_column_limits, detect_mojibake, try_fix_mojibake

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
    'gemini-2.0-flash': 1000000,  # 1M context window  
    'models/gemini-2.0-flash': 1000000,  # Alternative name
}

def estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token for most languages."""
    return len(text) // 4

def truncate_input_for_model(full_prompt: str, input_text: str, model: str, safety_margin: int = 2000) -> Tuple[str, bool]:
    """
    Truncate input_text if the full message would exceed model's context limit.
    Returns (truncated_input_text, was_truncated)
    """
    context_limit = MODEL_CONTEXT_LIMITS.get(model, 8192)  # Default to conservative limit
    
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

# Initialize clients
openai_client = AsyncOpenAI()
gemini_client = None
if GEMINI_AVAILABLE:
    gemini_api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_API_KEY")
    if gemini_api_key:
        # Suppress AFC messages by redirecting stdout/stderr during client init
        import sys
        import contextlib
        import io
        
        # Initialize without AFC filtering for now - we'll handle it per-call
        try:
            # Attempt to configure higher concurrency if possible
            import grpc
            options = [
                ('grpc.keepalive_time_ms', 10000),
                ('grpc.keepalive_timeout_ms', 5000),
                ('grpc.keepalive_permit_without_calls', True),
                ('grpc.http2.max_pings_without_data', 0),
                ('grpc.http2.min_time_between_pings_ms', 10000),
                ('grpc.http2.min_ping_interval_without_data_ms', 300000),
                ('grpc.max_concurrent_streams', 100),  # Try to increase concurrent streams
            ]
            gemini_client = genai.Client(api_key=gemini_api_key, transport_options=options)
        except:
            # Fallback to default client if transport options don't work
            gemini_client = genai.Client(api_key=gemini_api_key)
    else:
        logging.warning("Gemini API key not found in GOOGLE_AI_API_KEY or GEMINI_API_KEY environment variables")

async def call_llm(model: str, messages: list, system_prompt: str = None, verbose: bool = False) -> str:
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
        result_text = await provider.generate_text(
            messages=messages,
            temperature=0.0  # Default to deterministic output
        )
        
        # Check for mojibake
        if detect_mojibake(result_text):
            provider_name = type(provider).__name__
            logging.warning(f"⚠️  Mojibake detected in {provider_name} response (length: {len(result_text)})")
            # Try to fix it
            fixed_text = try_fix_mojibake(result_text)
            if fixed_text != result_text:
                logging.info("✅ Mojibake fixed successfully")
                result_text = fixed_text
            else:
                logging.warning("❌ Unable to fix mojibake automatically")
        
        return result_text
        
    except Exception as e:
        logging.error(f"LLM API call failed: {e}")
        raise

async def call_llm_structured(model: str, messages: List[Dict], pydantic_model: Type[BaseModel], 
                             system_prompt: str = None, verbose: bool = False, provider=None):
    """
    Make a structured LLM API call using provider-specific structured output APIs.
    
    Args:
        model: Model name
        messages: List of message dictionaries
        pydantic_model: Pydantic model class for response format
        system_prompt: Optional system prompt
        verbose: Enable verbose logging
        provider: Optional pre-created provider (for efficiency)
        
    Returns:
        Parsed Pydantic model instance
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
        result = await provider.generate_structured(
            messages=messages,
            pydantic_model=pydantic_model,
            temperature=0.0  # Default to deterministic output
        )
        
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
                       enrichment_config, output_schema=None, system_prompt=None, overwrite=False, config=None, truncate=False, verbose=False, output_table=None, key_column=DEFAULT_KEY_COLUMN, enrichment_strategy=None, suppress_progress_messages=False):
    """Process a batch of rows with the LLM"""
    if verbose:
        logging.info(f"Processing batch of {len(results)} rows")
    
    # Get or create prompt_id for tracking prompt versions
    enrichment_name = enrichment_config.get('name', 'unknown')
    prompt_id = get_or_create_prompt_id(db_path, enrichment_name, prompt, system_prompt, model)
    logging.debug(f"Using prompt_id: {prompt_id[:8]} for enrichment: {enrichment_name}")
    
    # Skip rows that already have data unless overwrite is True
    skipped_rows = []
    if not overwrite:
        # ALWAYS check enrichment_responses table for prior attempts
        # This is the authoritative record of "we already tried this"
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            enrichment_name = enrichment_config.get('name', 'unknown')
            
            # Check enrichment_responses for each document
            for row in results:
                sha1 = row.get('sha1', 'NO_SHA1')
                if sha1 != 'NO_SHA1':
                    # Check if we've already processed this sha1+enrichment+model combo
                    cursor.execute(
                        "SELECT 1 FROM enrichment_responses WHERE sha1 = ? AND enrichment_name = ? AND model_used = ? LIMIT 1",
                        (sha1, enrichment_name, model)
                    )
                    if cursor.fetchone():
                        skipped_rows.append({
                            'rowid': row.get('rowid', 'NO_ROWID'), 
                            'sha1': sha1, 
                            'original': f"already processed (enrichment_responses)", 
                            'updated': None
                        })
            
            # For backward compatibility, also check output location
            # This handles cases where enrichment_responses might be missing records
            if output_table:
                # Check if output table exists
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (output_table,))
                if cursor.fetchone():
                    # Check if table has model_used column (derived table)
                    cursor.execute(f"PRAGMA table_info({output_table})")
                    columns_info = cursor.fetchall()
                    column_names = [info[1] for info in columns_info]
                    has_model_column = "model_used" in column_names
                    
                    # Check for any rows not already skipped
                    skipped_sha1s = {r['sha1'] for r in skipped_rows}
                    for row in results:
                        sha1 = row.get('sha1', 'NO_SHA1')
                        if sha1 not in skipped_sha1s:
                            key_value = row.get(key_column, 'NO_KEY')
                            if has_model_column:
                                # For derived tables, check both key and model
                                cursor.execute(f"SELECT 1 FROM {output_table} WHERE {key_column} = ? AND model_used = ? LIMIT 1", 
                                             (key_value, model))
                            else:
                                # For regular tables, check just the key
                                cursor.execute(f"SELECT 1 FROM {output_table} WHERE {key_column} = ? LIMIT 1", (key_value,))
                            
                            if cursor.fetchone():
                                skipped_rows.append({
                                    'rowid': row.get('rowid', 'NO_ROWID'), 
                                    'sha1': sha1, 
                                    'original': f"exists in {output_table}" + (f" for model {model}" if has_model_column else ""), 
                                    'updated': None
                                })
            else:
                # For direct column mode, check source table column
                skipped_sha1s = {r['sha1'] for r in skipped_rows}
                additional_skipped = [
                    {'rowid': row.get('rowid', 'NO_ROWID'), 'sha1': row.get('sha1', 'NO_SHA1'), 'original': row.get(output_cols[0]), 'updated': None}
                    for row in results
                    if row.get(output_cols[0]) and row.get('sha1', 'NO_SHA1') not in skipped_sha1s
                ]
                skipped_rows.extend(additional_skipped)

    if skipped_rows and not suppress_progress_messages:
        if verbose:
            logging.info(f"⏭️  Skipping {len(skipped_rows)} rows with existing data. Use --overwrite to update these rows.")
        else:
            print(f"⏭️  Skipping {len(skipped_rows)} rows (already have data)")
        # Don't update progress bar for skipped rows - only count actual processing
    
    if overwrite and len(results) > 0:
        existing_data_count = 0
        # Count rows that have been processed before (in enrichment_responses)
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            enrichment_name = enrichment_config.get('name', 'unknown')
            
            for row in results:
                sha1 = row.get('sha1', 'NO_SHA1')
                if sha1 != 'NO_SHA1':
                    cursor.execute(
                        "SELECT 1 FROM enrichment_responses WHERE sha1 = ? AND enrichment_name = ? AND model_used = ? LIMIT 1",
                        (sha1, enrichment_name, model)
                    )
                    if cursor.fetchone():
                        existing_data_count += 1
        
        if existing_data_count > 0:
            if verbose:
                logging.info(f"🔄 Overwriting {existing_data_count} rows that have existing data")
            else:
                print(f"🔄 Processing {existing_data_count} rows (overwriting existing data)")
    
    # Filter out skipped rows - using both rowid and sha1 for comparison
    skipped_ids = {(r['rowid'], r['sha1']) for r in skipped_rows}
    rows_to_process = [row for row in results if (row.get('rowid', 'NO_ROWID'), row.get('sha1', 'NO_SHA1')) not in skipped_ids]
    
    if verbose:
        logging.info(f"Found {len(rows_to_process)} rows to process")
    else:
        if rows_to_process and not suppress_progress_messages:
            print(f"🔄 Processing {len(rows_to_process)} rows...")
    
    if not rows_to_process:
        if verbose:
            logging.warning("No rows left to process!")
        elif not suppress_progress_messages:
            print("✅ All rows already processed!")
        return skipped_rows

    # Set up concurrency limits for API and DB access
    semaphore = asyncio.Semaphore(DEFAULT_API_SEMAPHORE_LIMIT)  # Allow concurrent API calls
    db_semaphore = asyncio.Semaphore(DEFAULT_DB_SEMAPHORE_LIMIT)  # Limit database writes to prevent locks
    processed_results = []
    
    # Create provider once and reuse for all requests (much more efficient)
    llm_provider = None
    if enrichment_strategy and enrichment_strategy.pydantic_model:
        from .llm_providers.factory import get_llm_provider
        llm_provider = get_llm_provider(model)
        logging.debug(f"Created reusable {type(llm_provider).__name__} for {model}")
    
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
                    provider=llm_provider
                )
                
                if result:  # Store ALL results, including failures/nulls for audit trail
                    async with db_semaphore:
                        # DUAL STORAGE: 1. Store raw JSON in audit table
                        # Handle case where raw_json might not exist (e.g., in error results)
                        raw_json = result.get('raw_json')
                        if not raw_json:
                            # Create raw_json from the result data
                            if result.get('updated'):
                                raw_json = json.dumps(result['updated'], ensure_ascii=False)
                            else:
                                raw_json = json.dumps({'error': result.get('error', 'Unknown error')}, ensure_ascii=False)
                        
                        await asyncio.to_thread(
                            store_raw_enrichment_response,
                            db_path,
                            result['sha1'],
                            enrichment_config['name'],
                            raw_json,
                            model,
                            result.get('enrichment_id'),  # Pass enrichment_id
                            prompt_id,  # Pass prompt_id for tracking
                            result.get('full_prompt')  # Pass full_prompt
                        )
                        
                        # DUAL STORAGE: 2. Store parsed columns in target table
                        # Only store to output table if we have actual data
                        if result.get('updated') and enrichment_strategy.storage_mode == "separate_table":
                            # Use separate output table
                            key_value = result.get(enrichment_strategy.key_column, result.get('sha1', 'NO_KEY'))
                            await asyncio.to_thread(
                                update_output_table,
                                db_path,
                                enrichment_strategy.output_table,
                                enrichment_strategy.key_column,
                                key_value,
                                result['updated'],
                                result.get('enrichment_id'),  # Pass enrichment_id
                                model  # Pass model for multi-model support
                            )
                        else:
                            # Direct column mode - update source table
                            # For single column, extract the value
                            if len(enrichment_strategy.output_columns) == 1:
                                column_name = enrichment_strategy.output_columns[0]
                                column_value = result['updated'].get(column_name)
                                # Convert enum to string if needed
                                if hasattr(column_value, 'value'):
                                    column_value = column_value.value
                                await asyncio.to_thread(
                                    update_database,
                                    db_path,
                                    table,
                                    column_name,
                                    [{
                                        'rowid': result['rowid'],
                                        'sha1': result['sha1'],  # Preserve sha1 for primary key lookup
                                        'original': '',
                                        'updated': column_value
                                    }]
                                )
                return result
                
            except Exception as e:
                logging.error(f"Error in schema-driven processing: {e}")
                # Fall back to legacy processing
                pass
        
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
                truncate=truncate
            )
            
            # ALWAYS store to enrichment_responses for audit trail, even for failures
            if result:
                async with db_semaphore:
                    # Store in enrichment_responses regardless of success/failure
                    raw_json = json.dumps(result.get('updated', {}), ensure_ascii=False) if result.get('updated') else json.dumps({'error': result.get('error', 'Unknown error')})
                    await asyncio.to_thread(
                        store_raw_enrichment_response,
                        db_path,
                        result['sha1'],
                        enrichment_config['name'],
                        raw_json,
                        model,
                        result.get('enrichment_id'),
                        prompt_id,
                        result.get('full_prompt')
                    )
                    
                    # Special handling for translation results which have multiple columns
                    if result.get('updated'):
                        # Update each column separately
                        for col in ['zh_json', 'en_json', 'english_translation']:
                            await asyncio.to_thread(
                                update_database,
                                db_path,
                                table,
                                col,
                                [{
                                    'rowid': result['rowid'],
                                    'original': result['original'].get(col, ''),
                                    'updated': result['updated'].get(col, '')
                                }]
                            )
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
                verbose=verbose
            )
            
            # ALWAYS store to enrichment_responses for audit trail, even for failures
            if result:
                async with db_semaphore:
                    # Store in enrichment_responses regardless of success/failure
                    raw_json = json.dumps({'result': result.get('updated')}, ensure_ascii=False) if result.get('updated') else json.dumps({'error': result.get('error', 'Unknown error')})
                    await asyncio.to_thread(
                        store_raw_enrichment_response,
                        db_path,
                        result['sha1'],
                        enrichment_config['name'],
                        raw_json,
                        model,
                        result.get('enrichment_id'),
                        prompt_id,
                        result.get('full_prompt')
                    )
                    
                    # Only update output table if we have actual data
                    if result.get('updated'):
                        if output_table:
                            # Use separate output table
                            key_value = result.get(key_column, result.get('sha1', 'NO_KEY'))
                            output_data = {output_cols[0]: result['updated']}
                            await asyncio.to_thread(
                                update_output_table,
                                db_path,
                                output_table,
                                key_column,
                                key_value,
                                output_data,
                                result.get('enrichment_id'),  # Pass enrichment_id from result
                                model  # Pass model for multi-model support
                            )
                        else:
                            # Traditional update to source table
                            await asyncio.to_thread(
                                update_database,
                                db_path,
                                table,
                                output_cols[0],
                                [result]
                            )
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
                verbose=verbose
            )
            
            # ALWAYS store to enrichment_responses for audit trail, even for failures
            if result:
                async with db_semaphore:
                    # Store in enrichment_responses regardless of success/failure
                    raw_json = json.dumps({'result': result.get('updated')}, ensure_ascii=False) if result.get('updated') else json.dumps({'error': result.get('error', 'Unknown error')})
                    await asyncio.to_thread(
                        store_raw_enrichment_response,
                        db_path,
                        result['sha1'],
                        enrichment_config['name'],
                        raw_json,
                        model,
                        result.get('enrichment_id'),
                        prompt_id,
                        result.get('full_prompt')
                    )
                    
                    # Only update output table if we have actual data
                    if result.get('updated'):
                        if output_table:
                            # Use separate output table
                            key_value = result.get(key_column, result.get('sha1', 'NO_KEY'))
                            output_data = {output_cols[0]: result['updated']}
                            await asyncio.to_thread(
                                update_output_table,
                                db_path,
                                output_table,
                                key_column,
                                key_value,
                                output_data,
                                result.get('enrichment_id'),  # Pass enrichment_id from result
                                model  # Pass model for multi-model support
                            )
                        else:
                            # Traditional update to source table
                            await asyncio.to_thread(
                                update_database,
                                db_path,
                                table,
                                output_cols[0],
                                [result]
                            )
        return result

    tasks = [process_and_save(row) for row in rows_to_process]
    processed_results = await asyncio.gather(*tasks)
    
    # Run WAL checkpoint periodically to prevent WAL file from growing too large
    # Do this every 1000 processed rows
    total_processed = len([r for r in processed_results if r and r.get('updated')])
    if total_processed > 0 and total_processed % 1000 == 0:
        logging.info(f"Running WAL checkpoint after {total_processed} rows...")
        await asyncio.to_thread(checkpoint_wal, db_path)
    
    return processed_results + skipped_rows

async def process_row_structured(row: Dict, input_cols: List[str], parsed_input_cols: List[Tuple[str, Optional[int]]], 
                               prompt: str, model: str, semaphore: asyncio.Semaphore, pbar: tqdm,
                               pydantic_model: Type[BaseModel], system_prompt: str = None, 
                               truncate: bool = False, verbose: bool = False, provider=None):
    """Process a single row using structured outputs (OpenAI only)."""
    async with semaphore:
        sha1 = row.get('sha1', 'NO_SHA1')
        # Generate a unique enrichment_id for this specific LLM call
        row_enrichment_id = str(uuid.uuid4())
        try:
            # Use new column parsing with character limits
            limited_data = apply_column_limits(row, parsed_input_cols)
            
            # Replace template variables in prompt with actual column values
            templated_prompt = prompt
            template_replacements = {}
            for col, _ in parsed_input_cols:
                if col not in ['rowid', 'sha1']:
                    # Handle both plain column names and table.column syntax
                    col_value = limited_data.get(col, '')
                    # Replace {column_name} with actual value
                    if f'{{{col}}}' in templated_prompt:
                        templated_prompt = templated_prompt.replace(f'{{{col}}}', str(col_value))
                        template_replacements[f'{{{col}}}'] = str(col_value)
                    # Also handle case where column has table prefix (e.g., {documents.title})
                    if '.' in col:
                        _, column_only = col.split('.', 1)
                        if f'{{{column_only}}}' in templated_prompt:
                            templated_prompt = templated_prompt.replace(f'{{{column_only}}}', str(col_value))
                            template_replacements[f'{{{column_only}}}'] = str(col_value)
            
            if template_replacements and verbose:
                logging.info(f"Template substitutions: {template_replacements}")
            
            input_text = "\n".join([
                f"{col}: {limited_data.get(col, '')}" 
                for col, _ in parsed_input_cols 
                if col not in ['rowid', 'sha1']
            ])
            
            # Handle truncation if enabled
            final_input_text = input_text
            was_truncated = False
            rowid = row.get('rowid', 'unknown')
            
            if truncate:
                logging.debug(f"Truncate enabled for rowid {rowid}, checking if needed...")
                prompt_tokens = estimate_tokens(templated_prompt)
                input_tokens = estimate_tokens(input_text)
                total_tokens = prompt_tokens + input_tokens
                logging.debug(f"Estimated tokens - prompt: {prompt_tokens}, input: {input_tokens}, total: {total_tokens}")
                
                final_input_text, was_truncated = truncate_input_for_model(templated_prompt, input_text, model)
                if was_truncated:
                    logging.info(f"✂️  Truncated input for rowid {rowid} (model: {model})")
                else:
                    logging.debug(f"No truncation needed for rowid {rowid}")
            else:
                logging.debug(f"Truncate disabled for rowid {rowid}")
            
            messages = [{"role": "user", "content": templated_prompt + "\n\n" + final_input_text}]
            full_prompt_content = templated_prompt + "\n\n" + final_input_text
            
            # Make structured API call with retry logic for language validation
            max_retries = 2  # Total of 3 attempts (original + 2 retries)
            
            for attempt in range(max_retries + 1):
                try:
                    result = await call_llm_structured(model, messages, pydantic_model, system_prompt, verbose, provider)
                    
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
                    logging.debug(f"[{sha1[:8]}] Structured result: {result_dict}")
                    if attempt > 0:
                        logging.info(f"✅ Language validation passed on attempt {attempt + 1} for rowid {row.get('rowid', 'unknown')}")
                    pbar.update(1)
                    
                    return {
                        'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                        'rowid': row.get('rowid', 'NO_ROWID'), 
                        'sha1': sha1,
                        'original': {},  # Not applicable for structured outputs
                        'updated': result_dict,
                        'raw_json': result.model_dump_json(),
                        'full_prompt': full_prompt_content
                    }
                    
                except LanguageValidationError as e:
                    if attempt < max_retries:
                        logging.warning(f"🔄 Language validation failed on attempt {attempt + 1} for rowid {row.get('rowid', 'unknown')}: {str(e)[:100]}... Retrying...")
                        # Don't update progress bar yet, we're retrying
                        continue
                    else:
                        # Final attempt failed, log error and continue
                        logging.error(f"❌ Language validation failed after {max_retries + 1} attempts for rowid {row.get('rowid', 'unknown')}: {str(e)}")
                        pbar.update(1)
                        return {
                            'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                            'rowid': row.get('rowid', 'NO_ROWID'), 
                            'sha1': sha1,
                            'original': {}, 
                            'updated': None, 
                            'error': f"Language validation failed after {max_retries + 1} attempts: {str(e)}",
                            'raw_json': json.dumps({'error': f"Language validation failed after {max_retries + 1} attempts: {str(e)}"}, ensure_ascii=False),
                            'full_prompt': full_prompt_content
                        }
                        
                except Exception as e:
                    # For non-language validation errors, don't retry
                    raise e
                
        except Exception as e:
            rowid = row.get('rowid', 'unknown')
            logging.error(f"Error processing rowid {rowid} (sha1: {sha1[:8]}): {str(e)}")
            pbar.update(1)
            return {
                'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                'rowid': rowid, 
                'sha1': sha1,
                'original': {}, 
                'updated': None, 
                'error': str(e),
                'raw_json': json.dumps({'error': str(e)}, ensure_ascii=False),
                'full_prompt': full_prompt_content if 'full_prompt_content' in locals() else None
            }

async def process_row(row: Dict, input_cols: List[str], parsed_input_cols: List[Tuple[str, Optional[int]]], prompt: str, 
                     model: str, semaphore: asyncio.Semaphore, pbar: tqdm, 
                     output_col: str, output_schema = None, 
                     system_prompt: str = None, config: Dict = None, truncate: bool = False, verbose: bool = False):
    async with semaphore:
        sha1 = row.get('sha1', 'NO_SHA1')
        # Generate a unique enrichment_id for this specific LLM call
        row_enrichment_id = str(uuid.uuid4())
        try:
            # Use new column parsing with character limits
            limited_data = apply_column_limits(row, parsed_input_cols)
            
            # Replace template variables in prompt with actual column values
            templated_prompt = prompt
            template_replacements = {}
            for col, _ in parsed_input_cols:
                if col not in ['rowid', 'sha1']:
                    # Handle both plain column names and table.column syntax
                    col_value = limited_data.get(col, '')
                    # Replace {column_name} with actual value
                    if f'{{{col}}}' in templated_prompt:
                        templated_prompt = templated_prompt.replace(f'{{{col}}}', str(col_value))
                        template_replacements[f'{{{col}}}'] = str(col_value)
                    # Also handle case where column has table prefix (e.g., {documents.title})
                    if '.' in col:
                        _, column_only = col.split('.', 1)
                        if f'{{{column_only}}}' in templated_prompt:
                            templated_prompt = templated_prompt.replace(f'{{{column_only}}}', str(col_value))
                            template_replacements[f'{{{column_only}}}'] = str(col_value)
            
            if template_replacements and verbose:
                logging.info(f"Template substitutions: {template_replacements}")
            
            input_text = "\n".join([
                f"{col}: {limited_data.get(col, '')}" 
                for col, _ in parsed_input_cols 
                if col not in ['rowid', 'sha1']
            ])
            
            # Add schema instructions to prompt if schema is defined
            full_prompt = templated_prompt
            if output_schema and config:
                schema_instructions = get_schema_prompt_instructions(config, output_schema)
                if schema_instructions:
                    full_prompt = templated_prompt + "\n\n" + schema_instructions
            
            # Handle truncation if enabled
            final_input_text = input_text
            was_truncated = False
            rowid = row.get('rowid', 'unknown')
            
            if truncate:
                logging.debug(f"Truncate enabled for rowid {rowid}, checking if needed...")
                prompt_tokens = estimate_tokens(full_prompt)
                input_tokens = estimate_tokens(input_text)
                total_tokens = prompt_tokens + input_tokens
                logging.debug(f"Estimated tokens - prompt: {prompt_tokens}, input: {input_tokens}, total: {total_tokens}")
                
                final_input_text, was_truncated = truncate_input_for_model(full_prompt, input_text, model)
                if was_truncated:
                    logging.info(f"✂️  Truncated input for rowid {rowid} (model: {model})")
                else:
                    logging.debug(f"No truncation needed for rowid {rowid}")
            else:
                logging.debug(f"Truncate disabled for rowid {rowid}")
            
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            full_prompt_content = full_prompt + "\n\n" + final_input_text
            messages.append({"role": "user", "content": full_prompt_content})
            
            # Make API call
            result = await call_llm(model, messages, system_prompt, verbose)
            
            # Validate result against schema if provided
            validated_result = result
            if output_schema and config:
                try:
                    validated_result = validate_with_schema(config, output_schema, result)
                    # Convert back to string for storage
                    if not isinstance(validated_result, str):
                        validated_result = str(validated_result)
                except SchemaValidationError as e:
                    logging.warning(f"[{sha1[:8]}] Schema validation failed: {e}")
                    # Return error instead of invalid result
                    pbar.update(1)
                    return {
                        'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                        'rowid': row.get('rowid', 'NO_ROWID'), 
                        'sha1': sha1,
                        'original': row.get(output_col, ''), 
                        'updated': None,
                        'error': f"Schema validation failed: {e}",
                        'full_prompt': full_prompt_content
                    }
            
            # Console progress - only show model response if verbose
            logging.debug(f"[{sha1[:8]}] {validated_result[:30]}..." if len(validated_result) > 30 else f"[{sha1[:8]}] {validated_result}")
            pbar.update(1)
            
            return {
                'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                'rowid': row.get('rowid', 'NO_ROWID'), 
                'sha1': sha1,
                'original': row.get(output_col, ''), 
                'updated': validated_result,
                'full_prompt': full_prompt_content
            }
                
        except Exception as e:
            rowid = row.get('rowid', 'unknown')
            logging.error(f"Error processing rowid {rowid} (sha1: {sha1[:8]}): {str(e)}")
            pbar.update(1)
            return {
                'enrichment_id': row_enrichment_id,  # Use the row-specific ID
                'rowid': rowid, 
                'sha1': sha1,
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
    overwrite: bool = False,
    config: Dict = None,
    truncate: bool = False,
    verbose: bool = False,
    output_table: str = None,
    key_column: str = DEFAULT_KEY_COLUMN,
    enrichment_strategy: EnrichmentStrategy = None,
    is_multi_model: bool = False
):
    """Process a single enrichment task"""
    logging.info(f"🎯 Starting enrichment '{enrichment_config['name']}'")
    logging.info(f"📊 Model: {model}, Rows: {len(results)}, Overwrite: {overwrite}")
    
    # Ensure enrichment_responses table exists ONCE before processing
    ensure_enrichment_responses_table(db_path)
    
    prompt = enrichment_config.get('prompt', '')
    
    # Handle append_file feature
    if 'append_file' in enrichment_config:
        append_file_path = enrichment_config['append_file']
        # Get the directory of the config file to resolve relative paths
        if config and '__config_path__' in config:
            config_dir = os.path.dirname(config['__config_path__'])
            # If append_file is not absolute, make it relative to config dir
            if not os.path.isabs(append_file_path):
                append_file_path = os.path.join(config_dir, append_file_path)
        else:
            # If no config path available, try to resolve from current working directory
            logging.warning("No config path available, using append_file path as-is")
        
        try:
            with open(append_file_path, 'r', encoding='utf-8') as f:
                appended_content = f.read()
            prompt = prompt + "\n\n" + appended_content
            logging.info(f"📎 Appended content from file: {append_file_path}")
        except FileNotFoundError:
            logging.error(f"❌ append_file not found: {append_file_path}")
            raise ValueError(f"append_file not found: {append_file_path}")
        except Exception as e:
            logging.error(f"❌ Error reading append_file: {e}")
            raise
    
    system_prompt = enrichment_config.get('system_prompt')
    output_schema = enrichment_config.get('schema')
    
    # Handle single or multiple output columns
    output_cols = enrichment_config.get('output_columns', [enrichment_config.get('output_column')])
    if isinstance(output_cols, str):
        output_cols = [output_cols]

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
        suppress_progress_messages=is_multi_model
    )
    
    return processed_results

async def process_translation(row: Dict, input_cols: List[Tuple[str, Optional[slice]]], prompt: str,
                            model: str, semaphore: asyncio.Semaphore, pbar: tqdm,
                            output_cols: List[str], output_schema: Optional[Type[BaseModel]] = None,
                            system_prompt: str = None, chunk_size: int = 3, truncate: bool = False):
    async with semaphore:
        sha1 = row.get('sha1', 'NO_SHA1')
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
                    'sha1': sha1,
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
                    
                    try:
                        response = await openai_client.beta.chat.completions.parse(
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
                            response = await openai_client.beta.chat.completions.parse(
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
                'sha1': sha1,
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
                'sha1': sha1,
                'original': {col: row.get(col, '') for col in output_cols},
                'updated': None,
                'error': str(e)
            }
    

