"""Service for handling enrichment operations."""

import logging
import re
from typing import Dict, List, Optional, Any, Tuple
import click

from ..types import EnrichmentConfig, ConfigDict, RowList
from ..constants import DEFAULT_TABLE_NAME, DEFAULT_KEY_COLUMN
from ..enrichment_config import prepare_enrichment_for_processing, EnrichmentStrategy
from ..db_operations import (
    execute_query, execute_query_optimized, ensure_output_column, 
    ensure_output_table, get_or_create_prompt_id
)
from ..llm_operations import process_enrichment
from ..utils.cost_estimation import estimate_enrichment_cost, should_confirm_cost
from ..utils.progress import create_progress_bar
from ..utils.query_utils import ensure_rowid_in_query, apply_null_filters, add_order_and_limit


class EnrichmentService:
    """Service for managing enrichment operations."""
    
    def __init__(self, db_path: str, config: ConfigDict):
        """Initialize the enrichment service.
        
        Args:
            db_path: Path to the database
            config: Configuration dictionary
        """
        self.db_path = db_path
        self.config = config
        self.logger = logging.getLogger(__name__)
        
    async def process_enrichment_task(
        self,
        enrichment_config: EnrichmentConfig,
        model: Optional[str] = None,
        overwrite: bool = False,
        limit: Optional[int] = None,
        rowid: Optional[int] = None,
        sha1: Optional[str] = None,
        table: Optional[str] = None,
        verbose: bool = False,
        truncate: bool = False,
        skip_cost_check: bool = False,
        cost_threshold: float = 1.0
    ) -> Dict[str, Any]:
        """Process a single enrichment task.
        
        Args:
            enrichment_config: Enrichment configuration
            model: Optional model override
            overwrite: Whether to overwrite existing values
            limit: Limit number of rows to process
            rowid: Process only specific rowid
            sha1: Process only specific sha1
            table: Optional table override
            verbose: Enable verbose output
            truncate: Enable truncation mode
            skip_cost_check: Skip cost estimation check
            cost_threshold: Cost threshold for confirmation
            
        Returns:
            Dictionary with processing results
        """
        # Prepare enrichment strategy
        default_table = self.config.get('default_table', DEFAULT_TABLE_NAME)
        strategy, config_errors = prepare_enrichment_for_processing(
            enrichment_config, default_table
        )
        
        if config_errors:
            raise ValueError(f"Configuration errors: {', '.join(config_errors)}")
        
        # Override table if specified
        if table:
            strategy.input_table = table
            enrichment_config['table'] = table
        
        # Prepare database tables
        self._prepare_database_tables(strategy)
        
        # Build and execute query
        query = self._build_query(enrichment_config, strategy, overwrite, limit, rowid, sha1)
        results = self._execute_query(query, enrichment_config, strategy)
        
        if not results:
            return {"processed": 0, "message": "No rows to process"}
        
        # Cost estimation
        if not skip_cost_check:
            cost_ok = await self._check_cost(
                results, enrichment_config, model or self.config.get('default_model'),
                cost_threshold, verbose
            )
            if not cost_ok:
                return {"processed": 0, "message": "Cancelled due to cost"}
        
        # Process enrichment
        processed_results = await self._run_enrichment(
            results, enrichment_config, model, strategy, 
            overwrite, verbose, truncate
        )
        
        return {
            "processed": len(processed_results),
            "results": processed_results
        }
    
    def _prepare_database_tables(self, strategy: EnrichmentStrategy) -> None:
        """Ensure required database tables and columns exist."""
        if strategy.storage_mode == "separate_table":
            ensure_output_table(
                self.db_path,
                strategy.output_table,
                strategy.key_column,
                strategy.output_columns,
                is_derived_table=True
            )
        else:
            # Direct column mode - ensure columns exist
            for column in strategy.output_columns:
                if column:
                    ensure_output_column(
                        self.db_path, 
                        strategy.input_table, 
                        column
                    )
    
    def _build_query(
        self,
        enrichment_config: EnrichmentConfig,
        strategy: EnrichmentStrategy,
        overwrite: bool,
        limit: Optional[int],
        rowid: Optional[int],
        sha1: Optional[str]
    ) -> str:
        """Build the SQL query for fetching rows to process."""
        # Handle specific row filters
        if rowid is not None:
            return f"SELECT rowid, * FROM {strategy.input_table} WHERE rowid = {rowid}"
        elif sha1 is not None:
            return f"SELECT rowid, * FROM {strategy.input_table} WHERE sha1 = '{sha1}'"
        
        # Get base query
        query_name = enrichment_config['input']['query']
        if query_name in self.config.get('sql_queries', {}):
            base_query = self.config['sql_queries'][query_name]
        else:
            base_query = query_name
        
        # Apply overwrite/append filters
        if strategy.storage_mode == "direct_column":
            base_query = apply_null_filters(
                base_query, 
                strategy.output_columns[0], 
                overwrite
            )
        
        # Add ORDER BY and LIMIT
        query = add_order_and_limit(base_query, limit)
        
        return ensure_rowid_in_query(query)
    
    
    def _execute_query(
        self, 
        query: str, 
        enrichment_config: EnrichmentConfig,
        strategy: EnrichmentStrategy
    ) -> RowList:
        """Execute the query and return results."""
        input_columns = enrichment_config['input'].get('input_columns', [])
        
        # Use optimized query if we have specific input columns
        if input_columns and len(input_columns) < 10:  # Arbitrary threshold
            return execute_query_optimized(
                self.db_path, query, input_columns
            )
        else:
            return execute_query(self.db_path, query)
    
    async def _check_cost(
        self,
        results: RowList,
        enrichment_config: EnrichmentConfig,
        model: str,
        cost_threshold: float,
        verbose: bool
    ) -> bool:
        """Check cost and get user confirmation if needed."""
        cost_estimate = estimate_enrichment_cost(
            enrichment_config, 
            model, 
            self.config, 
            results
        )
        
        if verbose and cost_estimate:
            print(f"\n💰 Estimated cost: ${cost_estimate['total_cost']:.4f}")
        
        if should_confirm_cost(cost_estimate, cost_threshold):
            return click.confirm(
                f"\n💸 Estimated cost exceeds ${cost_threshold:.2f}. Continue?"
            )
        
        return True
    
    async def _run_enrichment(
        self,
        results: RowList,
        enrichment_config: EnrichmentConfig,
        model: Optional[str],
        strategy: EnrichmentStrategy,
        overwrite: bool,
        verbose: bool,
        truncate: bool
    ) -> List[Dict[str, Any]]:
        """Run the actual enrichment process."""
        # Use model from config or default
        model = model or enrichment_config.get('model') or self.config.get('default_model')
        
        # Create progress bar
        desc = f"🚀 {enrichment_config['name']} ({model})"
        progress_bar = create_progress_bar(
            total=len(results),
            desc=desc,
            verbose=verbose
        )
        
        # Get prompt ID for tracking
        prompt_id = get_or_create_prompt_id(
            self.db_path,
            enrichment_config['name'],
            enrichment_config.get('prompt', ''),
            enrichment_config.get('system_prompt'),
            model
        )
        
        with progress_bar as pbar:
            processed_results = await process_enrichment(
                results=results,
                enrichment_config=enrichment_config,
                model=model,
                pbar=pbar,
                db_path=self.db_path,
                table=strategy.input_table,
                overwrite=overwrite,
                config=self.config,
                truncate=truncate,
                verbose=verbose,
                output_table=strategy.output_table,
                key_column=strategy.key_column,
                enrichment_strategy=strategy
            )
        
        return processed_results