"""
Enrichment configuration processing.

This module prepares enrichment configs and preserves older concepts like
`storage_mode`, `output_column`, and `output_table`. The current core CLI path
stores model outputs in normalized tables (`_enrichments`, `_enrichment_audit`,
`_enrichment_runs`) and uses views as the main user-facing projection layer.
"""

from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass
import logging
try:
    from .pydantic_schema import analyze_schema_complexity, get_sql_type_from_pydantic_type, create_pydantic_model_from_schema
except ImportError:
    from pydantic_schema import analyze_schema_complexity, get_sql_type_from_pydantic_type, create_pydantic_model_from_schema

logger = logging.getLogger(__name__)

@dataclass
class EnrichmentStrategy:
    """Represents the storage strategy for an enrichment."""
    
    # Input configuration
    input_table: str
    input_columns: List[str]
    
    # Legacy storage terminology retained for compatibility and ancillary tooling.
    # The current core execution path writes normalized results to `_enrichments`.
    storage_mode: str  # "direct_column" or "separate_table"
    
    # Output configuration
    output_table: Optional[str] = None
    output_columns: List[str] = None
    key_column: str = "sha1"
    
    # Schema information
    schema_dict: Dict[str, Any] = None
    pydantic_model: Any = None
    
    # Additional metadata
    requires_audit_trail: bool = True
    sql_column_types: Dict[str, str] = None

class EnrichmentConfigError(Exception):
    """Raised when enrichment configuration is invalid"""
    pass

def determine_enrichment_strategy(enrichment_config: Dict[str, Any],
                                default_table: str = "documents",
                                sql_queries: Optional[Dict[str, str]] = None,
                                config_key_column: str = "sha1") -> EnrichmentStrategy:
    """
    Analyze enrichment configuration and derive field metadata plus compatibility
    hints about storage shape.

    Args:
        enrichment_config: Enrichment configuration from YAML
        default_table: Default input table if not specified
        sql_queries: Dictionary of SQL queries from config (for table extraction)

    Returns:
        EnrichmentStrategy object containing field metadata, key-column info,
        and legacy storage hints.

    Raises:
        EnrichmentConfigError: If configuration is invalid
    """
    enrichment_name = enrichment_config.get('name', 'unnamed')

    # Extract schema - make it optional for plain text enrichments
    schema_dict = enrichment_config.get('schema')
    if not schema_dict:
        # For plain text enrichments without schema, create a simple string schema
        # using the output_column name if available
        output_column = enrichment_config.get('output_column')
        if output_column:
            # Create a simple string schema for backward compatibility
            schema_dict = {output_column: {"type": "string"}}
            logger.info(f"No schema specified for '{enrichment_name}', using simple string schema for column '{output_column}'")
        else:
            raise EnrichmentConfigError(f"Enrichment '{enrichment_name}' must specify either a schema or output_column")

    # Warn about implicit enum syntax
    if isinstance(schema_dict, list):
        logger.warning(f"Enrichment '{enrichment_name}' uses implicit enum syntax - "
                      f"consider using explicit 'enum: {schema_dict}' for clarity")

    # Get output_column early - needed for schema analysis
    output_column = enrichment_config.get('output_column')

    # Analyze schema complexity
    schema_analysis = analyze_schema_complexity(schema_dict, output_column=output_column)

    # If it's a bare type schema like {type: "string"}, wrap it with output_column
    if schema_analysis.get('is_bare_type') and output_column:
        schema_dict = {output_column: schema_dict}
        logger.info(f"Wrapped bare type schema with output_column '{output_column}' for enrichment '{enrichment_name}'")

    # Determine input table - try multiple sources
    input_table = None

    # 1. First check for explicit input_table
    input_table = enrichment_config.get('input_table')

    # 2. If not found, check for 'table' field
    if not input_table:
        input_table = enrichment_config.get('table')

    # 3. If still not found, try to extract from SQL query (named or inline)
    if not input_table:
        import re
        input_config = enrichment_config.get('input', {})
        query_name = input_config.get('query')
        if query_name:
            # Resolve: named query in sql_queries dict, or inline SQL
            if sql_queries and query_name in sql_queries:
                query = sql_queries[query_name]
            elif '\n' in query_name or query_name.strip().upper().startswith('SELECT'):
                query = query_name  # it's inline SQL
            else:
                query = None
            if query:
                table_match = re.search(r'\bFROM\s+(\w+)', query, re.IGNORECASE)
                if table_match:
                    input_table = table_match.group(1)
                    logger.debug(f"Extracted table '{input_table}' from SQL query for enrichment '{enrichment_name}'")

    # 4. Fall back to default only if nothing else works
    if not input_table:
        input_table = default_table
        logger.warning(f"No table specified for enrichment '{enrichment_name}', using default '{default_table}'")
    
    # Determine input columns
    input_config = enrichment_config.get('input', {})
    input_columns = input_config.get('input_columns', ['raw_content'])
    if isinstance(input_columns, str):
        input_columns = [input_columns]
    
    # Determine legacy storage mode hints based on schema and explicit output_table.
    # Core CLI writes normalized enrichments regardless of this flag.
    output_table = enrichment_config.get('output_table')
    
    # Determine storage mode
    if output_table:
        storage_mode = "separate_table"
        target_table = output_table
    else:
        # Legacy hint: simple schemas without output_table map to the older
        # direct-column terminology, even though the core CLI now stores
        # normalized enrichments.
        storage_mode = "direct_column"
        target_table = input_table
    
    # Extract output columns from schema
    output_columns = schema_analysis['field_names']
    
    # Generate Pydantic model for structured outputs
    model_name = f"{enrichment_name.title().replace('_', '')}Model"
    try:
        # Check if schema specifies that all fields should be optional
        all_optional = enrichment_config.get('all_fields_optional', False)
        pydantic_model = create_pydantic_model_from_schema(schema_dict, model_name, all_fields_optional=all_optional)
    except Exception as e:
        raise EnrichmentConfigError(f"Failed to create Pydantic model for '{enrichment_name}': {e}")
    
    # Generate SQL column types for table creation
    sql_column_types = {}
    for field_name, field_type in pydantic_model.model_fields.items():
        python_type = field_type.annotation
        sql_type = get_sql_type_from_pydantic_type(python_type)
        sql_column_types[field_name] = sql_type
    
    # Extract key column: per-enrichment override > config-level > default
    key_column = enrichment_config.get('key_column') or config_key_column or 'sha1'
    
    return EnrichmentStrategy(
        input_table=input_table,
        input_columns=input_columns,
        storage_mode=storage_mode,
        output_table=target_table if storage_mode == "separate_table" else None,
        output_columns=output_columns,
        key_column=key_column,
        schema_dict=schema_dict,
        pydantic_model=pydantic_model,
        requires_audit_trail=True,  # Always store raw JSON for audit
        sql_column_types=sql_column_types
    )

def validate_enrichment_config(enrichment_config: Dict[str, Any]) -> List[str]:
    """
    Validate enrichment configuration and return list of errors.
    
    Args:
        enrichment_config: Enrichment configuration to validate
        
    Returns:
        List of error messages (empty if valid)
    """
    errors = []
    enrichment_name = enrichment_config.get('name', 'unnamed')
    
    # Required fields
    if not enrichment_config.get('name'):
        errors.append("Enrichment must have a 'name' field")
    
    # Schema is optional if output_column is specified
    if not enrichment_config.get('schema') and not enrichment_config.get('output_column'):
        errors.append(f"Enrichment '{enrichment_name}' must specify either a 'schema' or 'output_column'")
    
    if not enrichment_config.get('prompt'):
        errors.append(f"Enrichment '{enrichment_name}' must specify a 'prompt'")

    dedupe_scope = enrichment_config.get('dedupe_scope') or enrichment_config.get('dedupe-scope')
    if dedupe_scope:
        normalized_dedupe_scope = str(dedupe_scope).strip().lower()
        if normalized_dedupe_scope == "name":
            normalized_dedupe_scope = "enrichment"
        if normalized_dedupe_scope not in {"query", "prompt", "enrichment"}:
            errors.append(
                f"Enrichment '{enrichment_name}' has invalid dedupe_scope '{dedupe_scope}' "
                "(expected query, prompt, enrichment, or name)"
            )
    
    # Schema validation
    schema = enrichment_config.get('schema')
    output_column = enrichment_config.get('output_column')
    if schema:
        try:
            schema_analysis = analyze_schema_complexity(schema, output_column=output_column)

            # Wrap bare type schemas before validation
            schema_for_validation = schema
            if schema_analysis.get('is_bare_type') and output_column:
                schema_for_validation = {output_column: schema}

            # Try to create Pydantic model
            try:
                all_optional = enrichment_config.get('all_fields_optional', False)
                create_pydantic_model_from_schema(schema_for_validation, f"Validation{enrichment_name}", all_fields_optional=all_optional)
            except Exception as e:
                errors.append(f"Invalid schema for '{enrichment_name}': {e}")
                
        except Exception as e:
            errors.append(f"Error analyzing schema for '{enrichment_name}': {e}")
    
    # Input configuration validation
    input_config = enrichment_config.get('input', {})
    if 'query' not in input_config and 'table' not in enrichment_config:
        errors.append(f"Enrichment '{enrichment_name}' must specify input.query or table")
    
    return errors

def get_storage_summary(strategy: EnrichmentStrategy) -> str:
    """
    Generate a human-readable summary of the storage strategy.
    
    Args:
        strategy: EnrichmentStrategy to summarize
        
    Returns:
        Human-readable string describing the storage approach
    """
    if strategy.storage_mode == "direct_column":
        column_name = strategy.output_columns[0] if strategy.output_columns else "unknown"
        return (
            f"Normalized enrichments keyed by {strategy.key_column}; "
            f"single-field alias '{column_name}'"
        )
    else:
        return (
            f"Normalized enrichments keyed by {strategy.key_column}; "
            f"legacy output_table hint '{strategy.output_table}' for "
            f"{len(strategy.output_columns)} field(s)"
        )

def prepare_enrichment_for_processing(enrichment_config: Dict[str, Any],
                                    default_table: str = "documents",
                                    sql_queries: Optional[Dict[str, str]] = None,
                                    config_key_column: str = "sha1") -> Tuple[EnrichmentStrategy, List[str]]:
    """
    Prepare enrichment configuration for processing, validating and determining strategy.

    Args:
        enrichment_config: Raw enrichment configuration from YAML
        default_table: Default input table
        sql_queries: Dictionary of SQL queries from config (for table extraction)

    Returns:
        Tuple of (EnrichmentStrategy, list of validation errors)
    """
    # Validate configuration
    errors = validate_enrichment_config(enrichment_config)
    if errors:
        return None, errors

    try:
        # Determine strategy - pass sql_queries for table extraction
        strategy = determine_enrichment_strategy(enrichment_config, default_table, sql_queries, config_key_column)
        return strategy, []

    except EnrichmentConfigError as e:
        return None, [str(e)]
    except Exception as e:
        return None, [f"Unexpected error processing enrichment: {e}"]

# Example usage and testing
if __name__ == "__main__":
    # Test configurations
    test_configs = [
        # Simple single-field analysis
        {
            "name": "sentiment",
            "schema": {"sentiment_score": {"enum": ["positive", "negative", "neutral"]}},
            "prompt": "Analyze sentiment",
            "input": {"query": "all_docs", "input_columns": ["content"]}
        },
        
        # Complex analysis with a legacy output_table hint
        {
            "name": "comprehensive_analysis",
            "schema": {
                "sentiment": {"enum": ["positive", "negative", "neutral"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "topics": {"type": "array", "items": {"type": "string"}}
            },
            "output_table": "analysis_results",
            "prompt": "Comprehensive analysis",
            "input": {"query": "all_docs", "input_columns": ["content"]}
        },
        
        # Complex schema without output_table (valid in the normalized storage model)
        {
            "name": "multi_field_no_output_table",
            "schema": {
                "field1": {"type": "string"},
                "field2": {"type": "number"}
            },
            "prompt": "Should succeed with normalized storage",
            "input": {"query": "all_docs"}
        }
    ]
    
    for i, config in enumerate(test_configs):
        print(f"\n=== Test Config {i+1}: {config['name']} ===")
        
        strategy, errors = prepare_enrichment_for_processing(config)
        
        if errors:
            print(f"Validation errors: {errors}")
        else:
            print(f"Strategy: {strategy.storage_mode}")
            print(f"   Input: {strategy.input_table} -> {strategy.input_columns}")
            print(f"   Output: {get_storage_summary(strategy)}")
            print(f"   Model: {strategy.pydantic_model.__name__}")
            print(f"   SQL Types: {strategy.sql_column_types}")
