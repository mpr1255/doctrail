# Doctrail Test Suite

This directory contains end-to-end tests for doctrail functionality.

## How Tests Work

The test suite uses **parametrized testing** - each YAML file in `test_configs/` becomes a test case that runs through the actual CLI.

### Test Types

1. **Ingest Tests** - Test document ingestion
   - Example: `test_ingest_basic.yml`

2. **Enrichment Tests** - Test LLM enrichment functionality
   - Example: `test_enrich_direct_column.yml` (single field → column)
   - Example: `test_enrich_separate_table.yml` (multi-field → table)

3. **Plugin Tests** - Test plugins like Zotero
   - Example: `test_zotero_literature_plugin.yml`

## Running Tests

```bash
# Run all tests
uv run python -m pytest tests/test_doctrail.py -v

# Run specific test
uv run python -m pytest tests/test_doctrail.py::test_cli_command[test_enrich_direct_column] -v

# Run with output
uv run python -m pytest tests/test_doctrail.py -xvs
```

## Adding New Tests

1. Create a new YAML file in `test_configs/`
2. The test will automatically be discovered and run

### Example Test Config

```yaml
# test_configs/test_my_feature.yml
database: placeholder  # Will use temp database
default_model: gpt-4.1-mini

sql_queries:
  docs: |
    SELECT rowid FROM documents  # Columns fetched based on input_columns!

enrichments:
  - name: my_enrichment
    input:
      query: docs
      input_columns: ["raw_content"]
    schema: ["option1", "option2", "option3"]
    prompt: "Classify this document"
```

## Test Infrastructure

- **Automatic Mocking**: All external APIs (OpenAI, Gemini, network) are mocked
- **Isolated Environment**: Each test gets a fresh database and temp directory
- **No API Keys Required**: Tests run without any credentials

## Directory Structure

```
tests/
├── test_doctrail.py        # Main test runner
├── test_configs/           # YAML test configurations
│   ├── test_enrich_*.yml   # Enrichment tests
│   ├── test_ingest_*.yml   # Ingestion tests
│   └── test_*_plugin.yml   # Plugin tests
├── test_yaml_imports/      # Files for import tests
│   └── enrichments/        # Importable enrichments
└── assets/                 # Test documents
    └── files/              # Sample files for ingestion
```

## Key Points

1. **sha1 is the only required field** in all SQL queries
2. Single field schemas → direct column (no output_table)
3. Multi-field schemas → require output_table
