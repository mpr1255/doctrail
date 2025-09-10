# Doctrail Features

This document covers all major features of Doctrail, organized by use case.

## Table of Contents
- [Schema-Driven Enrichment](#schema-driven-enrichment)
- [Input Processing Features](#input-processing-features)
- [Multi-Model Support](#multi-model-support)
- [Processing Modes](#processing-modes)
- [Export System](#export-system)
- [Plugin System](#plugin-system)
- [Advanced Features](#advanced-features)

## Schema-Driven Enrichment

Doctrail's most powerful feature is schema-driven enrichment using structured outputs.

### Simple Schema (Direct Column Storage)

For simple data types, values are stored directly in the source table:

```yaml
enrichments:
  - name: sentiment
    input:
      query: "SELECT * FROM documents"
      input_columns: ["content"]
    schema:
      sentiment: {enum: ["positive", "negative", "neutral"]}
    prompt: "Analyze the sentiment of this document."
```

This creates a `sentiment` column in the `documents` table.

### Complex Schema (Separate Table Storage)

For complex structured data, Doctrail automatically creates separate tables:

```yaml
enrichments:
  - name: detailed_analysis
    input:
      query: "SELECT * FROM documents"
      input_columns: ["content"]
    output_table: document_analysis  # Custom table name
    schema:
      sentiment: {enum: ["very_positive", "positive", "neutral", "negative", "very_negative"]}
      confidence: {type: "number", minimum: 0, maximum: 1}
      key_topics: {type: "array", items: {type: "string"}, maxItems: 5}
      metrics:
        type: "object"
        properties:
          readability_score: {type: "number"}
          complexity_level: {enum: ["low", "medium", "high"]}
```

### Schema Types

Supported JSON Schema types:
- `string` - Text values
- `number` - Floating point numbers
- `integer` - Whole numbers
- `boolean` - True/false values
- `array` - Lists of values
- `object` - Nested structures
- `enum` - Predefined values (shorthand: array of strings)

### Language-Specific Validation

Enforce language requirements in responses:

```yaml
schema:
  chinese_only_field:
    type: "string"
    x-language: "chinese"  # Must contain Chinese characters
  
  english_only_field:
    type: "string" 
    x-language: "english"  # Must contain only ASCII characters
```

### Field Conversions

Automatic conversions for data processing:

```yaml
schema:
  city:
    type: "string"
    convert: "chinese_to_pinyin"  # Convert Chinese to Pinyin
  
  province:
    type: "string"
    convert: "chinese_to_pinyin"
```

### Dual Storage System

All enrichments use dual storage:
1. **Raw JSON** in `enrichment_responses` table (audit trail)
2. **Parsed columns** in target table (queryable data)

## Input Processing Features

### Input Column Limits

Prevent token limit errors by truncating input:

```yaml
input:
  input_columns:
    - "content:1000"      # First 1000 characters
    - "title"             # Full title
    - "abstract:500"      # First 500 characters
```

### Query Filtering

Two-stage filtering system:
1. **SQL Query**: Selects which rows to process
2. **Input Columns**: Selects which data from each row to send to LLM

```yaml
input:
  # Stage 1: Select Chinese documents only
  query: "SELECT * FROM documents WHERE language = 'zh'"
  
  # Stage 2: Send only specific fields to LLM
  input_columns: ["title", "abstract:200"]
```

### File Appending

Keep complex prompts organized in separate files:

```yaml
enrichments:
  - name: complex_analysis
    prompt: |
      Analyze this document using the framework below:
    append_file: "analysis_framework.md"  # Relative to YAML file
```

The prompt and file content are combined before sending to the LLM.

## Multi-Model Support

### OpenAI Models

```yaml
default_model: gpt-4o-mini  # Fast and cheap
# Or override per enrichment:
enrichments:
  - name: complex_analysis
    model: gpt-4o  # More capable
```

Supported OpenAI models:
- `gpt-4o-mini` - Fast, cost-effective (128k context)
- `gpt-4o` - Most capable (128k context)
- `gpt-4` - Legacy (8k context)
- `gpt-3.5-turbo` - Legacy (16k context)

### Google Gemini Models

```yaml
default_model: gemini-2.0-flash-exp
```

Supported Gemini models:
- `gemini-2.0-flash-exp` - Latest experimental (1M context)

### Model Context Limits

Doctrail automatically handles context limits:

```bash
# Automatic truncation
./doctrail.py enrich --config config.yml --enrichments task --truncate

# Or configure in YAML
enrichments:
  - name: summarize
    truncate: true  # Auto-truncate long inputs
```

## Processing Modes

### Append Mode (Default)

Skip documents that already have values:

```bash
./doctrail.py enrich --config config.yml --enrichments sentiment
# Second run skips processed documents
```

### Overwrite Mode

Reprocess all documents, replacing existing values:

```bash
./doctrail.py enrich --config config.yml --enrichments sentiment --overwrite
```

### Incremental Processing

Process specific subsets:

```bash
# By row count
--limit 100

# By specific row ID
--rowid 42

# By document hash
--sha1 abc123def456
```

### Batch Processing

Control concurrent API calls:

```yaml
# In config
batch_size: 50

# Or via CLI
--batch-size 50
```

## Export System

### Template-Based Exports

Doctrail uses Jinja2 templates for flexible exports:

```yaml
exports:
  - name: report
    query: "SELECT * FROM documents WHERE processed = 1"
    template: "templates/report.j2"
    formats: ["md", "pdf", "html"]
    output_naming: "{title}_{date}"
```

### Built-in Templates

Example parallel translation template:

```markdown
---
title: "{{ title if title else sha1 }}"
date: "{{ date }}"
---

{% for idx, zh_line in zh_lines.items() %}
{{ zh_line }}

{{ en_lines[idx] }}

{% endfor %}
```

### Export Formats

Via Pandoc integration:
- Markdown (`.md`)
- PDF (`.pdf`) - via Typst
- HTML (`.html`) - with embedded resources
- Word (`.docx`)
- And more...

### Dynamic Queries

Exports can use complex SQL with joins:

```yaml
exports:
  - name: comprehensive_report
    query: |
      SELECT 
        d.*,
        da.sentiment,
        da.key_topics,
        GROUP_CONCAT(e.entity_name) as entities
      FROM documents d
      LEFT JOIN document_analysis da ON d.sha1 = da.sha1
      LEFT JOIN entities e ON d.sha1 = e.doc_sha1
      GROUP BY d.sha1
```

## Plugin System

### Built-in Plugins

1. **Default File Ingester** - Handles common document formats
2. **Zotero Connector** - Imports from Zotero libraries
3. **DOI Connector** - Ingests from DOI cache databases

### Plugin Discovery

Plugins are loaded from (in order):
1. `src/plugins/` - Built-in plugins
2. `./plugins/` - Current directory
3. `--plugin-dir` - Custom directory

### Creating Custom Plugins

Implement the `IngesterPlugin` protocol:

```python
class MyPlugin:
    @property
    def name(self) -> str:
        return "my_plugin"
    
    @property
    def description(self) -> str:
        return "My custom ingestion plugin"
    
    def add_arguments(self, parser):
        parser.add_argument('--my-option', help='Custom option')
    
    async def ingest(self, args) -> Dict[str, Any]:
        # Implementation
        return {
            "documents_processed": count,
            "errors": []
        }
```

## Advanced Features

### Audit Trail

All enrichments are logged in `enrichment_responses` table:

```sql
SELECT 
    sha1,
    enrichment_name,
    model_used,
    raw_json,
    created_at
FROM enrichment_responses
WHERE enrichment_name = 'sentiment'
ORDER BY created_at DESC;
```

### WAL Mode for Concurrency

SQLite databases use WAL mode for better concurrency:
- Multiple readers
- Single writer
- Automatic checkpointing

### Session Logging

Detailed logs saved to timestamped files:

```bash
ls /tmp/doctrail_logs/
# doctrail_20241203_141523.log
# doctrail_20241203_152234.log
```

### Graceful Interruption

Ctrl+C handling with progress preservation:
```
^C
✋ Enrichment interrupted by user.
💡 Run the same command again to continue where you left off.
```

### Named Queries

Reuse SQL queries across configurations:

```yaml
sql_queries:
  recent: "SELECT * FROM documents WHERE created > date('now', '-7 days')"
  unprocessed: "SELECT * FROM documents WHERE status IS NULL"

enrichments:
  - name: analyze_recent
    input:
      query: recent  # Reference named query
```

### System Prompts

Reusable system prompts for consistency:

```yaml
system_prompts:
  researcher: |
    You are an expert research assistant.
    Be precise and cite sources when available.

enrichments:
  - name: analyze
    system_prompt: researcher  # Reference named prompt
```

### Character Encoding

Full UTF-8 support throughout:
- Database storage
- LLM communication  
- Export files
- JSON serialization

### Mojibake Detection

Automatic detection and correction of encoding issues in LLM responses:
- Detects common mojibake patterns (e.g., "CafÃ©" instead of "Café")
- Attempts automatic fixing of UTF-8/Latin-1 encoding errors
- Logs warnings when mojibake is detected
- Works with both OpenAI and Gemini models

### Error Handling

Comprehensive error handling:
- Schema validation errors
- API failures with retry
- Missing data handling
- Clear error messages

### Progress Tracking

Visual progress with tqdm:
- Document count
- Processing rate
- Time estimates
- Animated spinner

## Performance Features

### Async Processing

Concurrent LLM API calls:
- 30 concurrent API calls (default)
- 2 concurrent DB writes
- Configurable limits

### Smart Filtering

Efficient query optimization:
- NULL checks for append mode
- Index usage
- Prepared statements

### Memory Management

Streaming processing:
- Batch processing
- Periodic WAL checkpoints
- No full dataset loading

## Security Features

### API Key Management

Environment variables:
```bash
export OPENAI_API_KEY="sk-..."
export GOOGLE_AI_API_KEY="..."
```

### Safe Database Operations

- Parameterized queries
- Transaction management
- Automatic backups via exports

### Input Validation

- Schema validation
- Type checking
- Enum constraints
- Language validation

---

For more details on specific features, see:
- [Configuration Guide](configuration.md)
- [Schema-Driven Development](schema-driven.md)
- [Plugin Development](plugins.md)