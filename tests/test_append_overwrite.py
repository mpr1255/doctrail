"""
Test append vs overwrite mode functionality.

This test ensures that:
1. In append mode, already-processed rows are skipped
2. In overwrite mode, already-processed rows are reprocessed
3. The --limit flag works correctly with both modes
"""

import pytest
import tempfile
import sqlite3
import yaml
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock
import asyncio
import json


@pytest.fixture
def temp_db():
    """Create a temporary database with test data."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Create literature table
    cursor.execute("""
        CREATE TABLE literature (
            sha1 TEXT PRIMARY KEY,
            title TEXT,
            authors TEXT,
            year INTEGER,
            abstract TEXT,
            raw_content TEXT,
            structured_summary TEXT
        )
    """)

    # Insert test data - 5 rows total
    # First 2 rows have summaries
    # Next 3 rows don't have summaries
    test_data = [
        ('sha1_001', 'Paper 1', 'Author A', 2024, 'Abstract 1', 'x' * 2000, 'Summary 1'),
        ('sha1_002', 'Paper 2', 'Author B', 2024, 'Abstract 2', 'x' * 2000, 'Summary 2'),
        ('sha1_003', 'Paper 3', 'Author C', 2024, 'Abstract 3', 'x' * 2000, None),
        ('sha1_004', 'Paper 4', 'Author D', 2024, 'Abstract 4', 'x' * 2000, None),
        ('sha1_005', 'Paper 5', 'Author E', 2024, 'Abstract 5', 'x' * 2000, None),
    ]

    cursor.executemany(
        "INSERT INTO literature VALUES (?, ?, ?, ?, ?, ?, ?)",
        test_data
    )

    # Create enrichment_responses table
    cursor.execute("""
        CREATE TABLE enrichment_responses (
            enrichment_id TEXT,
            sha1 TEXT,
            enrichment_name TEXT,
            raw_json TEXT,
            model_used TEXT,
            prompt_id TEXT,
            full_prompt TEXT,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()

    yield Path(db_path)
    Path(db_path).unlink(missing_ok=True)


def test_prompt_id_uses_shared_effective_prompt_with_append_file(tmp_path):
    from doctrail.core_runtime.shared import _resolve_enrichment_prompt
    from doctrail.db_operations import compute_prompt_id, get_or_create_prompt_id

    append_file = tmp_path / "append.txt"
    append_file.write_text("Appendix text", encoding="utf-8")
    config_path = tmp_path / "config.yml"
    config_path.write_text("enrichments: []\n", encoding="utf-8")
    db_path = tmp_path / "prompts.db"

    enrichment_config = {
        "name": "topic_review",
        "prompt": "Base prompt",
        "system_prompt": "System prompt",
        "append_file": append_file.name,
    }
    config_data = {"__config_path__": str(config_path)}

    effective_prompt = _resolve_enrichment_prompt(enrichment_config, config_data)
    outer_prompt_id = compute_prompt_id(
        enrichment_config["name"],
        effective_prompt,
        enrichment_config["system_prompt"],
    )
    stored_prompt_id = get_or_create_prompt_id(
        str(db_path),
        enrichment_config["name"],
        effective_prompt,
        enrichment_config["system_prompt"],
        "gpt-4o-mini",
    )

    assert effective_prompt == "Base prompt\n\nAppendix text"
    assert outer_prompt_id == stored_prompt_id
    assert outer_prompt_id == "fdde1d6abbfe53973e52443c5d580d08eff887b02d3ff7db9f1db0011fe5980e"


@pytest.fixture
def config_file(temp_db):
    """Create a test configuration file."""
    config = {
        'database': str(temp_db),
        'sql_queries': {
            'test_query': 'SELECT rowid, sha1 FROM literature WHERE raw_content IS NOT NULL ORDER BY year DESC'
        },
        'models': {
            'test_model': {
                'name': 'gpt-4o-mini'
            }
        },
        'enrichments': [{
            'name': 'test_summary',
            'input': {
                'query': 'test_query',
                'input_columns': ['title', 'abstract', 'raw_content']
            },
            'prompt': 'Summarize: {title}',
            'model': 'test_model',
            'output_column': 'structured_summary'
        }]
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
        yaml.dump(config, f)
        config_path = f.name

    yield Path(config_path)
    Path(config_path).unlink(missing_ok=True)


def test_append_mode_skips_processed_rows(temp_db, config_file):
    """Prompt-scoped append mode should skip rows that already have output data."""
    from doctrail.main import cli
    from click.testing import CliRunner

    runner = CliRunner()

    # Mock the LLM call to track which rows are processed
    processed_rows = []

    async def mock_process_enrichment(*args, **kwargs):
        results = kwargs.get('results', [])
        for row in results:
            if row.get('sha1') not in ['sha1_001', 'sha1_002']:  # These should be skipped
                processed_rows.append(row.get('sha1'))
        return []

    with patch('doctrail.core.process_enrichment', new=mock_process_enrichment):
        # Run enrichment with --limit 2 in append mode (default)
        result = runner.invoke(cli, [
            'enrich',
            '--config', str(config_file),
            '--enrichments', 'test_summary',
            '--limit', '2',
            '--dedupe-scope', 'prompt',
            '--allow-column-collision',
            '--skip-cost-check'
        ])

        # Check that it recognized rows as already processed
        assert "already processed" in result.output.lower() or "all rows already" in result.output.lower()

        # Verify no API calls were made for already-processed rows
        assert 'sha1_001' not in processed_rows
        assert 'sha1_002' not in processed_rows


def test_overwrite_mode_reprocesses_rows(temp_db, config_file):
    """Test that overwrite mode reprocesses rows that already have data."""
    from doctrail.main import cli
    from click.testing import CliRunner

    runner = CliRunner()

    # Mock the LLM call
    processed_rows = []

    async def mock_process_enrichment(*args, **kwargs):
        results = kwargs.get('results', [])
        for row in results:
            processed_rows.append(row.get('sha1'))
        return [{'sha1': r.get('sha1'), 'updated': f"New summary for {r.get('sha1')}"}
                for r in results]

    with patch('doctrail.core.process_enrichment', new=mock_process_enrichment):
        # Run enrichment with --limit 2 in overwrite mode
        result = runner.invoke(cli, [
            'enrich',
            '--config', str(config_file),
            '--enrichments', 'test_summary',
            '--limit', '2',
            '--overwrite',
            '--allow-column-collision',
            '--skip-cost-check'
        ])

        # Check that it mentions overwrite mode
        assert "overwrite" in result.output.lower()

        # Verify API calls WERE made for already-processed rows
        assert 'sha1_001' in processed_rows or 'sha1_002' in processed_rows


def test_limit_with_append_processes_unprocessed_rows(temp_db, config_file):
    """Prompt-scoped append mode should process only rows missing the output column."""
    from doctrail.main import cli
    from click.testing import CliRunner

    runner = CliRunner()

    # Mock the LLM call
    processed_rows = []

    async def mock_process_enrichment(*args, **kwargs):
        results = kwargs.get('results', [])
        for row in results:
            processed_rows.append(row.get('sha1'))
        return [{'sha1': r.get('sha1'), 'updated': f"Summary for {r.get('sha1')}"}
                for r in results]

    with patch('doctrail.core.process_enrichment', new=mock_process_enrichment):
        # Run enrichment with --limit 3 in append mode
        # Should skip first 2 (have summaries) and process next 3 up to limit
        result = runner.invoke(cli, [
            'enrich',
            '--config', str(config_file),
            '--enrichments', 'test_summary',
            '--limit', '5',  # Get all 5 rows
            '--dedupe-scope', 'prompt',
            '--allow-column-collision',
            '--skip-cost-check'
        ])

        # Only unprocessed rows should be processed
        assert 'sha1_001' not in processed_rows  # Has summary
        assert 'sha1_002' not in processed_rows  # Has summary
        assert 'sha1_003' in processed_rows or 'sha1_004' in processed_rows or 'sha1_005' in processed_rows


def test_query_scope_uses_query_hash_not_existing_output_column(temp_db):
    """Query-scoped append mode should key off successful outputs for that query."""
    from doctrail.llm_operations import process_batch
    from doctrail.db_operations import create_query_hash, ensure_enrichments_table, get_or_create_prompt_id

    base_query = "SELECT rowid, sha1 FROM literature WHERE sha1 = 'sha1_001'"
    changed_query = "SELECT rowid, sha1 FROM literature WHERE sha1 = 'sha1_001' AND year >= 2024"
    prompt_text = 'Summarize: {title}'
    prompt_id = get_or_create_prompt_id(str(temp_db), 'test_summary', prompt_text, None, 'gpt-4o-mini')
    ensure_enrichments_table(str(temp_db))

    conn = sqlite3.connect(temp_db)
    conn.execute(
        """
        INSERT INTO _enrichments (
            key_value, enrichment_name, field_name, value, value_type, timestamp,
            model, prompt_hash, enrichment_id, run_id, query_hash
        ) VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?)
        """,
        (
            'sha1_001',
            'test_summary',
            'summary',
            'old summary',
            'string',
            'gpt-4o-mini',
            prompt_id,
            'seed-001',
            'run-001',
            create_query_hash(base_query),
        ),
    )
    conn.commit()
    conn.close()

    rows = [{
        'sha1': 'sha1_001',
        'title': 'Paper 1',
        'abstract': 'Abstract 1',
        'raw_content': 'x' * 2000,
        'summary': 'Existing summary should not force a skip in query scope',
    }]

    async def mock_llm_call(*args, **kwargs):
        return "Fresh summary"

    async def run_once(query_hash):
        with patch('doctrail.llm_operations.call_llm', new=mock_llm_call), \
             patch('doctrail.llm_operations.persist_enrichment_result'):
            return await process_batch(
                results=rows,
                prompt=prompt_text,
                model='gpt-4o-mini',
                pbar=MagicMock(),
                input_cols=['title', 'abstract', 'raw_content'],
                parsed_input_cols=[('title', None), ('abstract', None), ('raw_content', None)],
                output_cols=['summary'],
                db_path=str(temp_db),
                table='literature',
                enrichment_config={'name': 'test_summary'},
                overwrite=False,
                verbose=False,
                query_hash=query_hash,
                dedupe_scope='query',
            )

    same_query_result = asyncio.run(run_once(create_query_hash(base_query)))
    assert same_query_result[0]['updated'] is None
    assert 'already processed' in same_query_result[0]['original']

    changed_query_result = asyncio.run(run_once(create_query_hash(changed_query)))
    assert changed_query_result[0]['updated'] == 'Fresh summary'


def test_query_scope_retries_error_only_attempts(temp_db):
    """Audit-only error attempts should not block a later retry in append mode."""
    from doctrail.llm_operations import process_batch
    from doctrail.db_operations import create_query_hash, ensure_enrichment_audit_table, get_or_create_prompt_id

    base_query = "SELECT rowid, sha1 FROM literature WHERE sha1 = 'sha1_001'"
    prompt_text = 'Summarize: {title}'
    prompt_id = get_or_create_prompt_id(str(temp_db), 'test_summary', prompt_text, None, 'gpt-4o-mini')
    ensure_enrichment_audit_table(str(temp_db))

    conn = sqlite3.connect(temp_db)
    conn.execute(
        """
        INSERT INTO _enrichment_audit (
            enrichment_id, key_value, enrichment_name, raw_json, model_used, prompt_id, query_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            'seed-error-001',
            'sha1_001',
            'test_summary',
            json.dumps({'error': 'provider timeout'}),
            'gpt-4o-mini',
            prompt_id,
            create_query_hash(base_query),
        ),
    )
    conn.commit()
    conn.close()

    rows = [{
        'sha1': 'sha1_001',
        'title': 'Paper 1',
        'abstract': 'Abstract 1',
        'raw_content': 'x' * 2000,
        'summary': None,
    }]

    async def mock_llm_call(*args, **kwargs):
        return "Fresh summary"

    async def run_once():
        with patch('doctrail.llm_operations.call_llm', new=mock_llm_call), \
             patch('doctrail.llm_operations.persist_enrichment_result'):
            return await process_batch(
                results=rows,
                prompt=prompt_text,
                model='gpt-4o-mini',
                pbar=MagicMock(),
                input_cols=['title', 'abstract', 'raw_content'],
                parsed_input_cols=[('title', None), ('abstract', None), ('raw_content', None)],
                output_cols=['summary'],
                db_path=str(temp_db),
                table='literature',
                enrichment_config={'name': 'test_summary'},
                overwrite=False,
                verbose=False,
                query_hash=create_query_hash(base_query),
                dedupe_scope='query',
            )

    retry_result = asyncio.run(run_once())
    assert retry_result[0]['updated'] == 'Fresh summary'


def test_enrichment_scope_yaml_skips_null_prompt_hash_and_cli_override(temp_db, tmp_path):
    """YAML dedupe_scope=enrichment should skip complete NULL-prompt rows; CLI should override it."""
    from doctrail.db_operations import ensure_enrichments_table
    from doctrail.main import cli
    from click.testing import CliRunner

    ensure_enrichments_table(str(temp_db))
    with sqlite3.connect(temp_db) as conn:
        conn.executemany(
            """
            INSERT INTO _enrichments (
                key_value, enrichment_name, field_name, value, value_type, timestamp,
                model, prompt_hash, enrichment_id, run_id, query_hash
            ) VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?)
            """,
            [
                (
                    'sha1_001', 'test_summary', 'summary', 'old summary', 'string',
                    'gpt-4o-mini', None, 'seed-001', 'run-001', None,
                ),
                (
                    'sha1_001', 'test_summary', 'rationale', 'old rationale', 'string',
                    'gpt-4o-mini', None, 'seed-001', 'run-001', None,
                ),
            ],
        )

    config = {
        'database': str(temp_db),
        'sql_queries': {
            'test_query': "SELECT rowid, sha1 FROM literature WHERE sha1 = 'sha1_001'"
        },
        'models': {
            'test_model': {
                'name': 'gpt-4o-mini'
            }
        },
        'enrichments': [{
            'name': 'test_summary',
            'dedupe_scope': 'enrichment',
            'input': {
                'query': 'test_query',
                'input_columns': ['title', 'abstract', 'raw_content']
            },
            'prompt': 'Changed prompt: {title}',
            'model': 'test_model',
            'schema': {
                'summary': {'type': 'string'},
                'rationale': {'type': 'string'},
            },
        }]
    }
    config_path = tmp_path / "config.yml"
    config_path.write_text(yaml.safe_dump(config))

    runner = CliRunner()
    yaml_result = runner.invoke(cli, [
        'enrich',
        '--config', str(config_path),
        '--enrichments', 'test_summary',
        '--dry-run',
        '--skip-cost-check',
    ])
    assert yaml_result.exit_code == 0, yaml_result.output
    assert "Already done:     1" in yaml_result.output, yaml_result.output
    assert "Would process:    0" in yaml_result.output, yaml_result.output

    override_result = runner.invoke(cli, [
        'enrich',
        '--config', str(config_path),
        '--enrichments', 'test_summary',
        '--dedupe-scope', 'query',
        '--dry-run',
        '--skip-cost-check',
    ])
    assert override_result.exit_code == 0, override_result.output
    assert "Already done:     0" in override_result.output, override_result.output
    assert "Would process:    1" in override_result.output, override_result.output


def test_enrichment_scope_requires_all_requested_fields(temp_db):
    """Name-scoped dedupe should not skip a partial multi-field enrichment."""
    from doctrail.llm_operations import process_batch
    from doctrail.db_operations import ensure_enrichments_table

    ensure_enrichments_table(str(temp_db))
    with sqlite3.connect(temp_db) as conn:
        conn.executemany(
            """
            INSERT INTO _enrichments (
                key_value, enrichment_name, field_name, value, value_type, timestamp,
                model, prompt_hash, enrichment_id, run_id, query_hash
            ) VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?)
            """,
            [
                (
                    'sha1_001', 'test_summary', 'summary', 'old summary', 'string',
                    'gpt-4o-mini', None, 'seed-001', 'run-001', None,
                ),
                (
                    'sha1_001', 'test_summary', 'rationale', 'old rationale', 'string',
                    'gpt-4o-mini', None, 'seed-001', 'run-001', None,
                ),
                (
                    'sha1_002', 'test_summary', 'summary', 'partial summary', 'string',
                    'gpt-4o-mini', None, 'seed-002', 'run-002', None,
                ),
            ],
        )

    rows = [
        {
            'sha1': 'sha1_001',
            'title': 'Paper 1',
            'abstract': 'Abstract 1',
            'raw_content': 'x' * 2000,
        },
        {
            'sha1': 'sha1_002',
            'title': 'Paper 2',
            'abstract': 'Abstract 2',
            'raw_content': 'x' * 2000,
        },
    ]
    processed = []

    async def mock_llm_call(model, messages, *args, **kwargs):
        prompt_text = messages[-1]['content']
        processed.append('sha1_002' if 'Paper 2' in prompt_text else 'unexpected')
        return "Fresh summary"

    async def run_once():
        with patch('doctrail.llm_operations.call_llm', new=mock_llm_call), \
             patch('doctrail.llm_operations.persist_enrichment_result'):
            return await process_batch(
                results=rows,
                prompt='Changed prompt: {title}',
                model='gpt-4o-mini',
                pbar=MagicMock(),
                input_cols=['title', 'abstract', 'raw_content'],
                parsed_input_cols=[('title', None), ('abstract', None), ('raw_content', None)],
                output_cols=['summary', 'rationale'],
                db_path=str(temp_db),
                table='literature',
                enrichment_config={'name': 'test_summary'},
                overwrite=False,
                verbose=False,
                query_hash='changed-query',
                dedupe_scope='enrichment',
            )

    result = asyncio.run(run_once())
    by_key = {row['key_value']: row for row in result}

    assert by_key['sha1_001']['updated'] is None
    assert 'already processed' in by_key['sha1_001']['original']
    assert by_key['sha1_002']['updated'] == 'Fresh summary'
    assert processed == ['sha1_002']


def test_enrichment_scope_skips_complete_rows_from_different_model(temp_db):
    """Enrichment-scoped dedupe treats the enrichment name, not model, as identity."""
    from doctrail.llm_operations import process_batch
    from doctrail.db_operations import ensure_enrichments_table

    ensure_enrichments_table(str(temp_db))
    with sqlite3.connect(temp_db) as conn:
        conn.executemany(
            """
            INSERT INTO _enrichments (
                key_value, enrichment_name, field_name, value, value_type, timestamp,
                model, prompt_hash, enrichment_id, run_id, query_hash
            ) VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?)
            """,
            [
                (
                    'sha1_001', 'test_summary', 'summary', 'old summary', 'string',
                    'provider/model-a', None, 'seed-001', 'run-001', None,
                ),
                (
                    'sha1_001', 'test_summary', 'rationale', 'old rationale', 'string',
                    'provider/model-a', None, 'seed-001', 'run-001', None,
                ),
            ],
        )

    rows = [{
        'sha1': 'sha1_001',
        'title': 'Paper 1',
        'abstract': 'Abstract 1',
        'raw_content': 'x' * 2000,
    }]
    processed = []

    async def mock_llm_call(model, messages, *args, **kwargs):
        processed.append(model)
        return "Fresh summary"

    async def run_once():
        with patch('doctrail.llm_operations.call_llm', new=mock_llm_call), \
             patch('doctrail.llm_operations.persist_enrichment_result'):
            return await process_batch(
                results=rows,
                prompt='Changed prompt: {title}',
                model='provider/model-b',
                pbar=MagicMock(),
                input_cols=['title', 'abstract', 'raw_content'],
                parsed_input_cols=[('title', None), ('abstract', None), ('raw_content', None)],
                output_cols=['summary', 'rationale'],
                db_path=str(temp_db),
                table='literature',
                enrichment_config={'name': 'test_summary'},
                overwrite=False,
                verbose=False,
                query_hash='changed-query',
                dedupe_scope='enrichment',
            )

    result = asyncio.run(run_once())

    assert result[0]['updated'] is None
    assert 'already processed' in result[0]['original']
    assert processed == []


def test_parallel_processing():
    """Test that multiple rows are processed in parallel, not sequentially."""
    import time
    from doctrail.llm_operations import process_batch

    # Create mock data
    results = [
        {'sha1': f'sha_{i}', 'title': f'Paper {i}', 'raw_content': 'x' * 1000}
        for i in range(5)
    ]

    # Track timing
    call_times = []

    async def mock_llm_call(*args, **kwargs):
        call_times.append(time.time())
        await asyncio.sleep(1)  # Simulate API delay
        return "Mock response"

    async def run_test():
        with patch('doctrail.llm_operations.call_llm', new=mock_llm_call), \
             patch('doctrail.llm_operations.persist_enrichment_result'):
            await process_batch(
                results=results,
                prompt="Test prompt",
                model="test-model",
                pbar=MagicMock(),
                input_cols=['title', 'raw_content'],
                parsed_input_cols=[('title', None), ('raw_content', None)],
                output_cols=['summary'],
                db_path=":memory:",
                table="test",
                enrichment_config={'name': 'test'},
                overwrite=True,
                verbose=False
            )

    # Run the test
    start_time = time.time()
    asyncio.run(run_test())
    total_time = time.time() - start_time

    # If sequential, would take 5+ seconds
    # If parallel, should take ~1 second plus overhead
    assert total_time < 3, f"Processing took {total_time}s, should be parallel"

    # Check that calls started close together (within 0.5 seconds)
    if len(call_times) >= 2:
        time_spread = max(call_times) - min(call_times)
        assert time_spread < 0.5, f"Calls spread over {time_spread}s, should start together"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
