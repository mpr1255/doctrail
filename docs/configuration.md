# Configuration Guide

This guide covers all configuration options for Doctrail YAML files.

## Table of Contents
- [Configuration Structure](#configuration-structure)
- [Global Settings](#global-settings)
- [SQL Queries](#sql-queries)
- [Enrichments](#enrichments)
- [Schemas](#schemas)
- [File Appending](#file-appending)
- [Exports](#exports)
- [Advanced Configuration](#advanced-configuration)

## Configuration Structure

A Doctrail configuration file has this basic structure:

```yaml
# Global settings
database: path/to/database.db
default_model: gpt-4o-mini
default_table: documents

# Named SQL queries
sql_queries:
  query_name: "SQL statement"

# Enrichment tasks
enrichments:
  - name: task_name
    # ... enrichment config

# Export configurations  
exports:
  - name: export_name
    # ... export config
```

## Global Settings

### Database Configuration

```yaml
# Required: Path to SQLite database
database: ~/research/documents.db

# Optional: Default table for enrichments
default_table: documents  # Default: "documents"
```

### Model Configuration

```yaml
# Default model for all enrichments
default_model: gpt-4o-mini

# Model-specific configurations (optional)
models:
  gpt-4o:
    name: gpt-4o
    max_tokens: 8192
    temperature: 0.0
  
  gemini-flash:
    name: gemini-2.0-flash-exp
    max_tokens: 1000000
```

### Logging Settings

```yaml
# Enable verbose logging
verbose: true  # Default: false

# Save enrichment results to JSON files
log_updates: true  # Default: false
```

## SQL Queries

Named queries for reuse across enrichments:

```yaml
sql_queries:
  # Simple queries
  all_docs: "SELECT rowid, sha1, * FROM documents"
  recent: "SELECT rowid, sha1, * FROM documents WHERE created > date('now', '-7 days')"
  
  # Complex queries with joins
  analyzed_docs: |
    SELECT 
      d.rowid, 
      d.sha1,
      d.raw_content,
      da.sentiment,
      da.key_topics
    FROM documents d
    LEFT JOIN document_analysis da ON d.sha1 = da.sha1
    WHERE da.sentiment IS NOT NULL
  
  # Parameterized for enrichments
  unprocessed: "SELECT rowid, sha1, * FROM documents WHERE sentiment IS NULL"
```

### Query Best Practices

1. Always include `rowid` and `sha1` in SELECT
2. Use `ORDER BY rowid` for consistent ordering
3. Add NULL checks for append mode
4. Consider indexes for performance

## Enrichments

### Basic Enrichment Structure

```yaml
enrichments:
  - name: unique_name          # Required: Unique identifier
    description: "Purpose"     # Optional: Human-readable description
    table: documents          # Optional: Override default_table
    input:                    # Required: Input configuration
      query: query_name       # SQL query or reference
      input_columns: [...]    # Columns to send to LLM
    output_column: column     # For simple schemas
    output_table: table_name  # For complex schemas
    schema: {}               # Schema definition
    prompt: "Instructions"    # Required: LLM prompt
    append_file: "file.md"   # Optional: Append file to prompt
    model: gpt-4o            # Optional: Override default_model
    system_prompt: "Role"    # Optional: System instructions
```

### Input Configuration

The `input` section controls data flow:

```yaml
input:
  # Query: Selects which ROWS to process
  query: "SELECT * FROM documents WHERE language = 'en'"
  # Or reference a named query
  query: recent_docs
  
  # Input columns: Selects which DATA from each row
  input_columns:
    - "content"          # Full column
    - "abstract:500"     # First 500 characters
    - "title"            # Full column
    - "metadata:1000"    # First 1000 characters
```

### Schema Types

#### Simple Schema (Direct Column)

```yaml
# Boolean
schema:
  has_citations: {type: "boolean"}

# Enum (verbose) - single choice
schema:
  category: {enum: ["research", "news", "blog", "other"]}

# Enum (shorthand) - single choice
schema:
  sentiment: ["positive", "negative", "neutral"]

# Enum List - multiple choices allowed
schema:
  topics: 
    enum_list: ["tech", "health", "finance", "politics", "science"]
    min_items: 1  # Must select at least 1
    max_items: 3  # Can select at most 3
    # unique_items: true  # Default - duplicates are automatically removed

# String with constraints
schema:
  summary: 
    type: "string"
    maxLength: 200

# Number with range
schema:
  score:
    type: "number"
    minimum: 0
    maximum: 100
```

#### Complex Schema (Separate Table)

```yaml
# Automatic separate table for complex data
output_table: analysis_results  # Optional custom name
key_column: sha1               # Default: "sha1"

schema:
  # Multiple fields
  sentiment: {enum: ["very_positive", "positive", "neutral", "negative", "very_negative"]}
  confidence: {type: "number", minimum: 0, maximum: 1}
  
  # Arrays
  topics:
    type: "array"
    items: {type: "string"}
    maxItems: 5
    minItems: 1
  
  # Nested objects
  metrics:
    type: "object"
    properties:
      readability: {type: "number"}
      complexity: {enum: ["low", "medium", "high"]}
      word_count: {type: "integer", minimum: 0}
```

### Language Validation

Enforce language requirements:

```yaml
schema:
  chinese_text:
    type: "string"
    x-language: "chinese"  # Must contain Chinese characters
  
  english_only:
    type: "string"
    x-language: "english"  # ASCII only
  
  mixed_allowed:
    type: "string"  # No language restriction
```

### Field Conversions

Automatic conversions for data processing:

```yaml
schema:
  # Convert Chinese to Pinyin
  city:
    type: "string"
    convert: "chinese_to_pinyin"
  
  province:
    type: "string"
    convert: "chinese_to_pinyin"
```

## File Appending

The `append_file` feature allows you to keep complex prompts in separate files:

```yaml
enrichments:
  - name: complex_analysis
    input:
      query: all_documents
      input_columns: ["content"]
    schema:
      analysis: {type: "string"}
    prompt: |
      Analyze this document according to the following guidelines:
    append_file: "analysis_guidelines.md"  # Relative to YAML file
```

The file `analysis_guidelines.md` (in the same folder as the YAML):
```markdown
## Analysis Guidelines

1. **Structure Analysis**
   - Identify document sections
   - Note organizational patterns
   - Assess logical flow

2. **Content Analysis**
   - Extract key themes
   - Identify supporting evidence
   - Note contradictions

3. **Quality Assessment**
   - Rate clarity (1-10)
   - Rate completeness (1-10)
   - Note any missing elements
```

### How It Works

1. The prompt from the YAML is used first
2. The content of `append_file` is appended with double newline
3. Then the input columns data is added
4. The complete prompt is sent to the LLM

### File Path Resolution

- **Relative paths**: Resolved relative to the YAML file's directory
- **Absolute paths**: Used as-is
- **Example**: If your YAML is at `/home/user/project/config.yml`
  - `append_file: "prompts/guidelines.md"` → `/home/user/project/prompts/guidelines.md`
  - `append_file: "/shared/prompts/guidelines.md"` → `/shared/prompts/guidelines.md`

## Exports

### Export Configuration

```yaml
exports:
  - name: analysis_report
    description: "Comprehensive analysis export"
    
    # SQL query for data
    query: |
      SELECT 
        d.*,
        da.sentiment,
        da.topics
      FROM documents d
      JOIN document_analysis da ON d.sha1 = da.sha1
    
    # Output configuration
    output_file: "report_{timestamp}.csv"  # With variables
    format: "csv"  # For simple exports
    
    # Or for template-based exports
    template: "templates/report.md"
    formats: ["md", "pdf", "html"]  # Multiple outputs
    
    # Optional filters
    required_fields: ["sentiment", "topics"]  # Skip if NULL
    
    # Filename patterns
    output_naming: "{title}_{date}"  # From row data
```

### Export Formats

Supported formats via Pandoc:
- `csv` - Direct SQL to CSV
- `md` - Markdown
- `pdf` - PDF via Typst
- `html` - Self-contained HTML
- `docx` - Microsoft Word
- `odt` - OpenDocument
- `rtf` - Rich Text Format

## Advanced Configuration

### Multi-Table Processing

```yaml
# Define multiple tables
tables:
  documents:
    base_query: "SELECT rowid, sha1, * FROM documents"
  
  literature:
    base_query: "SELECT rowid, sha1, * FROM literature"

# Enrichment can target any table
enrichments:
  - name: analyze_all
    table: all  # Special: process all defined tables
    # Or
    table: "documents,literature"  # Specific tables
```

### Conditional Processing

```yaml
# Chain enrichments with conditions
enrichments:
  # Step 1: Classify
  - name: classify
    output_column: doc_type
    schema: ["legal", "medical", "technical", "other"]
    
  # Step 2: Process only legal documents  
  - name: extract_legal
    input:
      query: "SELECT * FROM documents WHERE doc_type = 'legal'"
```

### Batch Size Control

```yaml
# Global batch size
batch_size: 50  # Default: 30

# Per-enrichment override
enrichments:
  - name: heavy_processing
    batch_size: 10  # Smaller batches for complex tasks
```

### Truncation Settings

```yaml
# Global truncation
truncate: true  # Auto-truncate all enrichments

# Per-enrichment
enrichments:
  - name: summarize
    truncate: true  # Enable for this task
    truncate_margin: 1000  # Safety margin (tokens)
```

## Complete Example

Here's a comprehensive configuration showcasing all features:

```yaml
# Research Paper Analysis Configuration
database: ~/research/papers.db
default_model: gpt-4o-mini
default_table: papers
verbose: true

# Reusable queries
sql_queries:
  recent_papers: |
    SELECT rowid, sha1, * FROM papers 
    WHERE published_date > date('now', '-90 days')
    ORDER BY published_date DESC
  
  unclassified: |
    SELECT rowid, sha1, * FROM papers
    WHERE research_area IS NULL

# System prompts
system_prompts:
  researcher: |
    You are an expert research analyst specializing in academic literature.
    Be precise and cite specific sections when relevant.

# Enrichments pipeline
enrichments:
  # Step 1: Classify research area
  - name: classify_area
    description: "Identify primary research area"
    input:
      query: unclassified
      input_columns: ["title", "abstract:500"]
    output_column: research_area
    schema: ["AI/ML", "Biology", "Physics", "Chemistry", "Medicine", "Other"]
    prompt: |
      Based on the title and abstract, identify the primary research area.
      Choose the most specific applicable category.
    
  # Step 2: Detailed analysis with external prompt file
  - name: paper_analysis
    description: "Comprehensive paper analysis"
    input:
      query: recent_papers
      input_columns: ["title", "abstract", "introduction:2000"]
    output_table: paper_insights
    schema:
      methodology:
        enum: ["experimental", "theoretical", "review", "meta-analysis"]
      
      significance:
        type: "integer"
        minimum: 1
        maximum: 10
        description: "Potential impact score"
      
      key_findings:
        type: "array"
        items: 
          type: "string"
          maxLength: 200
        maxItems: 5
      
      limitations:
        type: "string"
        maxLength: 500
      
      future_work:
        type: "array"
        items: {type: "string"}
        maxItems: 3
    
    system_prompt: researcher
    model: gpt-4o  # Use more capable model
    prompt: |
      Analyze this research paper according to the detailed guidelines below.
    append_file: "analysis_rubric.md"  # Detailed rubric in separate file

# Export configurations
exports:
  - name: research_summary
    description: "Export analysis results to CSV"
    query: |
      SELECT 
        p.title,
        p.authors,
        p.published_date,
        p.research_area,
        pi.methodology,
        pi.significance,
        pi.key_findings
      FROM papers p
      JOIN paper_insights pi ON p.sha1 = pi.sha1
      WHERE pi.significance >= 7
      ORDER BY pi.significance DESC
    output_file: "high_impact_papers_{timestamp}.csv"
    format: "csv"
  
  - name: detailed_reports
    description: "Generate detailed PDF reports"
    query: |
      SELECT p.*, pi.*
      FROM papers p
      JOIN paper_insights pi ON p.sha1 = pi.sha1
      WHERE p.published_date > date('now', '-30 days')
    template: "templates/paper_report.md"
    formats: ["md", "pdf", "html"]
    output_naming: "{title}_analysis"
```

## Best Practices

1. **Start Simple**: Test with basic schemas before adding complexity
2. **Use Named Queries**: Improve readability and reusability
3. **Character Limits**: Prevent token limit errors with input truncation
4. **Test Small**: Use `--limit` flag during development
5. **Version Control**: Keep configs in git for tracking changes
6. **Document Schemas**: Add descriptions for complex fields
7. **Validate Outputs**: Use enum schemas for consistent classification
8. **Organize Prompts**: Use `append_file` for complex prompts

---

For specific use cases, see:
- [Schema-Driven Enrichment](schema-driven.md)
- [Export Templates](templates.md)
- [Example Configurations](examples.md)