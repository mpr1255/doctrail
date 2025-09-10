import importlib.util
import logging
import sys
import os
import platform
import socket
from datetime import datetime
from pathlib import Path
from typing import Optional, Type, List, Tuple, Any, Dict
from pydantic import BaseModel
import re
import yaml
from .schema_managers import EnumSchemaManager, SimpleSchemaManager, BaseSchemaManager

def _handle_import_constructor(loader, node):
    """Handle !import tag in YAML files"""
    # Get the import path
    import_path = loader.construct_scalar(node)
    
    # Get the directory of the current YAML file
    current_file = loader.name
    current_dir = os.path.dirname(current_file)
    
    # Resolve the import path relative to the current YAML file
    full_import_path = os.path.join(current_dir, import_path)
    full_import_path = os.path.expanduser(full_import_path)
    
    # Load the imported YAML file
    with open(full_import_path, 'r') as f:
        imported_data = yaml.safe_load(f)
    
    return imported_data

def load_config(config_path: str) -> Dict:
    """Loads configuration from a YAML file."""
    try:
        # Create custom YAML loader class with import support
        class ImportLoader(yaml.SafeLoader):
            pass
        
        ImportLoader.add_constructor('!import', _handle_import_constructor)
        
        # Expand user path ~ if present
        expanded_path = os.path.expanduser(config_path)
        if not os.path.exists(expanded_path):
             raise FileNotFoundError(f"Config file not found at {expanded_path}")
        with open(expanded_path, 'r') as f:
            # Create a loader instance and store the file path for relative imports
            loader = ImportLoader(f)
            loader.name = expanded_path
            try:
                config_data = loader.get_single_data()
            finally:
                loader.dispose()
        if not isinstance(config_data, dict):
             raise TypeError(f"Config file {expanded_path} did not load as a dictionary.")
        
        # Store the config file path for relative path resolution
        config_data['__config_path__'] = os.path.abspath(expanded_path)
        
        # Process enrichments list to handle imported enrichments
        if 'enrichments' in config_data and isinstance(config_data['enrichments'], list):
            processed_enrichments = []
            for item in config_data['enrichments']:
                if isinstance(item, dict):
                    # If it's a dict, it could be an imported enrichment or a regular one
                    if 'name' in item:
                        # It's a complete enrichment (either imported or inline)
                        processed_enrichments.append(item)
                    else:
                        # It might have other keys, just add it
                        processed_enrichments.append(item)
                else:
                    # Handle other types if needed
                    logging.warning(f"Unexpected enrichment type: {type(item)}")
            config_data['enrichments'] = processed_enrichments
        
        # Process sql_queries to merge any imported queries
        if 'sql_queries' in config_data and isinstance(config_data['sql_queries'], dict):
            # SQL queries might also be imported, so we need to handle nested dicts
            merged_queries = {}
            for key, value in config_data['sql_queries'].items():
                if isinstance(value, dict) and 'sql_queries' in value:
                    # This is an imported SQL queries file
                    merged_queries.update(value['sql_queries'])
                else:
                    # Regular SQL query
                    merged_queries[key] = value
            config_data['sql_queries'] = merged_queries
        
        # Load schemas if present
        schemas_data = config_data.get('schemas', {})
        
        if schemas_data:
            config_data['_schema_managers'] = {}
            
            # Load schema format
            for schema_name, schema_def in schemas_data.items():
                if isinstance(schema_def, dict) and schema_def.get('type') == 'enum':
                    config_data['_schema_managers'][schema_name] = EnumSchemaManager(schema_name, schema_def)
                    logging.info(f"Loaded enum schema: {schema_name}")
                else:
                    # Legacy format - treat as simple type
                    config_data['_schema_managers'][schema_name] = SimpleSchemaManager(schema_name, schema_def)
                    logging.info(f"Loaded simple schema: {schema_name}")
        
        logging.info(f"Successfully loaded configuration from {expanded_path}")
        return config_data
    except FileNotFoundError as e:
        logging.error(f"Configuration file not found: {e}")
        raise  # Re-raise after logging
    except yaml.YAMLError as e:
        logging.error(f"Error parsing YAML configuration file {config_path}: {e}")
        raise  # Re-raise after logging
    except Exception as e:
        logging.error(f"An unexpected error occurred loading config {config_path}: {e}")
        raise # Re-raise after logging

def load_pydantic_model(file_path: str, model_name: Optional[str]) -> Type[BaseModel]:
    spec = importlib.util.spec_from_file_location("dynamic_model", file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    
    if model_name:
        return getattr(module, model_name)
    else:
        # Return the first BaseModel subclass found in the module
        return next(obj for name, obj in module.__dict__.items() 
                    if isinstance(obj, type) and issubclass(obj, BaseModel))

def setup_logging(verbose: bool):
    """Set up robust logging for enrichment operations"""
    # Configure root logger
    level = logging.DEBUG if verbose else logging.WARNING  # Less verbose by default

    # Clear any existing handlers
    root = logging.getLogger()
    if root.handlers:
        for handler in root.handlers[:]: # Iterate over a copy
            root.removeHandler(handler)
            handler.close() # Close handlers before removing

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout) # Explicitly use stdout
    console_handler.setLevel(level)
    # Use a simpler formatter for console to avoid duplicate timestamps if loguru is also used
    console_formatter = logging.Formatter('%(message)s')
    console_handler.setFormatter(console_formatter)

    # Create file handler for detailed logs in /tmp
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    try:
        log_dir = Path("/tmp/doctrail_logs")
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"doctrail_{timestamp}.log"

        # Create detailed file handler
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)  # Always use DEBUG level for file
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)

        # Create a debug file with system info
        debug_file = log_dir / f"doctrail_debug_{timestamp}.log"
        with open(debug_file, 'w') as f:
            # Write system information
            f.write(f"SQLite Enricher Debug Information\n")
            f.write(f"==============================\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write(f"Python version: {sys.version}\n")
            f.write(f"Platform: {platform.platform()}\n")
            f.write(f"Hostname: {socket.gethostname()}\n")
            f.write(f"Current directory: {os.getcwd()}\n")
            # Use try-except for script directory in case __file__ is not defined (e.g., interactive)
            try:
                 script_dir = Path(__file__).parent.absolute()
                 f.write(f"Script directory: {script_dir}\n\n")
            except NameError:
                 f.write(f"Script directory: Not available\n\n")
            f.write(f"Environment variables:\n")
            for key, value in os.environ.items():
                f.write(f"  {key}={value}\n")

        # Configure root logger
        root.setLevel(logging.DEBUG) # Set root logger to DEBUG to capture all levels
        root.addHandler(console_handler)
        root.addHandler(file_handler)
        
        # Suppress HTTP request logs in non-verbose mode
        if not verbose:
            # Suppress OpenAI HTTP logs
            logging.getLogger("openai").setLevel(logging.WARNING)
            logging.getLogger("httpx").setLevel(logging.WARNING)
            logging.getLogger("httpcore").setLevel(logging.WARNING)
            # Suppress low-level HTTP trace logs (send_request_headers, receive_response_headers, etc.)
            logging.getLogger("httpcore.http11").setLevel(logging.WARNING)
            logging.getLogger("httpcore.connection").setLevel(logging.WARNING)
            logging.getLogger("httpcore.proxy").setLevel(logging.WARNING)
            # Suppress other noisy loggers
            logging.getLogger("urllib3").setLevel(logging.WARNING)
            logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)
            # Suppress Gemini AFC logs - try multiple logger names
            logging.getLogger("google").setLevel(logging.ERROR)
            logging.getLogger("google.genai").setLevel(logging.ERROR)
            logging.getLogger("genai").setLevel(logging.ERROR)
            logging.getLogger("google.ai").setLevel(logging.ERROR)
            logging.getLogger("google.auth").setLevel(logging.WARNING)
            logging.getLogger("google.auth.transport").setLevel(logging.WARNING)
            # Suppress any other API noise
            logging.getLogger("asyncio").setLevel(logging.WARNING)
            # Suppress requests library if used
            logging.getLogger("requests").setLevel(logging.WARNING)
            logging.getLogger("requests.packages.urllib3").setLevel(logging.WARNING)
        else:
            # In verbose mode, still suppress the most verbose HTTP trace logs
            # but allow INFO level for httpx to see request summaries
            logging.getLogger("httpcore.http11").setLevel(logging.INFO)
            logging.getLogger("httpcore.connection").setLevel(logging.INFO)
            logging.getLogger("httpcore.proxy").setLevel(logging.INFO)

        # Log setup info only if verbose mode is enabled
        if verbose:
            logging.info(f"✅ SQLite Enricher started with verbose={verbose}")
            logging.info(f"📁 Log directory: {log_dir}")
            logging.info(f"📄 Main log file: {log_file}")
            logging.info(f"🔍 Debug info: {debug_file}")
        else:
            # Just log to file for non-verbose mode
            logging.debug(f"SQLite Enricher started with verbose={verbose}")
            logging.debug(f"Log directory: {log_dir}")
            logging.debug(f"Main log file: {log_file}")
            logging.debug(f"Debug info: {debug_file}")

        # Log important paths for troubleshooting
        logging.debug(f"Current working directory: {os.getcwd()}")
        if 'script_dir' in locals():
             logging.debug(f"Script directory: {script_dir}")

        return str(log_file), str(debug_file)

    except Exception as e:
        # Fallback logging if file setup fails
        logging.basicConfig(level=level, format='%(message)s')
        logging.error(f"Error setting up file logging: {e}. Logging to console only.")
        return None, None

def parse_input_cols(input_cols: List[str]) -> List[Tuple[str, Optional[slice]]]:
    """
    Legacy function for backwards compatibility.
    Parse input columns with slice notation like ["content[0:100]"].
    """
    parsed_cols = []
    for col in input_cols:
        if '[' in col and ']' in col:
            col_name, slice_str = col.split('[')
            slice_str = slice_str.rstrip(']')
            start, end = map(lambda x: int(x) if x else None, slice_str.split(':'))
            parsed_cols.append((col_name, slice(start, end)))
        else:
            parsed_cols.append((col, None))
    return parsed_cols

def parse_input_columns_with_limits(input_columns: List[str]) -> List[Tuple[str, Optional[int]]]:
    """
    Parse input columns with character limit notation.
    
    Examples:
        ["content:500", "title"] -> [("content", 500), ("title", None)]
        ["raw_content:300", "filename", "metadata:100"] -> [("raw_content", 300), ("filename", None), ("metadata", 100)]
    
    Returns:
        List of tuples: (column_name, character_limit)
        character_limit is None if no limit specified
    """
    parsed_columns = []
    
    for col_spec in input_columns:
        if ':' in col_spec:
            # Split on the first colon only
            parts = col_spec.split(':', 1)
            if len(parts) == 2:
                col_name, limit_str = parts
                try:
                    char_limit = int(limit_str)
                    if char_limit <= 0:
                        raise ValueError(f"Character limit must be positive, got {char_limit}")
                    parsed_columns.append((col_name.strip(), char_limit))
                except ValueError as e:
                    logging.warning(f"Invalid character limit in '{col_spec}': {e}. Using full column.")
                    parsed_columns.append((col_name.strip(), None))
            else:
                # Malformed, treat as column name without limit
                parsed_columns.append((col_spec.strip(), None))
        else:
            # No limit specified
            parsed_columns.append((col_spec.strip(), None))
    
    return parsed_columns

def apply_column_limits(row_data: Dict, parsed_columns: List[Tuple[str, Optional[int]]]) -> Dict[str, str]:
    """
    Apply character limits to row data based on parsed column specifications.
    Handles both table.column syntax and plain column names.
    
    Args:
        row_data: Dictionary containing row data from database
        parsed_columns: List of (column_name, character_limit) tuples
    
    Returns:
        Dictionary with column names as keys and (potentially truncated) values as strings
    """
    result = {}
    
    for col_name, char_limit in parsed_columns:
        # Try to find the column value
        value = None
        found_key = None
        
        # First try exact match
        if col_name in row_data:
            value = row_data[col_name]
            found_key = col_name
        # If not found and contains table prefix, try without prefix
        elif '.' in col_name:
            _, column_only = col_name.split('.', 1)
            if column_only in row_data:
                value = row_data[column_only]
                found_key = column_only
                logging.debug(f"Found column '{col_name}' as '{column_only}' in row data")
        
        if found_key is not None:
            # Convert to string if not already
            if value is None:
                str_value = ""
            else:
                str_value = str(value)
            
            # Apply character limit if specified
            if char_limit is not None and len(str_value) > char_limit:
                truncated_value = str_value[:char_limit]
                logging.debug(f"Truncated column '{col_name}' from {len(str_value)} to {char_limit} characters")
                result[col_name] = truncated_value
            else:
                result[col_name] = str_value
        else:
            logging.warning(f"Column '{col_name}' not found in row data. Available columns: {list(row_data.keys())}")
            result[col_name] = ""
    
    return result


def detect_mojibake(text: str, threshold: float = 0.15) -> bool:
    """
    Detect if text contains mojibake (garbled text from encoding issues).
    
    Common mojibake patterns include:
    - Ã©, Ã¡, Ã³ (UTF-8 interpreted as Latin-1)
    - â€™, â€œ (smart quotes misinterpreted)
    - Ã¢â‚¬ (multiple encoding errors)
    - Â characters (often from double encoding)
    
    Args:
        text: Text to check for mojibake
        threshold: Ratio of suspicious characters to total characters to trigger detection
        
    Returns:
        True if mojibake is likely present
    """
    if not text or len(text) < 10:
        return False
    
    # Common mojibake patterns
    mojibake_patterns = [
        # UTF-8 as Latin-1 patterns - using explicit character matches instead of ranges
        r'Ã[¡¢£¤¥¦§¨©ª«¬­®¯°±²³´µ¶·¸¹º»¼½¾¿]',  # Ã followed by extended ASCII
        r'Â[\x80-\xBF]',  # Â followed by bytes 0x80-0xBF
        r'â€[™œ"]',  # Smart quotes mojibake
        r'â€¦',  # Ellipsis mojibake
        r'â€"',  # Em dash mojibake
        r'Ã¢â‚¬',  # Common triple encoding error
        r'Ã‚Â',  # Double encoding marker
        r'Ã¢â€',  # Another common pattern
        r'Ã¯Â»Â¿',  # BOM mojibake
        r'â€¹',  # Single quote mojibake
        r'â€º',  # Single quote mojibake
        r'Ã¢â‚¬Â',  # Multiple encoding errors
        r'ÃƒÂ',  # Another double encoding pattern
        r'Ã¢â‚¬â„¢',  # Apostrophe mojibake
        r'Ã¢â‚¬Å"',  # Left double quote mojibake
        r'Ã¢â‚¬\x9d',  # Right double quote mojibake
        r'Ã‚Â§',  # Section sign mojibake
        r'Ã‚Â©',  # Copyright mojibake
        r'Ã‚Â®',  # Registered trademark mojibake
    ]
    
    # Count mojibake occurrences
    mojibake_count = 0
    for pattern in mojibake_patterns:
        matches = re.findall(pattern, text)
        mojibake_count += len(matches)
    
    # Also check for high density of non-ASCII characters in supposedly English text
    non_ascii_count = sum(1 for char in text if ord(char) > 127)
    total_chars = len(text)
    
    # Calculate ratios
    mojibake_ratio = mojibake_count / total_chars if total_chars > 0 else 0
    non_ascii_ratio = non_ascii_count / total_chars if total_chars > 0 else 0
    
    # Detect if there's likely mojibake
    has_mojibake = mojibake_ratio > threshold or (non_ascii_ratio > 0.3 and mojibake_count > 5)
    
    if has_mojibake:
        logging.debug(f"Mojibake detected: {mojibake_count} patterns found, "
                     f"mojibake ratio: {mojibake_ratio:.2%}, "
                     f"non-ASCII ratio: {non_ascii_ratio:.2%}")
    
    return has_mojibake


def try_fix_mojibake(text: str) -> str:
    """
    Attempt to fix common mojibake patterns.
    
    This function tries to fix text that was incorrectly decoded,
    typically UTF-8 text that was interpreted as Latin-1.
    
    Args:
        text: Text potentially containing mojibake
        
    Returns:
        Fixed text if successful, original text if fix fails
    """
    if not text:
        return text
    
    try:
        # Most common case: UTF-8 interpreted as Latin-1
        # Encode back to Latin-1 bytes, then decode as UTF-8
        fixed = text.encode('latin-1').decode('utf-8')
        
        # Verify the fix worked by checking if mojibake is reduced
        if detect_mojibake(fixed) < detect_mojibake(text):
            logging.info("Successfully fixed mojibake in text")
            return fixed
        else:
            return text
    except (UnicodeDecodeError, UnicodeEncodeError):
        # If the conversion fails, try other common patterns
        try:
            # Windows-1252 interpreted as Latin-1
            fixed = text.encode('windows-1252').decode('utf-8') 
            if detect_mojibake(fixed) < detect_mojibake(text):
                logging.info("Successfully fixed mojibake using Windows-1252 conversion")
                return fixed
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
    
    # If all fixes fail, return original
    return text