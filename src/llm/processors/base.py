"""Base processor for LLM operations."""

import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List

from ...types import RowDict, EnrichmentResult


class BaseProcessor(ABC):
    """Base class for LLM processors."""
    
    def __init__(self, db_path: str, config: Optional[Dict[str, Any]] = None):
        """Initialize processor.
        
        Args:
            db_path: Path to database
            config: Optional configuration
        """
        self.db_path = db_path
        self.config = config or {}
        self.logger = logging.getLogger(self.__class__.__name__)
    
    @abstractmethod
    async def process_row(
        self,
        row: RowDict,
        enrichment_config: Dict[str, Any],
        model: str,
        **kwargs
    ) -> EnrichmentResult:
        """Process a single row.
        
        Args:
            row: Row data
            enrichment_config: Enrichment configuration
            model: Model to use
            **kwargs: Additional arguments
            
        Returns:
            Enrichment result
        """
        pass
    
    @abstractmethod
    async def process_batch(
        self,
        rows: List[RowDict],
        enrichment_config: Dict[str, Any],
        model: str,
        **kwargs
    ) -> List[EnrichmentResult]:
        """Process a batch of rows.
        
        Args:
            rows: List of row data
            enrichment_config: Enrichment configuration
            model: Model to use
            **kwargs: Additional arguments
            
        Returns:
            List of enrichment results
        """
        pass
    
    def format_prompt(
        self,
        template: str,
        row: RowDict,
        input_columns: List[str]
    ) -> str:
        """Format prompt template with row data.
        
        Args:
            template: Prompt template
            row: Row data
            input_columns: Columns to include
            
        Returns:
            Formatted prompt
        """
        # Create context dict with only requested columns
        context = {col: row.get(col, '') for col in input_columns}
        
        # Format template
        try:
            return template.format(**context)
        except KeyError as e:
            self.logger.error(f"Missing column in template: {e}")
            raise
    
    def extract_columns(
        self,
        row: RowDict,
        columns: List[str]
    ) -> Dict[str, Any]:
        """Extract specified columns from row.
        
        Args:
            row: Row data
            columns: Column names to extract
            
        Returns:
            Dictionary with extracted columns
        """
        return {col: row.get(col, None) for col in columns}