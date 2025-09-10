# Doctrail Configuration Specification

This document provides a complete specification for Doctrail configuration files (config.yaml).

## Table of Contents
1. [Basic Structure](#basic-structure)
2. [Core Settings](#core-settings)
3. [SQL Queries](#sql-queries)
4. [Model Configuration](#model-configuration)
5. [System Prompts](#system-prompts)
6. [Enrichments](#enrichments)
7. [Exports](#exports)
8. [Zotero Configuration](#zotero-configuration)
9. [Complete Example](#complete-example)

## Basic Structure

A Doctrail config file is a YAML document with the following top-level keys:

```yaml
database: <path>              # Required
default_model: <model_name>   # Optional
verbose: <boolean>            # Optional
log_updates: <boolean>        # Optional

sql_queries: {...}            # Optional
models: {...}                 # Optional
system_prompts: {...}         # Optional
enrichments: [...]            # Required for enrich command
exports: {...}                # Optional
zotero: {...}                 # Optional
```

## Core Settings

### database
**Type:** String (file path)  
**Required:** Yes  
**Description:** Path to SQLite database file. Supports `~` expansion.

```yaml
database: ~/projects/research/data.db
```

### default_model
**Type:** String  
**Default:** `gpt-4o-mini`  
**Description:** Default LLM model for enrichments.

### verbose
**Type:** Boolean  
**Default:** `false`  
**Description:** Enable verbose logging.

### log_updates
**Type:** Boolean  
**Default:** `false`  
**Description:** Save enrichment results to timestamped JSON files.

## SQL Queries

Named SQL queries for reuse across enrichments.

```yaml
sql_queries:
  query_name: "SQL statement"
  
  # Examples:
  recent_docs: "SELECT * FROM documents WHERE created > date('now', '-7 days')"
  chinese_docs: "SELECT * FROM documents WHERE language = 'zh'"
  unprocessed: "SELECT * FROM documents WHERE processed IS NULL"
```

## Model Configuration

Configure available LLM models and their parameters.

```yaml
models:
  model_name:
    name: <actual_model_name>     # Required: OpenAI model identifier
    max_tokens: <integer>         # Optional
    temperature: <float>          # Optional (0.0-2.0)
    
  # Example:
  gpt-4o:
    name: gpt-4o
    max_tokens: 8192
    temperature: 0.0
```

## System Prompts

Reusable system prompts for LLM calls.

```yaml
system_prompts:
  prompt_name: |
    Multi-line system prompt text.
    Can include instructions for the LLM.
    
  # Example:
  translator: |
    You are a professional translator specializing in legal documents.
    Maintain exact meaning and formal tone.
```

## Enrichments

Define tasks that process database content through LLMs.

### Basic Structure

```yaml
enrichments:
  - name: <string>                # Required: Unique identifier
    description: <string>         # Optional: Human-readable description
    table: <string>               # Required: Target table name
    input:                        # Required: Input configuration
      query: <string>             # SQL query or named query reference
      input_columns: [...]        # List of column names to use
    output_column: <string>       # Required*: Single output column
    output_columns: [...]         # Required*: Multiple output columns
    output_table: <string>        # Optional: Custom table for complex schemas
    prompt: <string>              # Required: User prompt template
    append_file: <string>         # Optional: Path to file to append to prompt
    system_prompt: <string>       # Optional: System prompt or reference
    model: <string>               # Optional: Override default model
    schema: <object>              # Optional: JSON schema for validation
```

*Either `output_column` or `output_columns` must be specified.

### Input Configuration Details

The `input` section defines a two-stage data flow:

1. **SQL Query Stage**: Determines which ROWS to process
   - The `query` field filters which documents/rows from the database will be processed
   - Should include `SELECT rowid, *` or `SELECT rowid, sha1, *` for proper tracking
   - Can reference named queries from `sql_queries` section or be inline SQL
   - Examples:
     - `"SELECT rowid, * FROM documents WHERE language = 'zh'"` (only Chinese docs)
     - `"SELECT rowid, * FROM documents WHERE processed IS NULL"` (unprocessed docs)
     - `"all_docs"` (reference to named query)

2. **Input Columns Stage**: Determines which DATA from each row goes to the LLM
   - The `input_columns` field filters what data from each selected row is sent to the LLM
   - Can include character limits to optimize for LLM context windows
   - Syntax:
     - `["column_name"]` - send full column content
     - `["column_name:500"]` - send only first 500 characters
     - `["content:500", "filename"]` - send truncated content + full filename

This separation allows for:
- Efficient filtering at the database level (SQL WHERE clauses, JOINs, etc.)
- Content optimization for LLM context limits (character truncation)
- Processing only relevant documents while controlling LLM input size

#### Input Column Character Limits

Input columns support character limits to prevent exceeding LLM context windows:

```yaml
input_columns: 
  - "raw_content:1000"    # Only first 1000 characters
  - "filename"            # Full filename (no limit)
  - "metadata:200"        # Only first 200 characters of metadata
```

### Examples

#### Simple Enrichment
```yaml
enrichments:
  - name: summarize
    table: documents
    input:
      query: "SELECT rowid, * FROM documents"  # Process all documents
      input_columns: ["content:500"]           # Send only first 500 chars to LLM
    output_column: summary
    prompt: "Summarize this document in 2-3 sentences:"
```

#### Boolean Classification
```yaml
enrichments:
  - name: has_entities
    table: documents
    input:
      query: unprocessed               # Reference to named query (filters ROWS)
      input_columns: ["content:300", "title"]  # Send truncated content + full title (filters DATA)
    output_column: has_entities
    schema: boolean
    prompt: "Does this document contain named entities? Answer only 'true' or 'false'."
```

#### Translation (Multiple Outputs)
```yaml
enrichments:
  - name: translate_to_english
    table: documents
    input:
      query: chinese_docs
      input_columns: ["content"]
    output_columns:
      - zh_json
      - en_json
      - english_translation
    system_prompt: translator
    prompt: "Translate line by line, maintaining exact correspondence."
```

#### Complex Schema (Separate Table)
```yaml
enrichments:
  - name: extract_entities
    table: documents
    input:
      query: has_entities_true
      input_columns: ["content"]
    output_table: extracted_entities
    schema:
      entities:
        type: "array"
        items:
          type: "object"
          properties:
            name: {type: "string"}
            type: {enum: ["person", "organization", "location", "date", "other"]}
            context: {type: "string", maxLength: 200}
    model: gpt-4o
    prompt: "Extract all named entities from this document."
```

## Exports

Configure data export with templates and formats.

```yaml
exports:
  export_name:
    description: <string>         # Optional
    query: <string>               # Required: SQL query for data
    template: <string>            # Required: Path to Jinja2 template
    formats: [...]                # Required: Output formats (md, html, pdf, docx)
    required_fields: [...]        # Optional: Fields that must be non-null
    output_naming: <pattern>      # Optional: Output filename pattern
    
  # Example:
  entity_report:
    description: "Generate entity extraction report"
    query: |
      SELECT d.*, COUNT(e.entity_id) as entity_count
      FROM documents d
      LEFT JOIN entities e ON d.sha1 = e.doc_sha1
      GROUP BY d.rowid
    template: "templates/entity_report.j2"
    formats: ["md", "pdf"]
    output_naming: "entities_{timestamp}"
```

## Zotero Configuration

For ingesting documents from Zotero.

```yaml
zotero:
  api_key: <string>               # Can use environment variables
  library_id: <string>
  library_type: <string>          # 'user' or 'group'
  
# Example with environment variables:
zotero:
  api_key: "${ZOTERO_API_KEY}"
  library_id: "${ZOTERO_LIBRARY_ID}"
  library_type: "user"
```

## Complete Example

```yaml
# Doctrail configuration for legal document analysis
database: ~/research/legal_docs.db
default_model: gpt-4o-mini
verbose: true
log_updates: true

# Reusable SQL queries
sql_queries:
  unprocessed: "SELECT rowid, sha1, * FROM documents WHERE processed IS NULL"
  has_entities: "SELECT rowid, sha1, * FROM documents WHERE entity_count > 0"
  recent: "SELECT * FROM documents WHERE date_added > date('now', '-30 days')"

# Model configurations
models:
  gpt-4o-mini:
    name: gpt-4o-mini
    max_tokens: 4096
    temperature: 0.0
  gpt-4o:
    name: gpt-4o
    max_tokens: 8192
    temperature: 0.0

# System prompts
system_prompts:
  legal_analyst: |
    You are an expert legal analyst specializing in case law.
    Focus on factual accuracy and legal precedents.
  translator: |
    You are a certified legal translator.
    Maintain exact legal terminology and meaning.

# Reusable schemas can be defined inline
# Complex schemas automatically create separate tables

# Enrichment tasks
enrichments:
  # Step 1: Quick classification
  - name: classify_document
    description: "Classify document type"
    table: documents
    input:
      query: unprocessed
      input_columns: ["content"]
    output_column: doc_type
    schema: document_type
    prompt: "Classify this document as: legal_case, statute, regulation, or other."
    
  # Step 2: Entity detection
  - name: detect_entities
    description: "Check for legal entities"
    table: documents
    input:
      query: "SELECT * FROM documents WHERE doc_type = 'legal_case'"
      input_columns: ["content"]
    output_column: has_entities
    schema: boolean
    system_prompt: legal_analyst
    prompt: "Does this legal document mention specific parties, judges, or attorneys?"
    
  # Step 3: Full extraction with JSON schema
  - name: extract_case_details
    description: "Extract comprehensive case information"
    table: documents
    input:
      query: has_entities
      input_columns: ["content", "title"]
    output_table: case_details
    schema:
      case_type:
        enum: ["civil", "criminal", "administrative", "appellate"]
      jurisdiction: {type: "string"}
      decision_date: {type: "string"}
      parties:
        type: "array"
        items:
          type: "object"
          properties:
            name: {type: "string"}
            party_type: ["plaintiff", "defendant", "appellant", "respondent"]
            represented_by: {type: "string"}
      holdings:
        type: "array"
        items:
          type: "object"
          properties:
            issue: {type: "string"}
            ruling: {type: "string"}
            precedent_cited: {type: "string"}
    model: gpt-4o
    system_prompt: legal_analyst
    prompt: |
      Analyze this legal case and extract structured information.
    append_file: "legal_extraction_guide.md"

# Export configurations
exports:
  case_summary:
    description: "Export case analysis summary"
    query: |
      SELECT 
        d.*,
        cd.case_type,
        cd.jurisdiction,
        cd.decision_date,
        cd.parties,
        cd.holdings
      FROM documents d
      LEFT JOIN case_details cd ON d.sha1 = cd.sha1
      WHERE d.doc_type = 'legal_case'
      ORDER BY cd.decision_date DESC
    template: "templates/case_summary.j2"
    formats: ["md", "pdf", "docx"]
    output_naming: "case_analysis_{timestamp}"

# Zotero configuration
zotero:
  api_key: "${ZOTERO_API_KEY}"
  library_id: "${ZOTERO_LIBRARY_ID}"
  library_type: "user"
```

## Best Practices

1. **Start Simple**: Begin with basic enrichments before adding complexity
2. **Use Named Queries**: Define reusable queries in `sql_queries`
3. **Leverage System Prompts**: Create consistent prompts for similar tasks
4. **Test Incrementally**: Use `--limit` flag to test on small datasets
5. **Version Control**: Keep configs in git for tracking changes
6. **Document Schemas**: Add descriptions to complex enrichments
7. **Validate Output**: Use schemas for structured data validation