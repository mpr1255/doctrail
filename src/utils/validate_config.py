#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml",
#     "pydantic",
# ]
# ///

"""Validate enrichment configuration before running."""

import yaml
import sys
from typing import Dict, List, Any
from pathlib import Path

# Import from our modules
from pydantic_schema import create_pydantic_model_from_schema, SchemaConversionError
from enrichment_config import parse_enrichment_config, EnrichmentConfigError

def validate_yaml_schema(schema_def: Dict[str, Any], enrichment_name: str) -> List[str]:
    """Validate a YAML schema definition and return any errors/warnings."""
    errors = []
    warnings = []
    
    # Check for common type mistakes
    for field_name, field_def in schema_def.items():
        if isinstance(field_def, dict):
            # Check for 'number' vs 'integer'
            if field_def.get('type') == 'number':
                warnings.append(f"Field '{field_name}': 'number' type maps to float. Use 'integer' for whole numbers.")
            
            # Check for invalid enum_list with unique_items
            if 'enum_list' in field_def and 'unique_items' in field_def:
                warnings.append(f"Field '{field_name}': 'unique_items' is not supported for enum_list. It's always enforced.")
            
            # Check for both enum and enum_list
            if 'enum' in field_def and 'enum_list' in field_def:
                errors.append(f"Field '{field_name}': Cannot have both 'enum' and 'enum_list'.")
            
            # Validate enum values
            if 'enum' in field_def:
                if not isinstance(field_def['enum'], list):
                    errors.append(f"Field '{field_name}': 'enum' must be a list.")
                elif len(field_def['enum']) == 0:
                    errors.append(f"Field '{field_name}': 'enum' cannot be empty.")
            
            # Validate enum_list values
            if 'enum_list' in field_def:
                if not isinstance(field_def['enum_list'], list):
                    errors.append(f"Field '{field_name}': 'enum_list' must be a list.")
                elif len(field_def['enum_list']) == 0:
                    errors.append(f"Field '{field_name}': 'enum_list' cannot be empty.")
    
    return errors, warnings

def test_pydantic_creation(schema_def: Dict[str, Any], enrichment_name: str) -> tuple[bool, str]:
    """Test if schema can be converted to Pydantic model."""
    try:
        model = create_pydantic_model_from_schema(enrichment_name, schema_def)
        # Try to instantiate with empty data to check for other issues
        test_data = {}
        for field_name, field_def in schema_def.items():
            if isinstance(field_def, dict):
                if 'enum' in field_def:
                    test_data[field_name] = field_def['enum'][0]
                elif 'enum_list' in field_def:
                    test_data[field_name] = [field_def['enum_list'][0]]
                elif field_def.get('type') == 'string':
                    test_data[field_name] = "test"
                elif field_def.get('type') == 'integer':
                    test_data[field_name] = 1
                elif field_def.get('type') == 'number':
                    test_data[field_name] = 1.0
        
        instance = model(**test_data)
        return True, "Schema validates successfully"
    except Exception as e:
        return False, str(e)

def validate_config_file(config_path: str) -> Dict[str, Any]:
    """Validate a config file and return results."""
    results = {
        "valid": True,
        "enrichments": {},
        "errors": [],
        "warnings": []
    }
    
    try:
        # Load YAML
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # Check for enrichments
        if 'enrichments' not in config:
            results["errors"].append("No 'enrichments' section found in config")
            results["valid"] = False
            return results
        
        # Validate each enrichment
        for enrichment in config['enrichments']:
            if not isinstance(enrichment, dict):
                continue
                
            name = enrichment.get('name', 'unnamed')
            enrichment_results = {
                "errors": [],
                "warnings": [],
                "schema_valid": False
            }
            
            # Check for schema
            if 'schema' not in enrichment:
                enrichment_results["warnings"].append("No schema defined")
            else:
                schema = enrichment['schema']
                
                # Validate YAML schema
                errors, warnings = validate_yaml_schema(schema, name)
                enrichment_results["errors"].extend(errors)
                enrichment_results["warnings"].extend(warnings)
                
                # Test Pydantic conversion
                if not errors:
                    success, message = test_pydantic_creation(schema, name)
                    enrichment_results["schema_valid"] = success
                    if not success:
                        enrichment_results["errors"].append(f"Pydantic validation failed: {message}")
                
                # Check for deprecated storage modes with schema-driven
                if enrichment.get('output', {}).get('storage_mode') == 'direct_column':
                    enrichment_results["warnings"].append(
                        "Using 'direct_column' with schema. Consider 'separate_table' for structured data."
                    )
            
            results["enrichments"][name] = enrichment_results
            
            # Update overall validity
            if enrichment_results["errors"]:
                results["valid"] = False
    
    except yaml.YAMLError as e:
        results["errors"].append(f"YAML parsing error: {e}")
        results["valid"] = False
    except Exception as e:
        results["errors"].append(f"Unexpected error: {e}")
        results["valid"] = False
    
    return results

def print_validation_results(results: Dict[str, Any], config_path: str):
    """Print validation results in a user-friendly format."""
    print(f"\n🔍 Validating: {config_path}")
    print("=" * 60)
    
    if results["valid"]:
        print("✅ Configuration is valid!")
    else:
        print("❌ Configuration has errors!")
    
    # Global errors
    if results["errors"]:
        print("\n🚨 Global Errors:")
        for error in results["errors"]:
            print(f"  - {error}")
    
    # Per-enrichment results
    if results["enrichments"]:
        print(f"\n📋 Enrichments ({len(results['enrichments'])} found):")
        for name, enrichment_results in results["enrichments"].items():
            print(f"\n  {name}:")
            
            if enrichment_results["schema_valid"]:
                print(f"    ✅ Schema valid")
            else:
                print(f"    ❌ Schema invalid")
            
            if enrichment_results["errors"]:
                print(f"    🚨 Errors:")
                for error in enrichment_results["errors"]:
                    print(f"      - {error}")
            
            if enrichment_results["warnings"]:
                print(f"    ⚠️  Warnings:")
                for warning in enrichment_results["warnings"]:
                    print(f"      - {warning}")
    
    print("\n" + "=" * 60)
    
    if not results["valid"]:
        print("\n💡 Fix the errors above before running enrichment.")
        print("Common fixes:")
        print("  - Change 'number' to 'integer' for whole numbers")
        print("  - Remove 'unique_items' from enum_list fields")
        print("  - Ensure all enums have at least one value")
    else:
        print("\n🚀 Ready to run enrichment!")

def main():
    """Main entry point for config validation."""
    if len(sys.argv) < 2:
        print("Usage: validate_config.py <config.yml>")
        sys.exit(1)
    
    config_path = sys.argv[1]
    if not Path(config_path).exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)
    
    results = validate_config_file(config_path)
    print_validation_results(results, config_path)
    
    sys.exit(0 if results["valid"] else 1)

if __name__ == "__main__":
    main()