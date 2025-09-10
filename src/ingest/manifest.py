"""
Manifest handling for document ingestion.

This module provides functionality to load and process manifest files
that contain metadata to be associated with documents during ingestion.
"""

import os
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)


def load_manifest(manifest_path: str) -> Dict[str, Dict[str, Any]]:
    """Load manifest file and return a filename->metadata mapping.
    
    Args:
        manifest_path: Path to manifest.json file
        
    Returns:
        Dictionary mapping filenames to their metadata
        
    Raises:
        FileNotFoundError: If manifest file doesn't exist
        json.JSONDecodeError: If manifest is not valid JSON
        ValueError: If manifest structure is invalid
    """
    manifest_path = Path(manifest_path).resolve()
    
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")
    
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest_data = json.load(f)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Invalid JSON in manifest file: {e}",
            e.doc,
            e.pos
        )
    
    # Validate manifest structure
    if not isinstance(manifest_data, dict):
        raise ValueError("Manifest must be a JSON object with filenames as keys")
    
    # Validate each entry
    for filename, metadata in manifest_data.items():
        if not isinstance(filename, str):
            raise ValueError(f"Manifest keys must be strings (filenames), got: {type(filename)}")
        
        if not isinstance(metadata, dict):
            raise ValueError(
                f"Manifest values must be objects (metadata), got {type(metadata)} for file: {filename}"
            )
        
        # Ensure all metadata values are simple types
        for key, value in metadata.items():
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise ValueError(
                    f"Metadata values must be simple types (string, number, boolean, null). "
                    f"Got {type(value)} for key '{key}' in file '{filename}'"
                )
    
    logger.info(f"Loaded manifest with metadata for {len(manifest_data)} files")
    return manifest_data


def get_file_metadata(
    file_path: str,
    manifest_data: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """Get metadata for a specific file from manifest.
    
    Args:
        file_path: Path to the file being processed
        manifest_data: Loaded manifest data
        
    Returns:
        Metadata dictionary for the file (empty dict if not found)
    """
    filename = os.path.basename(file_path)
    metadata = manifest_data.get(filename, {})
    
    if metadata:
        logger.debug(f"Found manifest metadata for {filename}: {len(metadata)} fields")
    
    return metadata


def find_manifest_in_directory(directory: str) -> Optional[str]:
    """Look for manifest.json in a directory.
    
    Args:
        directory: Directory to search in
        
    Returns:
        Path to manifest.json if found, None otherwise
    """
    manifest_path = Path(directory) / "manifest.json"
    
    if manifest_path.exists():
        logger.info(f"Found manifest.json in {directory}")
        return str(manifest_path)
    
    return None