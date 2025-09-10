"""Enrichment processor for handling LLM-based enrichments."""

import asyncio
import uuid
import json
import logging
from typing import Dict, Any, Optional, List, Tuple
from tqdm import tqdm

from .base import BaseProcessor
from ...types import RowDict, EnrichmentResult
from ...constants import DEFAULT_API_SEMAPHORE_LIMIT, DEFAULT_DB_SEMAPHORE_LIMIT
from ...db_operations import (
    store_raw_enrichment_response, update_output_table,
    update_database, checkpoint_wal
)
from ...schema_managers import validate_with_schema
from ...core_utils import parse_input_columns_with_limits, apply_column_limits
from ..client import LLMClient
from ..token_utils import truncate_input_for_model


class EnrichmentProcessor(BaseProcessor):
    """Processor for enrichment operations."""
    
    def __init__(self, db_path: str, config: Optional[Dict[str, Any]] = None):
        """Initialize enrichment processor."""
        super().__init__(db_path, config)
        self.api_semaphore = asyncio.Semaphore(DEFAULT_API_SEMAPHORE_LIMIT)
        self.db_semaphore = asyncio.Semaphore(DEFAULT_DB_SEMAPHORE_LIMIT)
        
    async def process_batch(
        self,
        rows: List[RowDict],
        enrichment_config: Dict[str, Any],
        model: str,
        pbar: Optional[tqdm] = None,
        overwrite: bool = False,
        verbose: bool = False,
        truncate: bool = False,
        enrichment_strategy: Optional[Any] = None,
        table: Optional[str] = None,
        output_table: Optional[str] = None,
        key_column: str = "sha1",
        prompt_id: Optional[str] = None,
        **kwargs
    ) -> List[EnrichmentResult]:
        """Process a batch of rows for enrichment.
        
        Args:
            rows: List of rows to process
            enrichment_config: Enrichment configuration
            model: Model to use
            pbar: Progress bar
            overwrite: Whether to overwrite existing values
            verbose: Enable verbose output
            truncate: Enable truncation
            enrichment_strategy: Enrichment strategy object
            table: Source table name
            output_table: Output table name
            key_column: Key column name
            prompt_id: Prompt ID for tracking
            **kwargs: Additional arguments
            
        Returns:
            List of enrichment results
        """
        # Initialize LLM client
        llm_client = LLMClient(model, self.config)
        
        # Parse input columns
        input_config = enrichment_config.get('input', {})
        input_cols_raw = input_config.get('input_columns', [])
        parsed_input_cols = parse_input_columns_with_limits(input_cols_raw)
        input_cols = [col for col, _ in parsed_input_cols]
        
        # Get prompt and system prompt
        prompt_template = enrichment_config.get('prompt', '')
        system_prompt = enrichment_config.get('system_prompt')
        
        # Process rows concurrently
        tasks = []
        for row in rows:
            task = self._process_single_row(
                row=row,
                enrichment_config=enrichment_config,
                model=model,
                llm_client=llm_client,
                input_cols=input_cols,
                parsed_input_cols=parsed_input_cols,
                prompt_template=prompt_template,
                system_prompt=system_prompt,
                pbar=pbar,
                verbose=verbose,
                truncate=truncate,
                enrichment_strategy=enrichment_strategy,
                prompt_id=prompt_id,
                table=table,
                output_table=output_table,
                key_column=key_column
            )
            tasks.append(task)
        
        results = await asyncio.gather(*tasks)
        
        # Checkpoint WAL after batch
        if not kwargs.get('suppress_wal_checkpoint', False):
            await asyncio.to_thread(checkpoint_wal, self.db_path)
        
        return results
    
    async def _process_single_row(
        self,
        row: RowDict,
        enrichment_config: Dict[str, Any],
        model: str,
        llm_client: LLMClient,
        input_cols: List[str],
        parsed_input_cols: List[Tuple[str, Optional[int]]],
        prompt_template: str,
        system_prompt: Optional[str],
        pbar: Optional[tqdm],
        verbose: bool,
        truncate: bool,
        enrichment_strategy: Optional[Any],
        prompt_id: Optional[str],
        table: Optional[str],
        output_table: Optional[str],
        key_column: str
    ) -> EnrichmentResult:
        """Process a single row."""
        async with self.api_semaphore:
            sha1 = row.get('sha1', 'NO_SHA1')
            enrichment_id = str(uuid.uuid4())
            
            try:
                # Apply column limits
                limited_row = apply_column_limits(row, parsed_input_cols)
                
                # Format prompt
                full_prompt = prompt_template.format(**{
                    col: limited_row.get(col, '') for col in input_cols
                })
                
                # Truncate if needed
                if truncate:
                    input_text = ' '.join(str(limited_row.get(col, '')) for col in input_cols)
                    full_prompt, was_truncated = truncate_input_for_model(
                        full_prompt, input_text, model
                    )
                
                # Create messages
                messages = [{"role": "user", "content": full_prompt}]
                
                # Call LLM
                if enrichment_strategy and enrichment_strategy.pydantic_model:
                    # Structured output
                    result = await llm_client.call_structured(
                        messages=messages,
                        pydantic_model=enrichment_strategy.pydantic_model,
                        system_prompt=system_prompt,
                        verbose=verbose
                    )
                    validated_result = result.model_dump()
                else:
                    # Regular text output
                    response = await llm_client.call(
                        messages=messages,
                        system_prompt=system_prompt,
                        verbose=verbose
                    )
                    
                    # Validate with schema if present
                    schema = enrichment_config.get('schema')
                    if schema:
                        validated_result = validate_with_schema(response, schema)
                    else:
                        validated_result = response
                
                # Store results
                await self._store_results(
                    enrichment_id=enrichment_id,
                    sha1=sha1,
                    enrichment_name=enrichment_config['name'],
                    result=validated_result,
                    model=model,
                    prompt_id=prompt_id,
                    full_prompt=full_prompt,
                    enrichment_strategy=enrichment_strategy,
                    key_column=key_column,
                    output_table=output_table,
                    table=table,
                    row=row
                )
                
                if pbar:
                    pbar.update(1)
                
                return {
                    'enrichment_id': enrichment_id,
                    'rowid': row.get('rowid'),
                    'sha1': sha1,
                    'original': None,
                    'updated': validated_result,
                    'full_prompt': full_prompt
                }
                
            except Exception as e:
                self.logger.error(f"Error processing row {sha1[:8]}: {e}")
                if pbar:
                    pbar.update(1)
                
                return {
                    'enrichment_id': enrichment_id,
                    'rowid': row.get('rowid'),
                    'sha1': sha1,
                    'original': None,
                    'updated': None,
                    'error': str(e),
                    'full_prompt': full_prompt if 'full_prompt' in locals() else None
                }
    
    async def _store_results(
        self,
        enrichment_id: str,
        sha1: str,
        enrichment_name: str,
        result: Any,
        model: str,
        prompt_id: Optional[str],
        full_prompt: str,
        enrichment_strategy: Optional[Any],
        key_column: str,
        output_table: Optional[str],
        table: Optional[str],
        row: RowDict
    ) -> None:
        """Store enrichment results in database."""
        async with self.db_semaphore:
            # Store raw response
            raw_json = json.dumps(result, ensure_ascii=False) if result else '{}'
            await asyncio.to_thread(
                store_raw_enrichment_response,
                self.db_path,
                sha1,
                enrichment_name,
                raw_json,
                model,
                enrichment_id,
                prompt_id,
                full_prompt
            )
            
            # Store parsed data
            if result and enrichment_strategy:
                if enrichment_strategy.storage_mode == "separate_table":
                    # Store in separate table
                    key_value = row.get(key_column, sha1)
                    await asyncio.to_thread(
                        update_output_table,
                        self.db_path,
                        output_table or enrichment_strategy.output_table,
                        key_column,
                        key_value,
                        result if isinstance(result, dict) else {'value': result},
                        enrichment_id,
                        model
                    )
                else:
                    # Update source table
                    if enrichment_strategy.output_columns:
                        column_name = enrichment_strategy.output_columns[0]
                        column_value = result.get(column_name) if isinstance(result, dict) else result
                        
                        # Convert enum to string if needed
                        if hasattr(column_value, 'value'):
                            column_value = column_value.value
                            
                        await asyncio.to_thread(
                            update_database,
                            self.db_path,
                            table or enrichment_strategy.input_table,
                            column_name,
                            [{
                                'rowid': row.get('rowid'),
                                'sha1': sha1,
                                'original': '',
                                'updated': column_value
                            }]
                        )
    
    async def process_row(
        self,
        row: RowDict,
        enrichment_config: Dict[str, Any],
        model: str,
        **kwargs
    ) -> EnrichmentResult:
        """Process a single row (implements abstract method)."""
        results = await self.process_batch(
            [row],
            enrichment_config,
            model,
            **kwargs
        )
        return results[0] if results else None