"""
Schema managers for different types of schemas in Doctrail.
Supports enum schemas, simple schemas, and structured (XML) schemas.
"""
import logging
from typing import Dict, List, Any, Union, Optional
from pydantic import BaseModel, ValidationError, create_model
import re
import unicodedata


class SchemaValidationError(Exception):
    """Raised when schema validation fails."""
    pass


class LanguageValidationError(SchemaValidationError):
    """Raised when language validation fails."""
    pass


def contains_hanzi(text: str) -> bool:
    """Check if text contains Chinese characters (hanzi)."""
    for char in text:
        # Check if character is in CJK (Chinese, Japanese, Korean) unicode blocks
        # This covers most Chinese characters
        if '\u4e00' <= char <= '\u9fff':  # CJK Unified Ideographs
            return True
        if '\u3400' <= char <= '\u4dbf':  # CJK Extension A
            return True
        if '\u20000' <= char <= '\u2a6df':  # CJK Extension B
            return True
        if '\u2a700' <= char <= '\u2b73f':  # CJK Extension C
            return True
        if '\u2b740' <= char <= '\u2b81f':  # CJK Extension D
            return True
        if '\u2b820' <= char <= '\u2ceaf':  # CJK Extension E
            return True
        if '\uf900' <= char <= '\ufaff':   # CJK Compatibility Ideographs
            return True
        if '\u2f800' <= char <= '\u2fa1f': # CJK Compatibility Supplement
            return True
    return False


def validate_language(text: str, lang: str) -> bool:
    """Validate that text matches the expected language.
    
    Args:
        text: Text to validate
        lang: Expected language ('zh' for Chinese, 'en' for English)
        
    Returns:
        True if text matches expected language, False otherwise
    """
    if not text or not lang:
        return True  # No validation if no text or no lang specified
    
    has_hanzi = contains_hanzi(text)
    
    if lang.lower() == 'zh':
        # For Chinese, expect hanzi to be present
        return has_hanzi
    elif lang.lower() == 'en':
        # For English, expect no hanzi
        return not has_hanzi
    else:
        # Unknown language, no validation
        return True


class BaseSchemaManager:
    """Base class for all schema managers."""
    
    def __init__(self, name: str, definition: Any):
        self.name = name
        self.definition = definition
    
    def validate_response(self, response: str) -> Any:
        """Validate and parse a response according to this schema."""
        raise NotImplementedError
    
    def get_prompt_instructions(self) -> str:
        """Get instructions to add to the LLM prompt."""
        raise NotImplementedError


class EnumSchemaManager(BaseSchemaManager):
    """Manages enum/choice schemas with strict validation."""
    
    def __init__(self, name: str, definition: Dict[str, Any]):
        super().__init__(name, definition)
        self.choices = definition.get('choices', [])
        self.case_sensitive = definition.get('case_sensitive', True)
        
        if not self.choices:
            raise ValueError(f"Enum schema '{name}' must have a 'choices' list")
        
        logging.info(f"Created enum schema '{name}' with choices: {self.choices}")
    
    def validate_response(self, response: str) -> str:
        """Strictly validate that response is exactly one of the allowed choices."""
        # Clean the response - strip whitespace and remove quotes
        cleaned_response = response.strip().strip('"\'')
        
        # Check for exact match (case sensitive or insensitive)
        choices_to_check = self.choices
        response_to_check = cleaned_response
        
        if not self.case_sensitive:
            choices_to_check = [choice.lower() for choice in self.choices]
            response_to_check = cleaned_response.lower()
        
        if response_to_check in choices_to_check:
            # Return the original case from our choices list
            if self.case_sensitive:
                return cleaned_response
            else:
                # Find the original case version
                for i, lower_choice in enumerate(choices_to_check):
                    if lower_choice == response_to_check:
                        return self.choices[i]
        
        # Response doesn't match any choice - reject it
        raise SchemaValidationError(
            f"Response '{cleaned_response}' is not one of the allowed choices: {self.choices}"
        )
    
    def get_prompt_instructions(self) -> str:
        """Generate aggressive prompt instructions to force enum compliance."""
        choices_str = ", ".join(f'"{choice}"' for choice in self.choices)
        
        return f"""
CRITICAL INSTRUCTION: You MUST respond with EXACTLY one of these words: {choices_str}

DO NOT:
- Add any explanation or additional text
- Use different capitalization
- Add punctuation
- Use synonyms or variations
- Return multiple choices

ONLY return the exact word from the list above. Your response will be automatically rejected if it doesn't match exactly.

Valid responses: {choices_str}
"""


class EnumListSchemaManager(BaseSchemaManager):
    """Manages list of enums - where the model can return multiple allowed values."""
    
    def __init__(self, name: str, definition: Dict[str, Any]):
        super().__init__(name, definition)
        self.choices = definition.get('choices', [])
        self.case_sensitive = definition.get('case_sensitive', True)
        self.min_items = definition.get('min_items', 0)
        self.max_items = definition.get('max_items', None)
        self.unique_items = definition.get('unique_items', True)  # Default to True
        
        if not self.choices:
            raise ValueError(f"Enum list schema '{name}' must have a 'choices' list")
        
        logging.info(f"Created enum list schema '{name}' with choices: {self.choices}, min_items: {self.min_items}, max_items: {self.max_items}, unique_items: {self.unique_items}")
    
    def validate_response(self, response: str) -> List[str]:
        """Validate that response contains a list of allowed choices."""
        import json
        
        # Clean the response
        cleaned_response = response.strip()
        
        # Try to parse as JSON array first
        parsed_list = None
        try:
            parsed_list = json.loads(cleaned_response)
            if not isinstance(parsed_list, list):
                # If it's a single string, wrap it in a list
                if isinstance(parsed_list, str):
                    parsed_list = [parsed_list]
                else:
                    raise ValueError("Response must be a list")
        except json.JSONDecodeError:
            # Try to parse as comma-separated values
            if ',' in cleaned_response:
                parsed_list = [item.strip().strip('"\'') for item in cleaned_response.split(',')]
            else:
                # Single value
                parsed_list = [cleaned_response.strip('"\'')]
        
        # Validate each item
        validated_items = []
        choices_to_check = self.choices
        
        if not self.case_sensitive:
            choices_to_check = [choice.lower() for choice in self.choices]
        
        for item in parsed_list:
            item_to_check = item
            if not self.case_sensitive:
                item_to_check = item.lower()
            
            if item_to_check in choices_to_check:
                # Find the original case version
                if self.case_sensitive:
                    validated_items.append(item)
                else:
                    for i, lower_choice in enumerate(choices_to_check):
                        if lower_choice == item_to_check:
                            validated_items.append(self.choices[i])
                            break
            else:
                raise SchemaValidationError(
                    f"Item '{item}' is not one of the allowed choices: {self.choices}"
                )
        
        # Apply deduplication if unique_items is True (default)
        if self.unique_items:
            # Preserve order while removing duplicates
            seen = set()
            deduplicated_items = []
            for item in validated_items:
                if item not in seen:
                    seen.add(item)
                    deduplicated_items.append(item)
            validated_items = deduplicated_items
        
        # Check min/max constraints
        if self.min_items and len(validated_items) < self.min_items:
            raise SchemaValidationError(
                f"Response must contain at least {self.min_items} items, got {len(validated_items)}"
            )
        
        if self.max_items and len(validated_items) > self.max_items:
            raise SchemaValidationError(
                f"Response must contain at most {self.max_items} items, got {len(validated_items)}"
            )
        
        return validated_items
    
    def get_prompt_instructions(self) -> str:
        """Generate prompt instructions for enum list compliance."""
        choices_str = ", ".join(f'"{choice}"' for choice in self.choices)
        
        min_max_str = ""
        if self.min_items and self.max_items:
            min_max_str = f"\n- You MUST return between {self.min_items} and {self.max_items} items"
        elif self.min_items:
            min_max_str = f"\n- You MUST return at least {self.min_items} items"
        elif self.max_items:
            min_max_str = f"\n- You MUST return at most {self.max_items} items"
        
        # Add note about unique items if applicable
        unique_note = ""
        if self.unique_items:
            unique_note = "\n- Each value should appear only once (duplicates will be removed)"
            if self.min_items:
                unique_note += f"\n- Ensure you have at least {self.min_items} UNIQUE values"
        
        return f"""
CRITICAL INSTRUCTION: You MUST respond with a JSON array containing ONLY these allowed values: {choices_str}

Format your response as a JSON array, for example: ["choice1", "choice2"]
{min_max_str}{unique_note}

DO NOT:
- Add any explanation or additional text
- Use values not in the allowed list
- Use different capitalization
- Add punctuation to the values
- Use synonyms or variations

Valid values: {choices_str}
"""


class SimpleSchemaManager(BaseSchemaManager):
    """Manages simple type schemas (legacy support)."""
    
    def __init__(self, name: str, definition: Any):
        super().__init__(name, definition)
        self.lang = None
        
        # Handle dict definitions with type and lang
        if isinstance(definition, dict):
            self.python_type = definition.get('type', 'string')
            self.lang = definition.get('lang')
        else:
            self.python_type = definition
        
        # Map string types to Python types
        if isinstance(self.python_type, str):
            type_mapping = {
                'str': str,
                'string': str,
                'int': int,
                'integer': int,
                'float': float,
                'bool': bool,
                'boolean': bool,
            }
            self.python_type = type_mapping.get(self.python_type.lower(), str)
        
        logging.info(f"Created simple schema '{name}' with type: {self.python_type}, lang: {self.lang}")
    
    def validate_response(self, response: str) -> Any:
        """Validate response according to the simple type and language."""
        cleaned_response = response.strip()
        
        # First validate language if specified
        if self.lang and self.python_type == str:
            if not validate_language(cleaned_response, self.lang):
                raise LanguageValidationError(
                    f"Response '{cleaned_response}' does not match expected language '{self.lang}'"
                )
        
        try:
            if self.python_type == bool:
                # Handle boolean conversion
                lower_response = cleaned_response.lower()
                if lower_response in ('true', 'yes', '1', 'on'):
                    return True
                elif lower_response in ('false', 'no', '0', 'off'):
                    return False
                else:
                    raise ValueError(f"Cannot convert '{cleaned_response}' to boolean")
            
            elif self.python_type == str:
                return cleaned_response
            
            else:
                # Try direct type conversion
                return self.python_type(cleaned_response)
                
        except (ValueError, TypeError) as e:
            raise SchemaValidationError(
                f"Cannot convert response '{cleaned_response}' to type {self.python_type.__name__}: {e}"
            )
    
    def get_prompt_instructions(self) -> str:
        """Get prompt instructions for simple types."""
        base_instruction = ""
        if self.python_type == bool:
            base_instruction = "Respond with exactly 'true' or 'false' (no quotes)."
        elif self.python_type in (int, float):
            base_instruction = f"Respond with a single {self.python_type.__name__} value (no extra text)."
        else:
            base_instruction = f"Respond with a single {self.python_type.__name__} value."
        
        # Add language instruction if specified
        if self.lang:
            if self.lang.lower() == 'zh':
                base_instruction += " MUST be in Chinese (contain Chinese characters)."
            elif self.lang.lower() == 'en':
                base_instruction += " MUST be in English (no Chinese characters)."
            else:
                base_instruction += f" MUST be in language: {self.lang}."
        
        return base_instruction


def get_schema_manager(config: Dict[str, Any], schema_name: str) -> Optional[BaseSchemaManager]:
    """Get a schema manager by name from the loaded config."""
    if '_schema_managers' in config:
        return config['_schema_managers'].get(schema_name)
    return None


def create_inline_schema_manager(schema_def: Any) -> BaseSchemaManager:
    """Create a schema manager from an inline schema definition."""
    if isinstance(schema_def, dict):
        if 'enum' in schema_def:
            # Inline enum: schema: { enum: ["choice1", "choice2"] }
            return EnumSchemaManager("inline_enum", {
                'type': 'enum',
                'choices': schema_def['enum'],
                'case_sensitive': schema_def.get('case_sensitive', True)
            })
        elif 'enum_list' in schema_def:
            # Inline enum list: schema: { enum_list: ["choice1", "choice2"], min_items: 1, max_items: 3 }
            return EnumListSchemaManager("inline_enum_list", {
                'type': 'enum_list',
                'choices': schema_def['enum_list'],
                'case_sensitive': schema_def.get('case_sensitive', True),
                'min_items': schema_def.get('min_items', 0),
                'max_items': schema_def.get('max_items', None),
                'unique_items': schema_def.get('unique_items', True)
            })
        elif 'boolean' in schema_def or schema_def.get('type') == 'boolean':
            # Inline boolean: schema: { boolean: true } or schema: { type: "boolean" }
            return SimpleSchemaManager("inline_boolean", bool)
        elif 'type' in schema_def:
            # Check if it's an enum_list type
            if schema_def['type'] == 'enum_list' and 'choices' in schema_def:
                # Ensure unique_items default is set
                if 'unique_items' not in schema_def:
                    schema_def = dict(schema_def)  # Copy to avoid modifying original
                    schema_def['unique_items'] = True
                return EnumListSchemaManager("inline_enum_list", schema_def)
            # Generic type specification
            return SimpleSchemaManager("inline_type", schema_def['type'])
    
    elif isinstance(schema_def, str):
        # Simple string type: schema: "boolean" or schema: "str"
        return SimpleSchemaManager("inline_simple", schema_def)
    
    elif isinstance(schema_def, list):
        # Direct enum list: schema: ["choice1", "choice2"]
        return EnumSchemaManager("inline_enum_list", {
            'type': 'enum',
            'choices': schema_def
        })
    
    raise ValueError(f"Unsupported inline schema definition: {schema_def}")


def validate_with_schema(config: Dict[str, Any], schema_def: Union[str, Dict, List], response: str) -> Any:
    """Validate a response using either a named schema or inline schema definition."""
    schema_manager = None
    
    if isinstance(schema_def, str):
        # Named schema reference
        schema_manager = get_schema_manager(config, schema_def)
        if not schema_manager:
            raise ValueError(f"Schema '{schema_def}' not found in configuration")
    else:
        # Inline schema definition
        schema_manager = create_inline_schema_manager(schema_def)
    
    try:
        return schema_manager.validate_response(response)
    except SchemaValidationError:
        # Re-raise schema validation errors as-is
        raise
    except Exception as e:
        # Wrap other exceptions
        raise SchemaValidationError(f"Schema validation failed: {e}")


def get_schema_prompt_instructions(config: Dict[str, Any], schema_def: Union[str, Dict, List]) -> str:
    """Get prompt instructions for either a named schema or inline schema definition."""
    schema_manager = None
    
    if isinstance(schema_def, str):
        # Named schema reference
        schema_manager = get_schema_manager(config, schema_def)
        if not schema_manager:
            return ""
    else:
        # Inline schema definition
        try:
            schema_manager = create_inline_schema_manager(schema_def)
        except ValueError:
            return ""
    
    return schema_manager.get_prompt_instructions() if schema_manager else ""