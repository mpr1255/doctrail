# Doctrail Schema Examples & Test Suite

**THIS IS THE SINGLE SOURCE OF TRUTH**: All schema patterns, test cases, and examples are here.

This directory contains:
1. **Working examples** of every schema pattern in Doctrail
2. **Test cases** that the test suite runs against
3. **The definitive reference** for LLMs learning Doctrail's schema system

## Directory Structure

```
schema_examples/
├── single_field/          # Single-field schema examples
├── multi_field/           # Multi-field schema examples
├── advanced/              # Advanced features (multi-model, converters, validation)
├── test_framework/        # Test-specific schemas (ingestion, export, etc.)
├── main_config.yml        # Main config showing imports
├── sql_queries.yml        # Reusable SQL queries
└── test_*.yml            # Test configs for import functionality
```

Doctrail's current product contract is:
- Model outputs are stored in normalized tables such as `enrichments` and `enrichment_audit`
- Humans inspect wide derived views such as `run_*`, `final_*`, and `view pivot` outputs
- Some example configs still include `output_table` because the test suite covers older compatibility paths and adjacent tooling

## Quick Reference

### Single-field schemas

```yaml
# EXPLICIT ENUM (recommended)
schema:
  enum: ["positive", "negative", "neutral"]
output_column: sentiment  # Field alias in normalized storage and views

# BOOLEAN 
schema: boolean
# Accepts: "true", "false", "yes", "no", "1", "0"
# Stored as normalized enrichment values

# INTEGER WITH CONSTRAINTS
schema:
  year: {type: "integer", minimum: 1900, maximum: 2024}
```

### Multi-field schemas

```yaml
schema:
  sentiment: {enum: ["positive", "negative", "neutral"]}
  confidence: {type: "float", minimum: 0.0, maximum: 1.0}  # Use "float" not "number"
  summary: {type: "string", maxLength: 500}
```

### Arrays vs Enum Lists

```yaml
# ARRAY - Extract unknown values from text
organizations: {type: "array", items: {type: "string"}, maxItems: 10}
# Output: ["Apple Inc.", "Some New Company", "Unknown Org"]

# ENUM LIST - Select multiple from predefined choices
tags:
  enum_list: ["urgent", "important", "review", "archive"]
  min_items: 1
  max_items: 3
# Output: ["urgent", "review"] - ONLY from the list
```

## Complete Schema Type Reference

| Type | YAML Schema | Python Type | SQLite Storage | When to Use |
|------|------------|-------------|----------------|-------------|
| **String** | `{type: "string"}` | `str` | `TEXT` | Any text |
| **Integer** | `{type: "integer"}` | `int` | `INTEGER` | Whole numbers |
| **Float** | `{type: "float"}` | `float` | `REAL` | Decimals |
| **Boolean** | `{type: "boolean"}` | `bool` | `INTEGER` (0/1) | True/false |
| **Enum** | `{enum: ["a", "b"]}` | `Enum` | `TEXT` | Single choice |
| **Enum List** | `{enum_list: ["a", "b"]}` | `List[Enum]` | `TEXT` (JSON) | Multiple choices |
| **Array** | `{type: "array", items: {type: "string"}}` | `List[str]` | `TEXT` (JSON) | Extract unknowns |

## Examples by Category

### Single Field (`single_field/`)
- **`classify_language.yml`** - Simple enum classification with explicit syntax
- **`validate_content.yml`** - Boolean validation
- **`test_enrich_direct_column.yml`** - Integer extraction with constraints

### Multi-Field (`multi_field/`)
- **`extract_entities.yml`** - Array extraction for organizations, locations, terms
- **`multi_table_review.yml`** - Multi-table inputs using `table.column` syntax
- **`test_enrich_separate_table.yml`** - Complex payment information extraction

### Advanced Features (`advanced/`)
- **`test_enrich_multi_model.yml`** - Compare outputs from multiple LLMs
- **`test_enrich_with_converter.yml`** - Chinese to Pinyin conversion
- **`test_enrich_validation_error.yml`** - Language validation and retry logic
- **`test_enrich_with_schema.yml`** - Complex nested schemas

### Test Framework (`test_framework/`)
- **`test_ingest_basic.yml`** - Document ingestion tests
- **`test_enrich_overwrite_mode.yml`** - Overwrite behavior tests
- **`test_zotero_literature_plugin.yml`** - Plugin integration tests

## Multi-Table Enrichments (NEW!)

**The Key Innovation**: Use sha1 as a universal document key to combine data from ANY tables.

### How It Works
1. **Your query returns sha1 values** (the document identifiers)
2. **Use `table.column` syntax** to fetch data from multiple tables  
3. **Doctrail joins automatically** using sha1 as the key
4. **Missing data returns NULL** gracefully

### Real-World Example
```yaml
sql_queries:
  deduplicated_docs: |
    SELECT sha1 FROM analysis_v1 
    WHERE confidence > 0.8
    AND valid_record = 'yes'

enrichments:
  - name: enhanced_extraction
    input:
      query: deduplicated_docs        # Returns list of sha1 values
      input_columns:
        # Multi-table syntax - fetch from different tables by sha1
        - "documents.raw_content"     # Original document content
        - "documents.filename"        # File metadata
        - "analysis_v1.province"      # Previous analysis results
        - "analysis_v1.city"
        - "red_cross_funds.evidence"  # Data from another enrichment
        - "metadata.author"           # Additional metadata table
    prompt: |
      Previous analysis: {analysis_v1.province} {analysis_v1.city}
      Existing evidence: {red_cross_funds.evidence}
      Document: {documents.filename}
      
      Content: {documents.raw_content}
```

### Character Limits
```yaml
input_columns:
  - "documents.raw_content:2000"  # Limit to 2000 characters
  - "documents.title"             # No limit
```

### Backward Compatibility
```yaml
input_columns:
  - "raw_content"           # Uses default table (from query)
  - "documents.filename"    # Explicit table.column syntax
```

## Constraints (All Enforced by LLM APIs)

```yaml
# STRING CONSTRAINTS
title:
  type: "string"
  minLength: 10      # Minimum characters
  maxLength: 500     # Maximum characters
  pattern: "^[A-Z]"  # Regex pattern

# NUMERIC CONSTRAINTS  
score:
  type: "float"
  minimum: 0.0       # Inclusive minimum
  maximum: 100.0     # Inclusive maximum
  
# ARRAY CONSTRAINTS
tags:
  type: "array"
  items: {type: "string"}
  minItems: 1        # Must have at least 1
  maxItems: 5        # Can have at most 5

# ENUM LIST CONSTRAINTS
categories:
  enum_list: ["A", "B", "C", "D"]
  min_items: 1       # Select at least 1
  max_items: 3       # Select at most 3
  unique_items: true # No duplicates (default: true)
```

## Language Validation (NEW: Array Support!)

### String Fields
```yaml
# Enforce Chinese characters
chinese_summary:
  type: "string"
  lang: "zh"        # Must contain Chinese characters

# Enforce English only  
english_summary:
  type: "string"
  lang: "en"        # No Chinese characters allowed
```

### Array Fields with Language Validation
```yaml
# Each array item must be in specified language
evidence_zh:
  type: "array"
  items: {type: "string", lang: "zh"}  # Each item must be Chinese
  maxItems: 5

keywords_en:
  type: "array" 
  items: {type: "string", lang: "en"}  # Each item must be English
  maxItems: 10
```

### Mixed Language Schema Example
```yaml
schema:
  summary_zh: {type: "string", lang: "zh"}                        # Chinese summary
  evidence_en: {type: "array", items: {type: "string", lang: "en"}} # English evidence list
  keywords_zh: {type: "array", items: {type: "string", lang: "zh"}} # Chinese keywords
  confidence: {type: "float", minimum: 0, maximum: 1}             # No language constraint
```

## Storage mental model

```
Source tables keep the original inputs
enrichment_audit stores the raw call/response history
enrichments stores parsed fields in normalized long form
run_* / final_* / pivot views turn those fields into wide review tables
```

## Test Database

**ALL tests use `/tests/assets/test.db`** with this schema:

```sql
CREATE TABLE documents (
    sha1 TEXT PRIMARY KEY,       -- Universal document key
    file_path TEXT,
    filename TEXT,
    file_extension TEXT,
    raw_content TEXT,            -- Main content for enrichment
    tika_metadata JSON,
    metadata_updated TEXT
);
```

**Important**: Your test configs should use:
- `database: placeholder` (test runner handles the real path)
- Columns that exist: `filename`, `raw_content`, `file_extension`, etc.
- `SELECT rowid, sha1 FROM documents` for input queries; prompt payload columns are fetched from `input_columns`

## Running Tests

The test suite automatically discovers and runs all YAML files in this directory:

```bash
# Run all tests
uv run python -m pytest tests/test_doctrail.py -v

# Run specific category
uv run python -m pytest tests/test_doctrail.py -k "single_field"

# Run specific test
uv run python -m pytest tests/test_doctrail.py -k "test_array_language_validation"

# Run with output for debugging
uv run python -m pytest tests/test_doctrail.py -v -s
```

## Testing Your Own Schema

1. Create a YAML file in the appropriate subdirectory
2. Follow the patterns shown in existing files
3. Run: `./doctrail enrich --config your_file.yml --limit 1 --verbose`

## Common Patterns

### Document Classification
```yaml
name: classify_document
schema:
  enum: ["technical", "business", "legal", "other"]
output_column: document_type
prompt: "Classify this document into one of the categories."
```

### Entity Extraction
```yaml
name: extract_entities
schema:
  people: {type: "array", items: {type: "string"}, maxItems: 10}
  organizations: {type: "array", items: {type: "string"}, maxItems: 10}
  locations: {type: "array", items: {type: "string"}, maxItems: 10}
prompt: "Extract all people, organizations, and locations mentioned."
```

### Multi-Select Tagging
```yaml
name: tag_document
schema:
  tags:
    enum_list: ["urgent", "confidential", "draft", "review_needed", "approved"]
    min_items: 1
    max_items: 3
output_column: document_tags
prompt: "Select 1-3 tags that apply to this document."
```

### Sentiment Analysis
```yaml
name: analyze_sentiment
schema:
  overall_sentiment: {enum: ["positive", "negative", "neutral"]}
  confidence: {type: "float", minimum: 0.0, maximum: 1.0}
  key_phrases: {type: "array", items: {type: "string"}, maxItems: 5}
prompt: "Analyze the sentiment with confidence score and key phrases."
```

## Best Practices

1. **Be Explicit**: Use `{enum: [...]}` not just `[...]`
2. **Use "float"**: Not "number" for decimals
3. **Set Array Limits**: Always use maxItems
4. **Consider Enums for Yes/No**: `{enum: ["yes", "no"]}` over boolean
5. **Name Your Output**: Use `output_column` or a named schema field for single extracted values
6. **Think in views**: Define the extracted fields first, then decide how you want to inspect them wide

## Common Errors and Solutions

| Error | Cause | Solution |
|-------|-------|----------|
| "Language validation failed" | Output doesn't match lang constraint | Check prompt clarity |
| "Response not in enum" | LLM returned value not in list | Make prompt more explicit |
| "Cannot convert to float" | Non-numeric response | Add "respond with a number" to prompt |

---

**Remember**: This is the ONLY place for schema examples and test cases. Everything is here!
