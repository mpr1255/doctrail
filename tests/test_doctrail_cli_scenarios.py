#!/usr/bin/env python3
"""Split doctrail tests."""

import pytest
import shutil
import yaml
import sqlite3
import asyncio
import csv
import json
from datetime import datetime
from click.testing import CliRunner
from pathlib import Path
import logging
import sys
import os
from types import SimpleNamespace
from typing import Optional, get_args

sys.path.insert(0, str(Path(__file__).parent.parent))

from doctrail.main import cli
import sqlite_utils
from doctrail.llm_providers.anthropic_provider import AnthropicProvider
from doctrail.llm_providers.gemini_provider import GeminiProvider
from doctrail.llm_providers.openai_provider import OpenAIProvider
from doctrail.utils.model_pricing import get_openai_batch_model_info
from tests.doctrail_support import *


@pytest.mark.parametrize(
    "config_file",
    discover_test_configs(),
    ids=_format_test_id
)
def test_cli_command(config_file, temp_env, caplog):
    """
    Main test function that runs once for each YAML config file.
    Tests the actual CLI commands with mocked external services.
    """
    runner = CliRunner()
    
    # Set up logging to capture test output
    caplog.set_level(logging.INFO)
    
    # Load the test configuration
    # Import the custom loader to handle !import tags
    import importlib
    utils_module = importlib.import_module('doctrail.core_utils')
    load_config = utils_module.load_config
    
    # Use our custom loader that handles imports
    config = load_config(str(config_file))
    
    # Provide high-level scenario summary for easier test triage
    print(_summarize_config(config, config_file))

    # Prepare temporary config file with correct paths
    temp_config_path = temp_env["temp_dir"] / "test_config.yml"
    
    # Update paths in config to point to temp environment
    if "database" in config:
        config["database"] = str(temp_env["db_path"])
    
    # Handle ingestion phase if specified
    if "ingest" in config:
        ingest_config = config["ingest"]
        
        # Determine source directory
        source_dir = ingest_config.get("source", "tests/assets/files")
        if not Path(source_dir).is_absolute():
            # Make path relative to project root
            source_dir = Path.cwd() / source_dir
        
        # Build ingest command
        ingest_args = [
            "--skip-requirements",
            "ingest",
            "--db-path", str(temp_env["db_path"]),
            "--input-dir", str(source_dir),
            "--table", ingest_config.get("table", "documents"),
            "--yes"  # Skip confirmation
        ]
        
        # Add optional parameters
        if ingest_config.get("include_pattern"):
            ingest_args.extend(["--include-pattern", ingest_config["include_pattern"]])
        if ingest_config.get("exclude_pattern"):
            ingest_args.extend(["--exclude-pattern", ingest_config["exclude_pattern"]])
        if ingest_config.get("limit"):
            ingest_args.extend(["--limit", str(ingest_config["limit"])])
        if ingest_config.get("readability"):
            ingest_args.append("--readability")
        if ingest_config.get("fulltext"):
            ingest_args.append("--fulltext")
        
        # Run ingest command
        result = runner.invoke(cli, ingest_args)
        
        # Check command succeeded
        if result.exit_code != 0:
            print(f"Ingest output:\n{result.output}")
            if result.exception:
                print(f"Exception:\n{result.exception}")
        assert result.exit_code == 0, f"Ingest failed:\n{result.output}\n{result.exception}"
    
    # Determine test type and run appropriate command
    if "enrichments" in config:
        # This is an enrichment test
        with open(temp_config_path, "w") as f:
            yaml.dump(config, f)
        
        # Get enrichment names
        enrichment_names = [e["name"] for e in config["enrichments"]]
        
        # Run enrichment command
        # Build the command with multiple --enrichments flags
        cmd_args = [
            "--skip-requirements",  # Global flag goes before command
            "enrich",
            "--config", str(temp_config_path),
        ]
        
        # Add each enrichment as a separate --enrichments flag
        for enrichment_name in enrichment_names:
            cmd_args.extend(["--enrichments", enrichment_name])

        cmd_args.append("--overwrite")
        cmd_args.append("--allow-column-collision")

        result = runner.invoke(cli, cmd_args)
        
        # Check command succeeded
        if result.exit_code != 0:
            print(f"Command output:\n{result.output}")
            if result.exception:
                print(f"Exception:\n{result.exception}")
        assert result.exit_code == 0, f"Enrichment failed:\n{result.output}\n{result.exception}"
        
        # Verify enrichment results in database
        verify_enrichment_results(temp_env["db_path"], config)
        
    elif config.get("_test_type") == "ingest":
        # This is an ingest test
        table_name = config.get("table", "documents")
        
        # Run ingest command
        result = runner.invoke(cli, [
            "--skip-requirements",  # Global flag goes before command
            "ingest",
            "--db-path", str(temp_env["db_path"]),
            "--input-dir", str(temp_env["docs_path"]),
            "--table", table_name,
            "--yes"  # Skip confirmation
        ])
        
        # Check command succeeded
        assert result.exit_code == 0, f"Ingest failed:\n{result.output}\n{result.exception}"
        
        # Verify files were ingested
        db = sqlite_utils.Database(temp_env["db_path"])
        assert table_name in db.table_names()
        assert db[table_name].count > 0
        
    elif config.get("_test_type") == "export":
        # This is an export test
        export_type = config["export_type"]
        output_dir = temp_env["temp_dir"] / "exports"
        
        with open(temp_config_path, "w") as f:
            yaml.dump(config, f)
        
        # Run export command
        result = runner.invoke(cli, [
            "export",
            "--config", str(temp_config_path),
            "--export-type", export_type,
            "--output-dir", str(output_dir)
        ])
        
        # Check command succeeded
        assert result.exit_code == 0, f"Export failed:\n{result.output}\n{result.exception}"
        
        # Verify export created files
        assert output_dir.exists()
        assert len(list(output_dir.iterdir())) > 0
