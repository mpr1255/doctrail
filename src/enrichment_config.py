"""
Enrichment Configuration Processing

This module handles the logic for determining storage strategies based on enrichment configurations
and schemas. It bridges the gap between YAML configuration and database operations.
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
    
    # Storage mode
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
                                default_table: str = "documents") -> EnrichmentStrategy:
    """
    Analyze enrichment configuration and determine the optimal storage strategy.
    
    Args:
        enrichment_config: Enrichment configuration from YAML
        default_table: Default input table if not specified
        
    Returns:
        EnrichmentStrategy object containing all storage decisions
        
    Raises:
        EnrichmentConfigError: If configuration is invalid
    """
    enrichment_name = enrichment_config.get('name', 'unnamed')
    
    # Extract schema
    schema_dict = enrichment_config.get('schema')
    if not schema_dict:
        raise EnrichmentConfigError(f"Enrichment '{enrichment_name}' must specify a schema")
    
    # Warn about implicit enum syntax
    if isinstance(schema_dict, list):
        logger.warning(f"Enrichment '{enrichment_name}' uses implicit enum syntax - "
                      f"consider using explicit 'enum: {schema_dict}' for clarity")
    
    # Analyze schema complexity
    schema_analysis = analyze_schema_complexity(schema_dict)
    
    # Determine input table
    input_table = enrichment_config.get('input_table') or \
                 enrichment_config.get('table') or \
                 default_table
    
    # Determine input columns
    input_config = enrichment_config.get('input', {})
    input_columns = input_config.get('input_columns', ['raw_content'])
    if isinstance(input_columns, str):
        input_columns = [input_columns]
    
    # Determine storage mode based on schema and explicit output_table
    output_table = enrichment_config.get('output_table')
    
    if schema_analysis['is_complex'] and not output_table:
        # Complex schema REQUIRES separate table
        raise EnrichmentConfigError(
            f"Enrichment '{enrichment_name}' has complex schema with {schema_analysis['field_count']} fields "
            f"but no output_table specified. Complex schemas require output_table."
        )
    
    # Determine storage mode
    if output_table:
        storage_mode = "separate_table"
        target_table = output_table
    else:
        # Simple schema, no output_table -> direct column mode
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
    
    # Extract key column (default to sha1)
    key_column = enrichment_config.get('key_column', 'sha1')
    
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
    
    if not enrichment_config.get('schema'):
        errors.append(f"Enrichment '{enrichment_name}' must specify a 'schema'")
    
    if not enrichment_config.get('prompt'):
        errors.append(f"Enrichment '{enrichment_name}' must specify a 'prompt'")
    
    # Schema validation
    schema = enrichment_config.get('schema')
    if schema:
        try:
            schema_analysis = analyze_schema_complexity(schema)
            
            # Complex schema must have output_table
            if schema_analysis['is_complex'] and not enrichment_config.get('output_table'):
                errors.append(
                    f"Enrichment '{enrichment_name}' has complex schema with "
                    f"{schema_analysis['field_count']} fields but no output_table specified"
                )
            
            # Try to create Pydantic model
            try:
                all_optional = enrichment_config.get('all_fields_optional', False)
                create_pydantic_model_from_schema(schema, f"Validation{enrichment_name}", all_fields_optional=all_optional)
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
        return f"Direct column '{column_name}' in table '{strategy.input_table}'"
    else:
        return (f"Separate table '{strategy.output_table}' with {len(strategy.output_columns)} columns "
                f"keyed by {strategy.key_column}")

def prepare_enrichment_for_processing(enrichment_config: Dict[str, Any], 
                                    default_table: str = "documents") -> Tuple[EnrichmentStrategy, List[str]]:
    """
    Prepare enrichment configuration for processing, validating and determining strategy.
    
    Args:
        enrichment_config: Raw enrichment configuration from YAML
        default_table: Default input table
        
    Returns:
        Tuple of (EnrichmentStrategy, list of validation errors)
    """
    # Validate configuration
    errors = validate_enrichment_config(enrichment_config)
    if errors:
        return None, errors
    
    try:
        # Determine strategy
        strategy = determine_enrichment_strategy(enrichment_config, default_table)
        return strategy, []
        
    except EnrichmentConfigError as e:
        return None, [str(e)]
    except Exception as e:
        return None, [f"Unexpected error processing enrichment: {e}"]

# Example usage and testing
if __name__ == "__main__":
    # Test configurations
    test_configs = [
        # Simple sentiment analysis (direct column)
        {
            "name": "sentiment",
            "schema": {"sentiment_score": {"enum": ["positive", "negative", "neutral"]}},
            "prompt": "Analyze sentiment",
            "input": {"query": "all_docs", "input_columns": ["content"]}
        },
        
        # Complex analysis (requires separate table)
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
        
        # Invalid: complex schema without output_table
        {
            "name": "invalid_complex",
            "schema": {
                "field1": {"type": "string"},
                "field2": {"type": "number"}
            },
            "prompt": "Should fail",
            "input": {"query": "all_docs"}
        }
    ]
    
    for i, config in enumerate(test_configs):
        print(f"\n=== Test Config {i+1}: {config['name']} ===")
        
        strategy, errors = prepare_enrichment_for_processing(config)
        
        if errors:
            print(f"❌ Validation errors: {errors}")
        else:
            print(f"✅ Strategy: {strategy.storage_mode}")
            print(f"   Input: {strategy.input_table} -> {strategy.input_columns}")
            print(f"   Output: {get_storage_summary(strategy)}")
            print(f"   Model: {strategy.pydantic_model.__name__}")
            print(f"   SQL Types: {strategy.sql_column_types}")