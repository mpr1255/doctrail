"""Configuration validators."""

import os
from typing import List, Dict, Any


class ConfigValidator:
    """Validates configuration structure and values."""
    
    def validate(self, config: Dict[str, Any]) -> List[str]:
        """Validate configuration and return list of errors.
        
        Args:
            config: Configuration dictionary
            
        Returns:
            List of error messages (empty if valid)
        """
        errors = []
        
        # Validate required fields
        if 'database' not in config:
            errors.append("Missing required field: 'database'")
        
        # Validate database path
        if 'database' in config:
            db_path = os.path.expanduser(config['database'])
            if not db_path:
                errors.append("Database path cannot be empty")
        
        # Validate enrichments
        if 'enrichments' in config:
            errors.extend(self._validate_enrichments(config['enrichments']))
        
        # Validate models
        if 'models' in config:
            errors.extend(self._validate_models(config['models']))
        
        # Validate exports
        if 'exports' in config:
            errors.extend(self._validate_exports(config['exports']))
        
        return errors
    
    def _validate_enrichments(self, enrichments: List[Dict[str, Any]]) -> List[str]:
        """Validate enrichment configurations.
        
        Args:
            enrichments: List of enrichment configurations
            
        Returns:
            List of error messages
        """
        errors = []
        
        if not isinstance(enrichments, list):
            errors.append("'enrichments' must be a list")
            return errors
        
        names_seen = set()
        
        for i, enrichment in enumerate(enrichments):
            # Check required fields
            if 'name' not in enrichment:
                errors.append(f"Enrichment {i} missing required field: 'name'")
            else:
                name = enrichment['name']
                if name in names_seen:
                    errors.append(f"Duplicate enrichment name: '{name}'")
                names_seen.add(name)
            
            if 'input' not in enrichment:
                errors.append(f"Enrichment '{enrichment.get('name', i)}' missing required field: 'input'")
            else:
                # Validate input configuration
                input_config = enrichment['input']
                if 'query' not in input_config:
                    errors.append(f"Enrichment '{enrichment.get('name', i)}' missing 'query' in input")
                if 'input_columns' not in input_config:
                    errors.append(f"Enrichment '{enrichment.get('name', i)}' missing 'input_columns' in input")
            
            if 'prompt' not in enrichment:
                errors.append(f"Enrichment '{enrichment.get('name', i)}' missing required field: 'prompt'")
            
            # Check output configuration
            has_output_column = 'output_column' in enrichment or 'output_columns' in enrichment
            if not has_output_column:
                errors.append(f"Enrichment '{enrichment.get('name', i)}' must specify 'output_column' or 'output_columns'")
            
            # Validate schema if present
            if 'schema' in enrichment:
                schema_errors = self._validate_schema(enrichment['schema'], enrichment.get('name', i))
                errors.extend(schema_errors)
        
        return errors
    
    def _validate_schema(self, schema: Any, enrichment_name: str) -> List[str]:
        """Validate schema configuration.
        
        Args:
            schema: Schema configuration
            enrichment_name: Name of enrichment for error messages
            
        Returns:
            List of error messages
        """
        errors = []
        
        # Schema can be a list (enum), string (type), or dict (complex)
        if isinstance(schema, list):
            if not schema:
                errors.append(f"Schema for '{enrichment_name}' cannot be an empty list")
        elif isinstance(schema, str):
            valid_types = ['string', 'integer', 'number', 'boolean']
            if schema not in valid_types:
                errors.append(f"Invalid schema type '{schema}' for '{enrichment_name}'")
        elif isinstance(schema, dict):
            # Complex schema validation would go here
            pass
        else:
            errors.append(f"Invalid schema type for '{enrichment_name}': must be list, string, or dict")
        
        return errors
    
    def _validate_models(self, models: Dict[str, Any]) -> List[str]:
        """Validate model configurations.
        
        Args:
            models: Model configurations
            
        Returns:
            List of error messages
        """
        errors = []
        
        for model_name, model_config in models.items():
            if not isinstance(model_config, dict):
                errors.append(f"Model '{model_name}' configuration must be a dictionary")
                continue
            
            # Validate temperature
            if 'temperature' in model_config:
                temp = model_config['temperature']
                if not isinstance(temp, (int, float)) or temp < 0 or temp > 2:
                    errors.append(f"Model '{model_name}' temperature must be between 0 and 2")
            
            # Validate max_tokens
            if 'max_tokens' in model_config:
                tokens = model_config['max_tokens']
                if not isinstance(tokens, int) or tokens <= 0:
                    errors.append(f"Model '{model_name}' max_tokens must be a positive integer")
        
        return errors
    
    def _validate_exports(self, exports: Dict[str, Any]) -> List[str]:
        """Validate export configurations.
        
        Args:
            exports: Export configurations
            
        Returns:
            List of error messages
        """
        errors = []
        
        for export_name, export_config in exports.items():
            if not isinstance(export_config, dict):
                errors.append(f"Export '{export_name}' configuration must be a dictionary")
                continue
            
            # Check required fields
            if 'query' not in export_config:
                errors.append(f"Export '{export_name}' missing required field: 'query'")
            
            if 'template' not in export_config:
                errors.append(f"Export '{export_name}' missing required field: 'template'")
            
            # Validate formats
            if 'formats' in export_config:
                formats = export_config['formats']
                if not isinstance(formats, list):
                    errors.append(f"Export '{export_name}' formats must be a list")
                else:
                    valid_formats = {'csv', 'json', 'jsonl', 'md', 'markdown', 'html', 'pdf', 'docx', 'txt'}
                    for fmt in formats:
                        if fmt not in valid_formats:
                            errors.append(f"Export '{export_name}' has invalid format: '{fmt}'")
        
        return errors