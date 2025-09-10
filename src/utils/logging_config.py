"""Centralized logging configuration for Doctrail."""

import logging
import os
from pathlib import Path
from typing import Optional, Dict, Any

from ..constants import LOG_FILE_PATH, LOG_FORMAT, LOG_DATE_FORMAT


def setup_logging(verbose: bool = False, log_file: Optional[str] = None) -> None:
    """Configure logging for the entire application.
    
    Args:
        verbose: If True, set DEBUG level for console output
        log_file: Optional custom log file path (defaults to LOG_FILE_PATH)
    """
    # Use provided log file or default
    log_path = log_file or LOG_FILE_PATH
    
    # Create log directory if it doesn't exist
    log_dir = Path(log_path).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    
    # Remove any existing handlers
    root_logger.handlers = []
    
    # File handler - always DEBUG level
    file_handler = logging.FileHandler(log_path, mode='a', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)
    
    # Console handler - respects verbose flag
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console_formatter = logging.Formatter(
        '%(levelname)s: %(message)s' if not verbose else LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    
    # Suppress noisy libraries
    suppress_noisy_loggers()


def suppress_noisy_loggers() -> None:
    """Suppress verbose logging from third-party libraries."""
    # Google/Gemini related
    logging.getLogger('google').setLevel(logging.ERROR)
    logging.getLogger('google.genai').setLevel(logging.ERROR)
    logging.getLogger('google.auth').setLevel(logging.ERROR)
    logging.getLogger('google.auth.transport').setLevel(logging.ERROR)
    logging.getLogger('google.generativeai').setLevel(logging.ERROR)
    
    # gRPC related
    logging.getLogger('grpc').setLevel(logging.ERROR)
    logging.getLogger('grpc._channel').setLevel(logging.ERROR)
    logging.getLogger('grpc._cython').setLevel(logging.ERROR)
    
    # HTTP/URL related
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    
    # Other noisy libraries
    logging.getLogger('filelock').setLevel(logging.WARNING)
    logging.getLogger('tika').setLevel(logging.WARNING)
    logging.getLogger('chardet').setLevel(logging.WARNING)
    
    # Suppress environment variable manipulation warnings
    os.environ['GRPC_VERBOSITY'] = 'ERROR'
    os.environ['GRPC_TRACE'] = ''
    os.environ['GLOG_minloglevel'] = '3'
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
    os.environ['GOOGLE_CLOUD_DISABLE_GRPC_LOGS'] = '1'
    os.environ['GRPC_ENABLE_FORK_SUPPORT'] = '0'
    os.environ['GRPC_GO_LOG_VERBOSITY_LEVEL'] = '99'
    os.environ['GRPC_GO_LOG_SEVERITY_LEVEL'] = 'ERROR'


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the given name.
    
    Args:
        name: Logger name (typically __name__)
        
    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)


def configure_logger(name: str, level: Optional[int] = None, 
                    handlers: Optional[list] = None) -> logging.Logger:
    """Configure a specific logger with custom settings.
    
    Args:
        name: Logger name
        level: Optional logging level
        handlers: Optional list of handlers
        
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    
    if level is not None:
        logger.setLevel(level)
    
    if handlers is not None:
        logger.handlers = []
        for handler in handlers:
            logger.addHandler(handler)
    
    return logger


def log_separator(logger: logging.Logger, char: str = "=", length: int = 50) -> None:
    """Log a separator line for visual clarity.
    
    Args:
        logger: Logger instance to use
        char: Character to use for separator
        length: Length of separator line
    """
    logger.info(char * length)