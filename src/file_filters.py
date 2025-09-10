"""File filtering utilities for ingestion."""

import re
import fnmatch
import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def should_skip_file(file_path: str) -> bool:
    """
    Check if a file should be skipped (log files, hidden files, system files, unsupported types)
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
    
    # Skip sync-related files and folders (Dropbox, OneDrive, etc.)
    sync_patterns = [
        '.sync',           # Resilio Sync directories
        'IgnoreList',      # Sync ignore files  
        'StreamsList',     # Sync stream files
        'FolderType',      # Sync folder type files
        '.dropbox',        # Dropbox files
        '.onedrive',       # OneDrive files
        'Icon\r',          # macOS folder icons
        'conflict'         # Sync conflict files
    ]
    
    # Check if filename contains any sync patterns
    for pattern in sync_patterns:
        if pattern in path.name:
            logger.info(f"Skipping sync-related file: {file_path}")
            return True
    
    # Check if any parent directory is a sync directory
    for parent in path.parents:
        if parent.name.startswith('.sync'):
            logger.info(f"Skipping file in sync directory: {file_path}")
            return True
    
    # Skip unsupported file types (video, audio, archives, etc.)
    unsupported_extensions = [
        # Video
        '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg',
        # Audio
        '.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a', '.opus',
        # Archives
        '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.iso',
        # Images (optional - uncomment if you want to skip)
        # '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg', '.ico', '.webp',
        # Other
        '.exe', '.dll', '.so', '.dylib', '.apk', '.deb', '.rpm'
    ]
    
    if path.suffix.lower() in unsupported_extensions:
        logger.info(f"Skipping unsupported file type: {file_path} ({path.suffix})")
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
    
    # Skip files that are likely log files (more specific patterns)
    stem_lower = path.stem.lower()
    log_patterns = ['.log', '_log', '-log', 'logfile', 'error.log', 'debug.log', 'access.log']
    if any(pattern in stem_lower for pattern in log_patterns) or stem_lower.endswith('log'):
        logger.info(f"Skipping log file based on name: {file_path}")
        return True
        
    return False


# Keep old function name for backward compatibility
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


def get_unsupported_file_error(file_path: str) -> str:
    """
    Generate appropriate error message for unsupported file types.
    """
    file_ext = Path(file_path).suffix.lower()
    
    # Map file extensions to helpful error messages
    error_messages = {
        '.epub': 'EPUB files are not supported. Please convert to PDF or TXT first.',
        '.mobi': 'MOBI files are not supported. Please convert to PDF or TXT first.',
        '.azw': 'AZW files are not supported. Please convert to PDF or TXT first.',
        '.azw3': 'AZW3 files are not supported. Please convert to PDF or TXT first.',
        '.ppt': 'PowerPoint files are not supported. Please export as PDF first.',
        '.pptx': 'PowerPoint files are not supported. Please export as PDF first.',
        '.xls': 'Excel files are not supported. Please export as PDF or CSV first.',
        '.xlsx': 'Excel files are not supported. Please export as PDF or CSV first.',
        '.rtf': 'RTF files are not supported. Please convert to PDF or TXT first.',
        '.odt': 'OpenDocument files are not supported. Please export as PDF first.',
        '.ods': 'OpenDocument files are not supported. Please export as PDF or CSV first.',
        '.odp': 'OpenDocument files are not supported. Please export as PDF first.',
        '.pages': 'Pages files are not supported. Please export as PDF first.',
        '.numbers': 'Numbers files are not supported. Please export as PDF or CSV first.',
        '.key': 'Keynote files are not supported. Please export as PDF first.',
    }
    
    if file_ext in error_messages:
        return error_messages[file_ext]
    else:
        return f'Doctrail does not currently support {file_ext} files.'


def check_for_manual_override(file_path: str) -> Optional[str]:
    """
    Check for manual override files that provide better content than automatic extraction.
    
    Looks for files with these suffixes in the same directory:
    - filename--good.txt: High-quality manual transcription 
    - filename--ocr.txt: OCR-processed version
    - filename--manual.txt: Any manually created version
    
    Returns the path to the override file if found, None otherwise.
    """
    base_path = Path(file_path)
    base_name = base_path.stem
    parent_dir = base_path.parent
    
    # Check for override files in priority order
    override_suffixes = ['--good.txt', '--ocr.txt', '--manual.txt']
    
    for suffix in override_suffixes:
        override_path = parent_dir / f"{base_name}{suffix}"
        if override_path.exists():
            logger.info(f"Found manual override file: {override_path}")
            return str(override_path)
    
    return None