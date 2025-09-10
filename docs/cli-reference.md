# CLI Reference

This document provides a complete reference for all Doctrail command-line options.

## Table of Contents
- [Global Options](#global-options)
- [Commands](#commands)
  - [ingest](#ingest-command)
  - [enrich](#enrich-command)
  - [export](#export-command)

## Global Options

These options apply to all commands:

```bash
doctrail [COMMAND] [OPTIONS]
```

- `-h, --help` - Show help message and exit

## Commands

### `ingest` Command

Ingest documents into a SQLite database from various sources.

#### Basic Usage

```bash
# Ingest from local directory
doctrail ingest --input-dir ./documents --db-path ./database.db

# Ingest from Zotero
doctrail ingest --zotero --collection "My Research" --db-path ./database.db

# Use a custom plugin
doctrail ingest --plugin doi_connector --project "organ_donation" --cache-db ./cache.sqlite --db-path ./database.db
```

#### Options

**Required (one of):**
- `--input-dir PATH` - Directory containing documents to ingest
- `--zotero` - Enable Zotero ingestion mode
- `--plugin NAME` - Use a custom ingestion plugin

**Database Configuration:**
- `--db-path PATH` - Path to SQLite database (required unless specified in config)
- `--config PATH` - Path to YAML config file (optional, overrides other options)
- `--table NAME` - Target table name (default: `documents`)

**Local File Ingestion Options:**
- `--force` - Force ingestion even if schema mismatch detected
- `--overwrite` - Overwrite existing documents
- `--limit N` - Limit number of files to process
- `--include-pattern PATTERN` - Only process files matching glob pattern (e.g., `"*.pdf"`)
- `--exclude-pattern PATTERN` - Skip files matching glob pattern (e.g., `"*.tmp,*.log"`)
- `--readability` - Use readability library for cleaner HTML extraction
- `--html-extractor CHOICE` - HTML extraction method: `default` or `smart` (default: `default`)
- `--yes, -y` - Skip confirmation prompts

**Zotero Options:**
- `--collection NAME` - Name of Zotero collection to ingest (required with `--zotero`)

**Plugin Options:**
- `--plugin-dir PATH` - Directory containing custom plugins

**DOI Connector Plugin Options:**
- `--cache-db PATH` - Path to cache.sqlite database
- `--project NAME` - Project name to filter by (use "ALL" for all projects)
- `--base-path PATH` - Base path for resolving relative file paths

**General Options:**
- `--verbose` - Enable detailed logging

#### Examples

```bash
# Ingest PDFs only, with confirmation
doctrail ingest --input-dir ./papers --db-path ./research.db --include-pattern "*.pdf"

# Ingest without confirmations, exclude temporary files
doctrail ingest --input-dir ./docs --db-path ./data.db --exclude-pattern "*.tmp,*.bak" --yes

# Ingest HTML files with smart extraction (better paragraph handling)
doctrail ingest --input-dir ./html_docs --db-path ./data.db --html-extractor smart

# Ingest from Zotero with API credentials in environment
export ZOTERO_API_KEY="your-api-key"
export ZOTERO_LIBRARY_ID="your-library-id"
doctrail ingest --zotero --collection "Literature Review" --db-path ./lit.db

# Use DOI connector plugin for a specific project
doctrail ingest --plugin doi_connector \
    --project "climate_change" \
    --cache-db /path/to/cache.sqlite \
    --db-path ./climate.db \
    --verbose
```

### `enrich` Command

Process database content through LLM enrichment tasks.

#### Basic Usage

```bash
# Run a single enrichment
doctrail enrich --config config.yml --enrichments sentiment_analysis

# Run multiple enrichments
doctrail enrich --config config.yml --enrichments task1 task2 task3

# Override model and limit rows
doctrail enrich --config config.yml --enrichments classify --model gpt-4o --limit 10
```

#### Options

**Required:**
- `--config PATH` - Path to YAML configuration file
- `--enrichments TASKS` - Enrichment task names (can specify multiple)

**Processing Control:**
- `--limit N` - Process only N rows
- `--rowid N` - Process only specific row by rowid
- `--sha1 HASH` - Process only specific row by SHA1 hash
- `--overwrite` - Overwrite existing values (default: skip rows with data)
- `--truncate` - Truncate long inputs to fit model context window

**Model Configuration:**
- `--model NAME` - Override default model (e.g., `gpt-4o`, `gpt-4o-mini`, `gemini-2.0-flash-exp`)
- `--batch-size N` - Override batch size for processing

**Database Options:**
- `--db-path PATH` - Override database path from config
- `--table NAME` - Process specific table(s), comma-separated or "all"

**Output Options:**
- `--log-updates` - Save enrichment results to timestamped JSON files
- `--verbose` - Enable detailed logging

#### Enrichment Task Syntax

Multiple ways to specify enrichments:

```bash
# Space-separated
--enrichments task1 task2 task3

# Comma-separated
--enrichments task1,task2,task3

# Multiple flags
--enrichments task1 --enrichments task2 --enrichments task3

# Mixed
--enrichments task1,task2 --enrichments task3
```

#### Filter Modes

The `--limit`, `--rowid`, and `--sha1` options are mutually exclusive:

```bash
# Process first 100 rows
doctrail enrich --config config.yml --enrichments analyze --limit 100

# Process specific row for debugging
doctrail enrich --config config.yml --enrichments analyze --rowid 42 --verbose

# Process by document hash
doctrail enrich --config config.yml --enrichments analyze --sha1 a1b2c3d4e5 --overwrite
```

#### Examples

```bash
# Basic sentiment analysis on 10 documents
doctrail enrich --config config.yml --enrichments sentiment --limit 10

# Overwrite existing classifications with a better model
doctrail enrich --config config.yml \
    --enrichments document_classifier \
    --model gpt-4o \
    --overwrite

# Debug a specific document
doctrail enrich --config config.yml \
    --enrichments extract_entities \
    --rowid 150 \
    --verbose

# Process documents with truncation for large content
doctrail enrich --config config.yml \
    --enrichments summarize \
    --truncate \
    --model gpt-4o-mini
```

### `export` Command

Export enriched documents in various formats.

#### Basic Usage

```bash
# Export to markdown
doctrail export --config config.yml --export-type report

# Export to specific directory
doctrail export --config config.yml --export-type analysis --output-dir ./results
```

#### Options

**Required:**
- `--config PATH` - Path to YAML configuration file
- `--export-type NAME` - Name of export configuration to use

**Output Options:**
- `--output-dir PATH` - Output directory (default: `./exports`)
- `--formats LIST` - Override output formats (e.g., `"md,pdf,html"`)

**General Options:**
- `--verbose` - Enable detailed logging

#### Export Formats

Supported output formats (via Pandoc):
- `md` - Markdown
- `html` - HTML with embedded resources
- `pdf` - PDF via Typst
- `docx` - Microsoft Word
- `odt` - OpenDocument Text
- `rtf` - Rich Text Format

#### Examples

```bash
# Export sentiment analysis results
doctrail export --config config.yml --export-type sentiment_report

# Export to custom directory in multiple formats
doctrail export --config config.yml \
    --export-type comprehensive_analysis \
    --output-dir ~/Documents/analysis \
    --formats "md,pdf,html"

# Export with verbose logging
doctrail export --config config.yml \
    --export-type case_studies \
    --verbose
```

## Exit Codes

- `0` - Success
- `1` - General error
- `2` - Invalid usage/bad parameters
- `130` - Interrupted by user (Ctrl+C)

## Environment Variables

Doctrail respects these environment variables:

- `OPENAI_API_KEY` - OpenAI API key for GPT models
- `GOOGLE_AI_API_KEY` or `GEMINI_API_KEY` - Google AI key for Gemini models
- `ZOTERO_API_KEY` - Zotero API key
- `ZOTERO_LIBRARY_ID` - Zotero library ID
- `ZOTERO_LIBRARY_TYPE` - Zotero library type (`user` or `group`)

## Tips and Best Practices

1. **Start Small**: Always test with `--limit` before processing entire datasets
2. **Use Rowid for Debugging**: The `--rowid` option is perfect for troubleshooting specific documents
3. **Check Before Overwriting**: Use `--verbose` to see what will be processed before using `--overwrite`
4. **Save Progress**: Enrichments save after each row, so you can safely interrupt and resume
5. **Monitor Costs**: Use cheaper models (like `gpt-4o-mini`) for initial testing

## Common Workflows

### Initial Setup
```bash
# 1. Ingest documents
doctrail ingest --input-dir ./documents --db-path ./research.db

# 2. Test enrichment on small sample
doctrail enrich --config config.yml --enrichments classify --limit 5 --verbose

# 3. Run full enrichment
doctrail enrich --config config.yml --enrichments classify

# 4. Export results
doctrail export --config config.yml --export-type classification_report
```

### Iterative Refinement
```bash
# Try one model
doctrail enrich --config config.yml --enrichments sentiment --limit 10

# Not happy? Try another model on same documents
doctrail enrich --config config.yml --enrichments sentiment --limit 10 --model gpt-4o --overwrite

# Check specific result
doctrail enrich --config config.yml --enrichments sentiment --sha1 abc123def --verbose
```

### Debugging
```bash
# See what query will be run
doctrail enrich --config config.yml --enrichments task --verbose --limit 0

# Process single document with full logging
doctrail enrich --config config.yml --enrichments task --rowid 42 --verbose

# Check enrichment audit trail
sqlite3 research.db "SELECT * FROM enrichment_responses WHERE sha1='abc123'"
```