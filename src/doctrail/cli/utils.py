"""
Shared utilities and imports for CLI commands.
"""
import sys
import logging
import os
import sqlite3
from pathlib import Path
import tempfile
import platform
import socket
import re
from importlib.metadata import PackageNotFoundError, version

import click
import yaml
import asyncio
from typing import Any, List, Optional, Dict
from tqdm import tqdm
import threading
import time
import json
from datetime import datetime

# Re-export commonly used items
from ..constants import (
    SPINNER_CHARS, ERROR_NO_ENRICHMENTS, ERROR_NO_DATABASE,
    ERROR_ENRICHMENT_NOT_FOUND, DEFAULT_TABLE_NAME, DEFAULT_MODEL,
    LOG_FILE_PATH, SUCCESS_ENRICHMENT
)
from ..db_operations import (
    get_db_connection, execute_query, execute_query_optimized
)
from ..llm_operations import process_enrichment
from ..core_utils import load_pydantic_model, parse_input_cols, load_config
from ..utils.logging_config import setup_logging
from ..utils.dependency_check import verify_dependencies
from ..utils.cost_estimation import (
    estimate_enrichment_cost, format_cost_estimate, should_confirm_cost,
    validate_model, get_supported_models
)
from ..utils.progress import create_progress_bar, SpinnerTqdm

CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])
try:
    __version__ = version("doctrail")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"


# =============================================================================
# Preset Enrichments (loaded from src/doctrail/presets/*.yml)
# =============================================================================

def _get_presets_dir() -> Path:
    """Get the presets directory from the package."""
    package_dir = Path(__file__).parent.parent  # Go up from cli/ to doctrail/
    presets_dir = package_dir / "presets"
    if presets_dir.exists():
        return presets_dir
    try:
        import importlib.resources as pkg_resources
        with pkg_resources.files('doctrail.presets') as p:
            return Path(p)
    except Exception:
        return presets_dir


def _load_presets() -> dict:
    """Load all preset enrichments from YAML files."""
    presets = {}
    presets_dir = _get_presets_dir()

    if not presets_dir.exists():
        return presets

    for yml_file in presets_dir.glob("*.yml"):
        try:
            with open(yml_file) as f:
                content = f.read()
                data = yaml.safe_load(content)
                name = data.get('name', yml_file.stem)
                presets[name] = {
                    "name": name,
                    "description": data.get('description', ''),
                    "filename": yml_file.name,
                    "content": content,
                }
        except Exception as e:
            logging.warning(f"Failed to load preset {yml_file}: {e}")

    return presets


# Load presets at module import time
PRESET_ENRICHMENTS = _load_presets()

# Aliases for British/Australian English and common variations
PRESET_ALIASES = {
    'summarise': 'summarize',
    'summarization': 'summarize',
    'summary': 'summarize',
    'lang': 'language',
    'entities': 'extract_entities',
    'ner': 'extract_entities',
    'type': 'document_type',
    'doctype': 'document_type',
    'methods': 'research_methods',
}


def _resolve_preset_alias(name: str) -> str:
    """Resolve preset aliases (e.g., 'summarise' -> 'summarize')."""
    return PRESET_ALIASES.get(name.lower(), name)


# =============================================================================
# Project Config Helpers
# =============================================================================

def _get_doctrail_dir() -> Path:
    """Get the .doctrail directory in current working directory."""
    return Path.cwd() / ".doctrail"


def _get_config_path() -> Path:
    """Get the config.yml path."""
    return _get_doctrail_dir() / "config.yml"


def _load_project_config() -> dict:
    """Load config from .doctrail/config.yml in current directory."""
    config_path = _get_config_path()
    if not config_path.exists():
        raise click.UsageError(
            "No doctrail project found in current directory.\n"
            "Run 'doctrail init' first to set up your project."
        )
    return load_config(str(config_path))


def _strip_runtime_config(config_data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a YAML-serializable config without loader/runtime helper objects."""
    return {
        key: value
        for key, value in config_data.items()
        if not key.startswith("_")
    }


def _write_merged_config(config_data: Dict[str, Any], *, base_config_path: str) -> str:
    """Write a temporary config next to the source config so relative paths still work."""
    config_dir = Path(base_config_path).expanduser().resolve().parent
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False, dir=config_dir) as handle:
        yaml.safe_dump(_strip_runtime_config(config_data), handle, sort_keys=False)
        return handle.name


def _exit_error(message: str) -> None:
    """Print an error message and force a non-zero Click exit."""
    click.echo(message, err=True)
    raise click.exceptions.Exit(1)
