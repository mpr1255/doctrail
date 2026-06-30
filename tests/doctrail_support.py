#!/usr/bin/env python3
"""Shared doctrail test support helpers."""

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
from enum import Enum
from typing import Optional, get_args, get_origin, Union, List

sys.path.insert(0, str(Path(__file__).parent.parent))

from doctrail.main import cli
import sqlite_utils
from doctrail.llm_providers.anthropic_provider import AnthropicProvider
from doctrail.llm_providers.gemini_provider import GeminiProvider
from doctrail.llm_providers.openai_provider import OpenAIProvider
from doctrail.utils.model_pricing import get_openai_batch_model_info

TESTS_DIR = Path(__file__).parent
ASSETS_DIR = TESTS_DIR / "assets"
CONFIGS_DIR = TESTS_DIR / "schema_examples"


@pytest.fixture
def temp_env(tmp_path):
    """Creates an isolated temporary environment for a single test."""
    # Create a fresh test database
    db_path = tmp_path / "test.db"

    # Copy the test database if it exists, otherwise create a new one.
    if (ASSETS_DIR / "test.db").exists():
        shutil.copy(ASSETS_DIR / "test.db", db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
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
    existing = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    if existing == 0:
        long_content_1 = '这是一个关于器官捐献的文档。2024年在北京市。' * 5
        long_content_2 = '红十字会向器官捐献者家庭发放了5万元慰问金。' * 5
        conn.execute("""
            INSERT INTO documents (sha1, filename, raw_content) VALUES
            (?, 'test1.pdf', ?),
            (?, 'test2.pdf', ?)
        """, ('test_sha1_1', long_content_1, 'test_sha2_2', long_content_2))
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
    async def mock_generate_structured(*args, **kwargs):
        """Generate mock structured data based on the Pydantic model.

        Handles both call signatures:
        - provider.generate_structured(messages, pydantic_model, ...)
        - call_llm_structured(model, messages, pydantic_model, ...)
        """
        # Find the pydantic_model argument (it's a class with model_fields)
        pydantic_model = None
        for arg in args:
            if isinstance(arg, type) and hasattr(arg, 'model_fields'):
                pydantic_model = arg
                break
        if pydantic_model is None:
            pydantic_model = kwargs.get('pydantic_model')
        if pydantic_model is None:
            raise ValueError(f"Could not find pydantic_model in args: {args}")

        def mock_scalar(field_name, field_type):
            origin = get_origin(field_type)
            args = get_args(field_type)

            if origin is Union:
                non_null_args = [arg for arg in args if arg is not type(None)]
                if non_null_args:
                    return mock_scalar(field_name, non_null_args[0])
                return None

            if isinstance(field_type, type) and issubclass(field_type, Enum):
                first = next(iter(field_type))
                return first.value

            if field_type == int:
                return 2024 if "year" in field_name else 1
            if field_type == float:
                return 0.75
            if field_type == bool:
                return True
            if field_type == str:
                if field_name.endswith("_zh") or "chinese" in field_name or "_zh_" in field_name:
                    return "中文证据"
                return "test value"
            if hasattr(field_type, "__args__"):
                non_null_args = [arg for arg in field_type.__args__ if arg is not type(None)]
                if non_null_args:
                    return mock_scalar(field_name, non_null_args[0])
                return None
            return "test value"

        def mock_value(field_name, field_type, raw_value):
            origin = get_origin(field_type)
            args = get_args(field_type)

            if origin in (list, List):
                item_type = args[0] if args else str
                if isinstance(raw_value, list):
                    return raw_value
                return [mock_scalar(field_name, item_type)]

            if raw_value is not None:
                return raw_value

            return mock_scalar(field_name, field_type)

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
            "fund_name": "Test Fund",
            "content_valid": True,
        }

        # Filter only fields that exist in the model
        valid_fields = {}
        for field_name, field_info in pydantic_model.model_fields.items():
            valid_fields[field_name] = mock_value(
                field_name,
                field_info.annotation,
                mock_data.get(field_name),
            )

        result = pydantic_model(**valid_fields)

        # Support return_usage kwarg used by call_llm_structured
        if kwargs.get('return_usage'):
            return result, {'input_tokens': 100, 'output_tokens': 10}
        return result

    # Mock text generation (for non-structured calls)
    async def mock_generate_text(messages, temperature=0.0, **kwargs):
        """Generate mock text responses."""
        return "This is a mock LLM response for testing."
    
    mock_provider.generate_structured = mock_generate_structured
    mock_provider.generate_text = mock_generate_text
    
    # Patch the factory function
    mocker.patch('doctrail.llm_providers.factory.get_llm_provider', return_value=mock_provider)
    
    # Also mock any direct OpenAI/Gemini calls that might bypass the factory
    mocker.patch('doctrail.llm_operations.call_llm_structured', side_effect=mock_generate_structured)
    mocker.patch('doctrail.llm_operations.call_llm', side_effect=mock_generate_text)
    
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
    
    mocker.patch('doctrail.ingester.process_document', side_effect=mock_document_process)
    
    # Mock file filtering
    mocker.patch('doctrail.ingest.file_utils.should_skip_file', return_value=False)
    
    # Mock dependency checking
    mocker.patch('doctrail.utils.dependency_check.verify_dependencies', return_value=True)

    # Scenario tests exercise CLI wiring with mocked LLMs; model catalog
    # validation is covered separately in tests/test_cost_estimation.py.
    for target in [
        'doctrail.utils.cost_estimation.validate_model',
        'doctrail.utils.cost_estimation.get_model_validation_error',
        'doctrail.core_runtime.enrichment.validate_model',
        'doctrail.core_runtime.enrichment.get_model_validation_error',
        'doctrail.core_runtime.batch.validate_model',
        'doctrail.core_runtime.batch.get_model_validation_error',
        'doctrail.core_runtime.shared.validate_model',
        'doctrail.core_runtime.shared.get_model_validation_error',
        'doctrail.core_runtime.commands.validate_model',
        'doctrail.core_runtime.commands.get_model_validation_error',
    ]:
        if target.endswith('validate_model'):
            mocker.patch(target, return_value=True)
        else:
            mocker.patch(target, return_value=None)

class FakeOpenAIBatchBackend:
    """Small in-memory harness for OpenAI batch contract tests."""

    def __init__(self, provider: OpenAIProvider):
        self.provider = provider
        self.uploaded_files: list[dict] = []
        self.batch_records: dict[str, dict] = {}
        self.file_contents: dict[str, str] = {}
        self.retrieve_fail_once: set[str] = set()
        self._wire_provider()

    def _wire_provider(self) -> None:
        async def files_create(file, purpose):
            payload = file.read()
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            file_id = f"file_{len(self.uploaded_files) + 1}"
            lines = [line for line in payload.splitlines() if line.strip()]
            self.uploaded_files.append({
                "id": file_id,
                "purpose": purpose,
                "lines": lines,
            })
            return SimpleNamespace(id=file_id)

        async def files_retrieve_content(file_id):
            if file_id in self.retrieve_fail_once:
                self.retrieve_fail_once.remove(file_id)
                raise RuntimeError(f"temporary failure retrieving {file_id}")
            return self.file_contents.get(file_id, "")

        async def batches_create(completion_window, endpoint, input_file_id, metadata):
            batch_id = f"batch_{len(self.batch_records) + 1}"
            upload = next(item for item in self.uploaded_files if item["id"] == input_file_id)
            self.batch_records[batch_id] = {
                "id": batch_id,
                "status": "validating",
                "input_file_id": input_file_id,
                "output_file_id": None,
                "error_file_id": None,
                "request_counts": SimpleNamespace(total=len(upload["lines"]), completed=0, failed=0),
                "metadata": metadata,
                "completed_at": None,
            }
            return SimpleNamespace(**self.batch_records[batch_id])

        async def batches_retrieve(batch_id):
            return SimpleNamespace(**self.batch_records[batch_id])

        async def batches_cancel(batch_id):
            self.batch_records[batch_id]["status"] = "cancelling"
            return SimpleNamespace(**self.batch_records[batch_id])

        self.provider.client = SimpleNamespace(
            files=SimpleNamespace(
                create=files_create,
                retrieve_content=files_retrieve_content,
            ),
            batches=SimpleNamespace(
                create=batches_create,
                retrieve=batches_retrieve,
                cancel=batches_cancel,
            ),
        )

    def set_batch_result(
        self,
        batch_id: str,
        *,
        status: str,
        output_lines: Optional[list[dict]] = None,
        error_lines: Optional[list[dict]] = None,
        usage: Optional[dict] = None,
    ) -> None:
        """Configure what a completed batch returns."""
        output_lines = output_lines or []
        error_lines = error_lines or []
        output_file_id = None
        error_file_id = None

        if output_lines:
            output_file_id = f"{batch_id}_output"
            self.file_contents[output_file_id] = "\n".join(
                json.dumps(line, ensure_ascii=False) for line in output_lines
            ) + "\n"
        if error_lines:
            error_file_id = f"{batch_id}_error"
            self.file_contents[error_file_id] = "\n".join(
                json.dumps(line, ensure_ascii=False) for line in error_lines
            ) + "\n"

        self.batch_records[batch_id].update({
            "status": status,
            "output_file_id": output_file_id,
            "error_file_id": error_file_id,
            "request_counts": SimpleNamespace(
                total=self.batch_records[batch_id]["request_counts"].total,
                completed=len(output_lines),
                failed=len(error_lines),
            ),
            "usage": usage,
            "completed_at": datetime.now().isoformat(),
        })

class FakeAnthropicBatchBackend:
    """Small in-memory harness for Anthropic batch contract tests."""

    def __init__(self, provider: AnthropicProvider):
        self.provider = provider
        self.submitted_batches: list[dict] = []
        self.batch_records: dict[str, dict] = {}
        self.result_lines: dict[str, list[dict]] = {}
        self._wire_provider()

    def _wire_provider(self) -> None:
        async def batches_create(requests):
            batch_id = f"msgbatch_{len(self.batch_records) + 1}"
            request_list = list(requests)
            self.submitted_batches.append({
                "id": batch_id,
                "requests": request_list,
            })
            self.batch_records[batch_id] = {
                "id": batch_id,
                "processing_status": "in_progress",
                "request_counts": SimpleNamespace(
                    succeeded=0,
                    errored=0,
                    canceled=0,
                    expired=0,
                    processing=len(request_list),
                ),
                "results_url": None,
                "ended_at": None,
            }
            return SimpleNamespace(**self.batch_records[batch_id])

        async def batches_retrieve(batch_id):
            return SimpleNamespace(**self.batch_records[batch_id])

        async def batches_cancel(batch_id):
            self.batch_records[batch_id]["processing_status"] = "canceling"
            return SimpleNamespace(**self.batch_records[batch_id])

        async def batches_results(batch_id):
            async def _iterate():
                for line in self.result_lines.get(batch_id, []):
                    yield line

            class _AsyncDecoder:
                def __aiter__(self_inner):
                    return _iterate()

            return _AsyncDecoder()

        self.provider.client = SimpleNamespace(
            messages=SimpleNamespace(
                batches=SimpleNamespace(
                    create=batches_create,
                    retrieve=batches_retrieve,
                    cancel=batches_cancel,
                    results=batches_results,
                )
            )
        )

    def set_batch_result(
        self,
        batch_id: str,
        *,
        status: str,
        results: Optional[list[dict]] = None,
    ) -> None:
        """Configure what a completed Anthropic batch returns."""
        results = results or []
        counts = {
            "succeeded": 0,
            "errored": 0,
            "canceled": 0,
            "expired": 0,
        }
        for line in results:
            result_type = line.get("result", {}).get("type")
            if result_type in counts:
                counts[result_type] += 1

        self.result_lines[batch_id] = results
        self.batch_records[batch_id].update({
            "processing_status": status,
            "request_counts": SimpleNamespace(
                succeeded=counts["succeeded"],
                errored=counts["errored"],
                canceled=counts["canceled"],
                expired=counts["expired"],
                processing=0 if status == "ended" else max(
                    self.batch_records[batch_id]["request_counts"].processing,
                    0,
                ),
            ),
            "results_url": f"https://example.invalid/{batch_id}/results" if results else None,
            "ended_at": datetime.now().isoformat() if status == "ended" else None,
        })

class FakeGeminiBatchBackend:
    """Small in-memory harness for Gemini batch contract tests."""

    def __init__(self, provider: GeminiProvider):
        self.provider = provider
        self.submitted_batches: list[dict] = []
        self.batch_records: dict[str, dict] = {}
        self.uploaded_files: dict[str, str] = {}
        self.downloaded_files: dict[str, str] = {}
        self._wire_provider()

    def _wire_provider(self) -> None:
        async def upload_batch_requests_file(file_path, display_name=None):
            file_id = f"files/input-{len(self.uploaded_files) + 1}"
            with open(file_path, "r", encoding="utf-8") as handle:
                self.uploaded_files[file_id] = handle.read()
            return {
                "name": file_id,
                "display_name": display_name,
            }

        async def create_batch_job(src, display_name=None):
            batch_id = f"batches/{len(self.batch_records) + 1}"
            request_list = [
                json.loads(line)
                for line in self.uploaded_files[src].splitlines()
                if line.strip()
            ]
            self.submitted_batches.append({
                "id": batch_id,
                "display_name": display_name,
                "input_file_name": src,
                "requests": request_list,
            })
            self.batch_records[batch_id] = {
                "id": batch_id,
                "name": batch_id,
                "state": "BATCH_STATE_PENDING",
                "completed_at": None,
                "batch_stats": {
                    "request_count": len(request_list),
                    "pending_request_count": len(request_list),
                    "running_request_count": 0,
                    "succeeded_request_count": 0,
                    "failed_request_count": 0,
                    "cancelled_request_count": 0,
                    "expired_request_count": 0,
                },
                "dest": {
                    "file_name": None,
                    "inlined_responses": [],
                },
            }
            return dict(self.batch_records[batch_id])

        async def get_batch_job(name):
            return dict(self.batch_records[name])

        async def cancel_batch_job(name):
            record = self.batch_records[name]
            record["state"] = "BATCH_STATE_CANCELLED"
            record["completed_at"] = datetime.now().isoformat()
            record["batch_stats"] = {
                "request_count": record["batch_stats"]["request_count"],
                "pending_request_count": 0,
                "running_request_count": 0,
                "succeeded_request_count": 0,
                "failed_request_count": 0,
                "cancelled_request_count": record["batch_stats"]["request_count"],
                "expired_request_count": 0,
            }
            return dict(record)

        async def download_batch_result_file(file_name):
            return self.downloaded_files[file_name]

        self.provider.upload_batch_requests_file = upload_batch_requests_file
        self.provider.create_batch_job = create_batch_job
        self.provider.get_batch_job = get_batch_job
        self.provider.cancel_batch_job = cancel_batch_job
        self.provider.download_batch_result_file = download_batch_result_file

    def set_batch_result(self, batch_id: str, *, status: str, result_lines: list[dict]) -> None:
        """Configure what a completed Gemini batch returns."""
        succeeded = sum(1 for item in result_lines if item.get("response"))
        failed = sum(1 for item in result_lines if item.get("error"))
        result_file_name = f"files/result-{len(self.downloaded_files) + 1}"
        self.downloaded_files[result_file_name] = "\n".join(
            json.dumps(item) for item in result_lines
        ) + "\n"
        self.batch_records[batch_id].update({
            "state": status,
            "completed_at": datetime.now().isoformat(),
            "batch_stats": {
                "request_count": len(result_lines),
                "pending_request_count": 0,
                "running_request_count": 0,
                "succeeded_request_count": succeeded,
                "failed_request_count": failed,
                "cancelled_request_count": 0,
                "expired_request_count": 0,
            },
            "dest": {
                "file_name": result_file_name,
                "inlined_responses": [],
            },
        })

def discover_test_configs():
    """Find all YAML test configuration files recursively."""
    if not CONFIGS_DIR.exists():
        return []
    # Find all .yml files recursively, excluding README and non-test files
    all_ymls = list(CONFIGS_DIR.rglob("*.yml"))
    non_test_stems = {"main_config", "sql_queries", "main_with_sql_import"}
    test_ymls = [
        f for f in all_ymls
        if f.stem not in non_test_stems
        and not any(part.startswith(".") for part in f.relative_to(CONFIGS_DIR).parts)
    ]
    return sorted(test_ymls)

def _format_test_id(path: Path) -> str:
    """Return a descriptive pytest id for a scenario."""
    try:
        rel_path = path.relative_to(CONFIGS_DIR)
    except ValueError:
        rel_path = path
    return str(rel_path)

def _summarize_config(config: dict, config_file: Path) -> str:
    """Create a human readable summary of the scenario being executed."""
    test_type = config.get('test_type') or config.get('_test_type') or 'unspecified'
    description = config.get('description') or config.get('_description') or config.get('name') or 'No description provided'
    has_ingest = 'ingest' in config
    enrichment_names = [e.get('name', '<unnamed>') for e in config.get('enrichments', []) if isinstance(e, dict)]
    export_names = list(config.get('exports', {}).keys()) if isinstance(config.get('exports'), dict) else []

    phases = []
    if has_ingest:
        phases.append('ingest')
    if enrichment_names:
        phases.append('enrich')
    if export_names:
        phases.append('export')
    phase_text = ' + '.join(phases) if phases else 'none'

    lines = [
        "\n=== Scenario ============================================",
        f"Config: {config_file.relative_to(CONFIGS_DIR)}",
        f"Type:   {test_type}",
        f"Desc:   {description}",
        f"Phases: {phase_text}",
    ]

    if enrichment_names:
        lines.append(f"Enrichments: {', '.join(enrichment_names)}")
    if export_names:
        lines.append(f"Exports:     {', '.join(export_names)}")

    lines.append("========================================================\n")
    return "\n".join(lines)

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
                    print(f"  Warning: Output table {table_name} is empty")
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


__all__ = [
    "TESTS_DIR",
    "ASSETS_DIR",
    "CONFIGS_DIR",
    "temp_env",
    "mock_external_apis",
    "FakeOpenAIBatchBackend",
    "FakeAnthropicBatchBackend",
    "FakeGeminiBatchBackend",
    "discover_test_configs",
    "_format_test_id",
    "_summarize_config",
    "verify_enrichment_results",
]
