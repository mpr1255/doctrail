"""
File utilities for document ingestion.

This module contains functions for file filtering, pattern matching,
and file system operations.
"""

import re
import fnmatch
import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def should_skip_file(file_path: str) -> bool:
    """
    Check if a file should be skipped (log files, hidden files, system files)
    Returns True if file should be skipped
    """
    path = Path(file_path)
    
    # Skip hidden files (starting with .)
    if path.name.startswith('.'):
        logger.info(f"Skipping hidden file: {file_path}")
        return True
    
    # Skip system files specific to different platforms
    system_files = ['.DS_Store', 'Thumbs.db', 'desktop.ini']
    if path.name in system_files:
        logger.info(f"Skipping system file: {file_path}")
        return True
    
    # Skip log files by extension
    if path.suffix.lower() in ['.log', '.txt']:
        # Additional check - if content starts with a timestamp or contains 'INFO', 'DEBUG', 'ERROR'
        try:
            with open(file_path, 'r', errors='ignore') as f:
                first_few_lines = ''.join([f.readline() for _ in range(5)])
                if (re.search(r'\d{4}-\d{2}-\d{2}', first_few_lines) and 
                    any(log_level in first_few_lines for log_level in ['INFO', 'DEBUG', 'ERROR', 'WARNING'])):
                    logger.info(f"Skipping log file based on content: {file_path}")
                    return True
        except Exception:
            pass
    
    # Skip files with 'log' in the name
    if 'log' in path.stem.lower():
        logger.info(f"Skipping log file based on name: {file_path}")
        return True
        
    return False


def is_log_file(file_path: str) -> bool:
    """Legacy function that calls should_skip_file"""
    return should_skip_file(file_path)


def apply_file_patterns(files: List[Path], include_pattern: Optional[str] = None, exclude_pattern: Optional[str] = None) -> List[Path]:
    """
    Apply include/exclude glob patterns to filter files.
    
    Args:
        files: List of Path objects to filter
        include_pattern: Glob pattern - only files matching this pattern will be included
        exclude_pattern: Glob pattern(s) - files matching these patterns will be excluded
                        Can be comma-separated for multiple patterns (e.g., "*pristine*,*.json")
    
    Returns:
        Filtered list of Path objects
    """
    filtered_files = files.copy()
    
    # Apply include pattern first
    if include_pattern:
        filtered_files = [f for f in filtered_files if fnmatch.fnmatch(f.name, include_pattern)]
        logger.info(f"Include pattern '{include_pattern}' matched {len(filtered_files)} files")
    
    # Apply exclude patterns
    if exclude_pattern:
        # Support comma-separated patterns
        exclude_patterns = [p.strip() for p in exclude_pattern.split(',')]
        original_count = len(filtered_files)
        
        for pattern in exclude_patterns:
            filtered_files = [f for f in filtered_files if not fnmatch.fnmatch(f.name, pattern)]
        
        excluded_count = original_count - len(filtered_files)
        logger.info(f"Exclude pattern(s) '{exclude_pattern}' removed {excluded_count} files")
    
    return filtered_files


def check_for_manual_override(file_path: str) -> Optional[str]:
    """
    Check if there's a manual override text file for this document.
    
    Looking for files like:
    - original_file.pdf -> original_file.txt
    - original_file.doc -> original_file.txt
    
    Returns the content of the override file if found, None otherwise.
    """
    path = Path(file_path)
    override_path = path.with_suffix('.txt')
    
    if override_path.exists() and override_path != path:
        try:
            with open(override_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    logger.info(f"Found manual override for {file_path}: {override_path}")
                    return content
        except Exception as e:
            logger.warning(f"Could not read manual override file {override_path}: {e}")
    
    return None