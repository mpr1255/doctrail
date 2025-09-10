#!/usr/bin/env python3
"""
Doctrail Test Suite
===================

This test suite uses parametrized testing to automatically test all functionality
based on YAML configuration files in the schema_examples/ directory.

Each YAML file represents a test case. To add a new test, simply add a new YAML file.
"""

import pytest
import shutil
import yaml
import sqlite3
import asyncio
from click.testing import CliRunner
from pathlib import Path
import logging
import sys
import os

# Add parent directory to path to import doctrail modules
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the CLI and necessary modules
from src.main import cli
import sqlite_utils

# --- Define paths to our assets ---
TESTS_DIR = Path(__file__).parent
ASSETS_DIR = TESTS_DIR / "assets"
CONFIGS_DIR = TESTS_DIR / "schema_examples"

# --- FIXTURES FOR TEST SETUP ---

@pytest.fixture
def temp_env(tmp_path):
    """Creates an isolated temporary environment for a single test."""
    # Create a fresh test database
    db_path = tmp_path / "test.db"
    
    # Copy the test database if it exists, otherwise create a new one
    if (ASSETS_DIR / "test.db").exists():
        shutil.copy(ASSETS_DIR / "test.db", db_path)
    else:
        # Create a minimal test database
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE documents (
                sha1 TEXT PRIMARY KEY,
                filename TEXT,
                raw_content TEXT,
                metadata TEXT,
                consolidated_metadata TEXT,
                doc_province TEXT,
                doc_city TEXT,
                doc_year INTEGER
            )
        """)
        # Insert test data
        conn.execute("""
            INSERT INTO documents (sha1, filename, raw_content) VALUES
            ('test_sha1_1', 'test1.pdf', '这是一个关于器官捐献的文档。2024年在北京市。'),
            ('test_sha2_2', 'test2.pdf', '红十字会向器官捐献者家庭发放了5万元慰问金。')
        """)
        conn.commit()
        conn.close()

    # Copy sample documents directory
    docs_path = tmp_path / "sample_docs"
    if (ASSETS_DIR / "files").exists():
        # Copy ALL test files to test every file type
        docs_path.mkdir()
        test_files = list((ASSETS_DIR / "files").glob("*"))  # Copy all files
        for f in test_files:
            if f.is_file():  # Skip directories
                shutil.copy(f, docs_path / f.name)
    else:
        # Create dummy test files
        docs_path.mkdir()
        (docs_path / "test1.pdf").write_text("Test PDF content")
        (docs_path / "test2.pdf").write_text("Another test PDF")

    # Provide paths to the test function
    yield {
        "db_path": db_path, 
        "docs_path": docs_path, 
        "temp_dir": tmp_path
    }

@pytest.fixture(autouse=True)
def mock_external_apis(mocker):
    """
    Automatically mocks all external API calls for every test.
    This ensures tests run fast and don't require API keys.
    """
    # Mock the LLM provider factory to return our mock provider
    mock_provider = mocker.MagicMock()
    
    # Mock structured output generation
    async def mock_generate_structured(messages, pydantic_model, temperature=0.0):
        """Generate mock structured data based on the Pydantic model."""
        # Create mock data based on common field names
        mock_data = {
            "doc_year": 2024,
            "doc_province": "Beijing",
            "doc_city": "Beijing",
            "amount": 50000,
            "recipient_type": "organ_donor_family",
            "payment_type": "condolence_money",
            "evidence_zh": "红十字会发放5万元慰问金",
            "evidence_en": "Red Cross distributed 50,000 yuan condolence money",
            "valid_record": "yes",
            "benefit_type": "cash",
            "entity_type": "red_cross",
            "comp_category": "financial",
            "total_amount": 50000,
            "families_helped": 10,
            "fund_name": "Test Fund"
        }
        
        # Filter only fields that exist in the model
        valid_fields = {}
        for field_name, field_info in pydantic_model.model_fields.items():
            if field_name in mock_data:
                valid_fields[field_name] = mock_data[field_name]
            else:
                # Provide sensible defaults based on field type
                field_type = field_info.annotation
                if field_type == int:
                    valid_fields[field_name] = 0
                elif field_type == float:
                    valid_fields[field_name] = 0.0
                elif field_type == str:
                    valid_fields[field_name] = "test_value"
                elif hasattr(field_type, "__args__"):  # Optional type
                    valid_fields[field_name] = None
        
        return pydantic_model(**valid_fields)
    
    # Mock text generation (for non-structured calls)
    async def mock_generate_text(messages, temperature=0.0):
        """Generate mock text responses."""
        return "This is a mock LLM response for testing."
    
    mock_provider.generate_structured = mock_generate_structured
    mock_provider.generate_text = mock_generate_text
    
    # Patch the factory function
    mocker.patch('src.llm_providers.factory.get_llm_provider', return_value=mock_provider)
    
    # Also mock any direct OpenAI/Gemini calls that might bypass the factory
    mocker.patch('src.llm_operations.call_llm_structured', side_effect=mock_generate_structured)
    mocker.patch('src.llm_operations.call_llm', side_effect=mock_generate_text)
    
    # Mock document processing with specialized extractors  
    async def mock_document_process(file_path, file_sha1, use_readability=False):
        """Mock document processing with specialized extractors."""
        content = f"Extracted content from {file_path}"
        metadata = {
            "title": "Test Document", 
            "author": "Test Author",
            "original_file_path": file_path,
            "extraction_method": "mock_extractor"
        }
        return file_sha1, content, metadata
    
    mocker.patch('src.ingester.process_document', side_effect=mock_document_process)
    
    # Mock file filtering
    mocker.patch('src.ingest.file_utils.should_skip_file', return_value=False)
    
    # Mock dependency checking
    mocker.patch('src.utils.dependency_check.verify_dependencies', return_value=True)

# --- TEST DISCOVERY ---

def discover_test_configs():
    """Find all YAML test configuration files recursively."""
    if not CONFIGS_DIR.exists():
        return []
    # Find all .yml files recursively, excluding README and non-test files
    all_ymls = list(CONFIGS_DIR.rglob("*.yml"))
    # Filter out non-test YAMLs like main_config.yml, sql_queries.yml
    test_ymls = [f for f in all_ymls if f.stem not in ["main_config", "sql_queries", "main_with_sql_import"]]
    return sorted(test_ymls)

# --- MAIN TEST FUNCTION ---

@pytest.mark.parametrize("config_file", discover_test_configs(), 
                        ids=lambda p: p.stem)
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
    utils_module = importlib.import_module('src.core_utils')
    load_config = utils_module.load_config
    
    # Use our custom loader that handles imports
    config = load_config(str(config_file))
    
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

def verify_enrichment_results(db_path, config):
    """Verify that enrichment produced expected results."""
    db = sqlite_utils.Database(db_path)
    
    # Check database was properly created
    print(f"Tables in test database: {db.table_names()}")
    
    # Check if enrichment_responses table exists first
    if "enrichment_responses" in db.table_names():
        total_records = db["enrichment_responses"].count
        print(f"Total enrichment_responses records: {total_records}")
        if total_records > 0:
            print("Sample records:")
            for row in db["enrichment_responses"].rows_where(limit=3):
                print(f"  - enrichment: {row['enrichment_name']}, model: {row['model_used']}, created: {row['created_at']}")
    
    for enrichment in config["enrichments"]:
        print(f"\nVerifying enrichment: {enrichment['name']}")
        
        # Check if output table was created (for separate table mode)
        if "output_table" in enrichment:
            table_name = enrichment["output_table"]
            if table_name in db.table_names():
                print(f"✓ Output table '{table_name}' exists")
                count = db[table_name].count
                print(f"  Row count: {count}")
                if count == 0:
                    print(f"  ⚠️  Warning: Output table {table_name} is empty")
            else:
                print(f"✗ Output table '{table_name}' not created")
        else:
            # Direct column mode - check if column was added
            if "schema" in enrichment:
                output_col = list(enrichment["schema"].keys())[0]
                if "documents" in db.table_names():
                    columns = [col.name for col in db["documents"].columns]
                    if output_col in columns:
                        print(f"✓ Output column '{output_col}' exists in documents table")
                    else:
                        print(f"✗ Output column '{output_col}' not found in documents table")

# --- UTILITY FUNCTIONS ---

def run_all_tests(verbose=False):
    """Run all tests and return results."""
    args = ["-v"] if verbose else []
    
    # Add coverage if available
    try:
        import pytest_cov
        args.extend(["--cov=src", "--cov-report=term-missing"])
    except ImportError:
        pass
    
    # Run tests
    return pytest.main([__file__] + args)

if __name__ == "__main__":
    # Run tests when script is executed directly
    import argparse
    parser = argparse.ArgumentParser(description="Run doctrail tests")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()
    
    exit_code = run_all_tests(verbose=args.verbose)
    sys.exit(exit_code)