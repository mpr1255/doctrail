"""
Pydantic Schema Generation from YAML Configurations

This module converts YAML schema definitions into Pydantic models for use with OpenAI structured outputs.
Supports the full range of YAML schema types used in doctrail enrichment configurations.
"""

from typing import Dict, Any, Type, List, Optional, Union
from pydantic import BaseModel, Field, create_model, field_validator, ValidationError
from enum import Enum
import logging
from .schema_managers import validate_language, LanguageValidationError

# Import conversion functions from plugins
try:
    from .plugins.chinese_converter import CONVERTERS, LANGUAGE_VALIDATORS
except ImportError:
    # Fallback if plugin is not available
    CONVERTERS = {}
    LANGUAGE_VALIDATORS = {}

logger = logging.getLogger(__name__)

class SchemaConversionError(Exception):
    """Raised when YAML schema cannot be converted to Pydantic model"""
    pass


def yaml_to_pydantic_type(yaml_type_def: Union[str, Dict[str, Any]]) -> tuple:
    """
    Convert YAML type definition to Pydantic field type and constraints.
    
    Args:
        yaml_type_def: YAML type definition (string or dict)
        
    Returns:
        tuple: (python_type, field_kwargs, lang, convert)
        
    Examples:
        yaml_to_pydantic_type("string") -> (str, {}, None, None)
        yaml_to_pydantic_type({"type": "number", "minimum": 0}) -> (float, {"ge": 0}, None, None)
        yaml_to_pydantic_type({"type": "string", "lang": "zh"}) -> (str, {}, "zh", None)
        yaml_to_pydantic_type({"type": "string", "convert": "chinese_to_pinyin"}) -> (str, {}, None, "chinese_to_pinyin")
        yaml_to_pydantic_type({"enum": ["a", "b", "c"]}) -> (CustomEnum, {}, None, None)
    """
    field_kwargs = {}
    lang = None
    convert = None
    
    # Handle string shorthand
    if isinstance(yaml_type_def, str):
        yaml_type_def = {"type": yaml_type_def}
    
    # Extract language requirement and conversion type if present
    if isinstance(yaml_type_def, dict):
        lang = yaml_type_def.get("lang")
        convert = yaml_type_def.get("convert")
    
    # Handle enum types (special case)
    if "enum" in yaml_type_def:
        enum_values = yaml_type_def["enum"]
        if not isinstance(enum_values, list) or len(enum_values) == 0:
            raise SchemaConversionError(f"Enum must be a non-empty list, got: {enum_values}")
        
        # Create dynamic enum class
        enum_name = f"DynamicEnum_{hash(tuple(enum_values)) % 10000}"
        enum_class = Enum(enum_name, {val: val for val in enum_values})
        return (enum_class, field_kwargs, lang, convert)
    
    # Handle enum_list types (list of enums)
    if "enum_list" in yaml_type_def:
        enum_values = yaml_type_def["enum_list"]
        if not isinstance(enum_values, list) or len(enum_values) == 0:
            raise SchemaConversionError(f"Enum list must be a non-empty list, got: {enum_values}")
        
        # Create dynamic enum class
        enum_name = f"DynamicEnumList_{hash(tuple(enum_values)) % 10000}"
        enum_class = Enum(enum_name, {val: val for val in enum_values})
        
        # Handle constraints for list
        if "min_items" in yaml_type_def:
            field_kwargs["min_length"] = yaml_type_def["min_items"]
        if "max_items" in yaml_type_def:
            field_kwargs["max_length"] = yaml_type_def["max_items"]
        
        # Note: For enum_list, we handle deduplication post-processing rather than validation
        # If the model returns duplicate values, we dedupe them after receiving the response
        # This avoids Pydantic validation errors and is more forgiving to LLM outputs
        
        # Return List of the enum
        return (List[enum_class], field_kwargs, lang, convert)
    
    # Handle array shorthand [val1, val2, val3] 
    if isinstance(yaml_type_def, list):
        enum_values = yaml_type_def
        enum_name = f"DynamicEnum_{hash(tuple(enum_values)) % 10000}"
        enum_class = Enum(enum_name, {val: val for val in enum_values})
        return (enum_class, field_kwargs, lang, convert)
    
    # Get base type
    base_type = yaml_type_def.get("type")
    if not base_type:
        raise SchemaConversionError(f"Schema must specify 'type' or 'enum': {yaml_type_def}")
    
    # Map YAML types to Python types
    type_mapping = {
        "string": str,
        "str": str,
        "float": float,  # Standard: use "float" not "number"
        "integer": int,
        "int": int,
        "boolean": bool,
        "bool": bool,
        "array": List[str],  # Default to List[str], can be refined
        "list": List[str],
        "object": Dict[str, Any],
        "dict": Dict[str, Any],
    }
    
    # Warn about deprecated "number" type
    if base_type == "number":
        logger.warning(f"Schema uses deprecated 'number' type - use 'float' instead")
        python_type = float
        base_type = "float"  # Normalize for consistency
    else:
        python_type = type_mapping.get(base_type)
    
    python_type = type_mapping.get(base_type)
    if not python_type:
        raise SchemaConversionError(f"Unsupported type: {base_type}")
    
    # Handle array item types
    if base_type in ["array", "list"]:
        items_def = yaml_type_def.get("items")
        if items_def:
            if isinstance(items_def, str):
                item_type = type_mapping.get(items_def, str)
            elif isinstance(items_def, dict):
                item_type, _, item_lang, item_convert = yaml_to_pydantic_type(items_def)
                # Store array item language/conversion requirements for later processing
                if item_lang or item_convert:
                    # We'll handle this in the schema processing loop below
                    pass
            else:
                item_type = str
            python_type = List[item_type]
    
    # Handle constraints
    constraints = yaml_type_def.copy()
    constraints.pop("type", None)
    constraints.pop("items", None)
    constraints.pop("lang", None)  # Remove lang from constraints as we handle it separately
    constraints.pop("convert", None)  # Remove convert from constraints as we handle it separately
    
    # Map YAML constraints to Pydantic Field constraints
    constraint_mapping = {
        "minimum": "ge",
        "maximum": "le", 
        "minLength": "min_length",
        "maxLength": "max_length",
        "minItems": "min_length",  # For arrays
        "maxItems": "max_length",  # For arrays
        "pattern": "regex",
    }
    
    for yaml_key, pydantic_key in constraint_mapping.items():
        if yaml_key in constraints:
            field_kwargs[pydantic_key] = constraints[yaml_key]
    
    # Handle default values
    if "default" in constraints:
        field_kwargs["default"] = constraints["default"]
    
    # Handle descriptions
    if "description" in constraints:
        field_kwargs["description"] = constraints["description"]
    
    # Handle optional/nullable fields
    if constraints.get("optional", False) or constraints.get("nullable", False):
        field_kwargs["__optional__"] = True
    
    return (python_type, field_kwargs, lang, convert)

def create_pydantic_model_from_schema(schema: Dict[str, Any], model_name: str = "DynamicModel", all_fields_optional: bool = False) -> Type[BaseModel]:
    """
    Create a Pydantic model from a YAML schema definition.
    
    Args:
        schema: Dictionary containing field definitions
        model_name: Name for the generated model class
        all_fields_optional: If True, all fields will be optional by default
        
    Returns:
        Pydantic model class
        
    Example:
        schema = {
            "sentiment": {"enum": ["positive", "negative", "neutral"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "topics": {"type": "array", "items": {"type": "string"}}
        }
        Model = create_pydantic_model_from_schema(schema, "AnalysisResult")
    """
    if not isinstance(schema, dict):
        raise SchemaConversionError(f"Schema must be a dictionary, got: {type(schema)}")
    
    if not schema:
        raise SchemaConversionError("Schema cannot be empty")
    
    fields = {}
    validators = {}  # Store custom validators for language validation
    
    for field_name, field_def in schema.items():
        try:
            python_type, field_kwargs, lang, convert = yaml_to_pydantic_type(field_def)
            
            # Check if field is optional
            is_optional = field_kwargs.pop("__optional__", False) or all_fields_optional
            
            # If optional, wrap type in Optional
            if is_optional:
                python_type = Optional[python_type]
            
            # Create Field with constraints if any exist
            if field_kwargs:
                # For optional fields, set default to None if not already set
                if is_optional and "default" not in field_kwargs:
                    field_kwargs["default"] = None
                fields[field_name] = (python_type, Field(**field_kwargs))
            else:
                # For required fields use ..., for optional use None
                default_value = None if is_optional else ...
                fields[field_name] = (python_type, default_value)
            
            # Language validation will be handled separately after model creation
                
        except Exception as e:
            raise SchemaConversionError(f"Error processing field '{field_name}': {e}")
    
    try:
        # Create the dynamic model 
        model = create_model(model_name, **fields)
        
        # Add language validation and conversion as custom methods
        language_requirements = {}
        conversion_requirements = {}
        array_language_requirements = {}  # field_name -> lang for array items
        array_conversion_requirements = {}  # field_name -> convert for array items
        
        for field_name, field_def in schema.items():
            if isinstance(field_def, dict):
                # Handle direct field language/conversion requirements
                if field_def.get('lang'):
                    language_requirements[field_name] = field_def['lang']
                if field_def.get('convert'):
                    conversion_requirements[field_name] = field_def['convert']
                
                # Handle array item language/conversion requirements
                if field_def.get('type') == 'array' and field_def.get('items'):
                    items_def = field_def['items']
                    if isinstance(items_def, dict):
                        if items_def.get('lang'):
                            array_language_requirements[field_name] = items_def['lang']
                        if items_def.get('convert'):
                            array_conversion_requirements[field_name] = items_def['convert']
        
        # Add custom validation and conversion methods to the model
        def apply_conversions(instance):
            """Apply field conversions like chinese_to_pinyin."""
            # Handle direct field conversions
            for field_name, conversion_type in conversion_requirements.items():
                value = getattr(instance, field_name, None)
                if value is not None:
                    converter = CONVERTERS.get(conversion_type)
                    if converter:
                        converted_value = converter(str(value))
                        setattr(instance, field_name, converted_value)
                        logger.debug(f"Converted {field_name}: '{value}' → '{converted_value}'")
            
            # Handle array item conversions
            for field_name, conversion_type in array_conversion_requirements.items():
                value_list = getattr(instance, field_name, None)
                if value_list is not None and isinstance(value_list, list):
                    converter = CONVERTERS.get(conversion_type)
                    if converter:
                        converted_list = [converter(str(item)) for item in value_list]
                        setattr(instance, field_name, converted_list)
                        logger.debug(f"Converted array {field_name}: {value_list} → {converted_list}")
        
        def validate_languages(instance):
            """Validate language requirements for fields."""
            # Handle direct field language validation
            for field_name, expected_lang in language_requirements.items():
                value = getattr(instance, field_name, None)
                if value is not None and not validate_language(str(value), expected_lang):
                    expected_desc = "Chinese characters" if expected_lang.lower() == 'zh' else "English (no Chinese characters)"
                    raise LanguageValidationError(
                        f"Field '{field_name}' must be in {expected_lang} ({expected_desc}), got: {str(value)[:50]}..."
                    )
            
            # Handle array item language validation
            for field_name, expected_lang in array_language_requirements.items():
                value_list = getattr(instance, field_name, None)
                if value_list is not None and isinstance(value_list, list):
                    for i, item in enumerate(value_list):
                        if item is not None and not validate_language(str(item), expected_lang):
                            expected_desc = "Chinese characters" if expected_lang.lower() == 'zh' else "English (no Chinese characters)"
                            raise LanguageValidationError(
                                f"Array field '{field_name}' item {i} must be in {expected_lang} ({expected_desc}), got: {str(item)[:50]}..."
                            )
        
        # Monkey patch the methods onto the model as static methods
        model.apply_conversions = staticmethod(apply_conversions)
        model.validate_languages = staticmethod(validate_languages)
        model._language_requirements = language_requirements
        model._conversion_requirements = conversion_requirements
        model._array_language_requirements = array_language_requirements
        model._array_conversion_requirements = array_conversion_requirements
        
        logger.debug(f"Created Pydantic model '{model_name}' with fields: {list(fields.keys())}")
        if language_requirements:
            logger.debug(f"Added language requirements: {language_requirements}")
        if array_language_requirements:
            logger.debug(f"Added array language requirements: {array_language_requirements}")
        if conversion_requirements:
            logger.debug(f"Added conversion requirements: {conversion_requirements}")
        if array_conversion_requirements:
            logger.debug(f"Added array conversion requirements: {array_conversion_requirements}")
        return model
        
    except Exception as e:
        raise SchemaConversionError(f"Failed to create Pydantic model '{model_name}': {e}")

def analyze_schema_complexity(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyze schema to determine storage strategy.
    
    Returns:
        {
            "field_count": int,
            "is_simple": bool,  # Single field
            "is_complex": bool,  # Multiple fields
            "field_names": List[str],
            "requires_separate_table": bool
        }
    """
    if not isinstance(schema, dict):
        return {
            "field_count": 0,
            "is_simple": False,
            "is_complex": False, 
            "field_names": [],
            "requires_separate_table": False
        }
    
    field_count = len(schema)
    field_names = list(schema.keys())
    
    return {
        "field_count": field_count,
        "is_simple": field_count == 1,
        "is_complex": field_count > 1,
        "field_names": field_names,
        "requires_separate_table": field_count > 1
    }

def get_sql_type_from_pydantic_type(python_type: Type) -> str:
    """
    Convert Python/Pydantic type to SQLite column type.
    
    Args:
        python_type: Python type from Pydantic model
        
    Returns:
        SQLite column type string
    """
    # Handle generic types
    origin = getattr(python_type, '__origin__', None)
    
    if origin is list or origin is List:
        return "TEXT"  # Store arrays as JSON strings
    elif origin is dict or origin is Dict:
        return "TEXT"  # Store objects as JSON strings
    elif python_type == str:
        return "TEXT"
    elif python_type == int:
        return "INTEGER" 
    elif python_type == float:
        return "REAL"
    elif python_type == bool:
        return "INTEGER"  # SQLite uses INTEGER for boolean
    elif issubclass(python_type, Enum):
        return "TEXT"  # Store enums as strings
    else:
        return "TEXT"  # Default fallback

# Example usage and testing
if __name__ == "__main__":
    # Test various schema types
    test_schemas = [
        # Simple enum
        {"sentiment": {"enum": ["positive", "negative", "neutral"]}},
        
        # Complex analysis
        {
            "sentiment": {"enum": ["positive", "negative", "neutral"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "topics": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            "word_count": {"type": "integer", "minimum": 0}
        },
        
        # Shorthand array syntax
        {"category": ["research", "policy", "news", "blog"]},
        
        # Mixed types
        {
            "classification": ["technical", "business", "personal"],
            "urgency_score": {"type": "integer", "minimum": 1, "maximum": 5},
            "summary": {"type": "string", "maxLength": 500}
        }
    ]
    
    for i, schema in enumerate(test_schemas):
        print(f"\n=== Test Schema {i+1} ===")
        print(f"Schema: {schema}")
        
        try:
            analysis = analyze_schema_complexity(schema)
            print(f"Analysis: {analysis}")
            
            model = create_pydantic_model_from_schema(schema, f"TestModel{i+1}")
            print(f"Model created: {model.__name__}")
            print(f"Fields: {list(model.model_fields.keys())}")
            
        except Exception as e:
            print(f"Error: {e}")