"""Type definitions and aliases for Doctrail."""

from typing import Dict, List, Optional, Any, Union, TypedDict, Protocol
from pathlib import Path

# Basic type aliases
RowDict = Dict[str, Any]
RowList = List[RowDict]
ConfigDict = Dict[str, Any]
PromptDict = Dict[str, str]
SchemaDict = Dict[str, Any]

# Path types
PathLike = Union[str, Path]

# Enrichment-specific types
class EnrichmentResult(TypedDict, total=False):
    """Result from an enrichment operation."""
    enrichment_id: str
    rowid: Optional[int]
    sha1: str
    original: Any
    updated: Any
    error: Optional[str]
    full_prompt: Optional[str]
    raw_json: Optional[str]


class EnrichmentConfig(TypedDict, total=False):
    """Configuration for an enrichment task."""
    name: str
    description: Optional[str]
    input: Dict[str, Any]
    output_column: Optional[str]
    output_columns: Optional[List[str]]
    output_table: Optional[str]
    key_column: Optional[str]
    schema: Optional[Union[List[str], Dict[str, Any]]]
    prompt: str
    system_prompt: Optional[str]
    model: Optional[str]
    append_file: Optional[str]
    table: Optional[str]


class ModelConfig(TypedDict):
    """Configuration for an LLM model."""
    name: str
    max_tokens: int
    temperature: float


# Protocol for plugin interfaces
class IngesterPlugin(Protocol):
    """Protocol for ingester plugins."""
    
    @property
    def name(self) -> str:
        """Plugin name."""
        ...
    
    @property
    def description(self) -> str:
        """Plugin description."""
        ...
    
    @property
    def target_table(self) -> str:
        """Target database table."""
        ...
    
    async def ingest(self, db_path: str, config: ConfigDict, **kwargs) -> Dict[str, int]:
        """Perform ingestion."""
        ...


class ConverterPlugin(Protocol):
    """Protocol for converter plugins."""
    
    def convert(self, value: Any) -> Any:
        """Convert a value."""
        ...


# Database operation types
class DatabaseUpdate(TypedDict):
    """Single database update operation."""
    rowid: int
    sha1: str
    original: Any
    updated: Any


# Export types
class ExportConfig(TypedDict, total=False):
    """Configuration for an export operation."""
    description: str
    query: str
    template: str
    formats: List[str]
    required_fields: Optional[List[str]]
    output_naming: Optional[str]