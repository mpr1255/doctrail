# Schema-Driven Enrichment Guide

Schema-driven enrichment is Doctrail's most powerful feature, enabling structured, validated outputs from LLMs using JSON Schema and OpenAI's structured outputs API.

## Table of Contents
- [Overview](#overview)
- [Schema Basics](#schema-basics)
- [Storage Modes](#storage-modes)
- [Schema Types Reference](#schema-types-reference)
- [Advanced Schemas](#advanced-schemas)
- [Migration from XML](#migration-from-xml)
- [Best Practices](#best-practices)

## Overview

Schema-driven enrichment ensures that LLM outputs conform to your exact specifications using **two different processing paths** depending on schema complexity:

- **Type Safety**: Outputs are guaranteed to match the schema
- **Automatic Validation**: Invalid responses are rejected  
- **Structured Storage**: Complex data stored in queryable format
- **Dual Storage**: Raw JSON audit trail + parsed columns

### How It Works

Doctrail uses different processing paths based on your schema:

#### Single Field Schemas (Legacy Path)
1. Define a simple schema (single field, enum, boolean)
2. Uses legacy `EnumSchemaManager` or `SimpleSchemaManager` for validation
3. Stores results directly in source table columns
4. **No Pydantic models generated**

#### Multi-Field Schemas (Pydantic Path)  
1. Define a complex schema (2+ fields, arrays, objects)
2. **MUST** include `output_table` parameter
3. Doctrail generates a Pydantic model from the schema
4. Uses OpenAI/Gemini structured outputs API for compliance
5. Results stored in separate table with foreign key relationships

### Processing Decision Tree

```python
if schema_field_count == 1:
    # Legacy processing: EnumSchemaManager, SimpleSchemaManager
    storage_mode = "direct_column"  
elif schema_field_count > 1 and output_table:
    # Pydantic processing: create_pydantic_model_from_schema()
    storage_mode = "separate_table"
else:
    raise ConfigError("Complex schemas require output_table")
```

## Schema Basics

### Simple Example

```yaml
enrichments:
  - name: sentiment_analysis
    input:
      query: "SELECT rowid, sha1 FROM documents"
      input_columns: ["content"]
    output_column: sentiment  # Required for single-field with list schema
    schema: ["positive", "negative", "neutral"]
    prompt: "Analyze the sentiment of this document."
```

This creates a `sentiment` column in your documents table with validated enum values.

### Complex Example

```yaml
enrichments:
  - name: document_insights
    input:
      query: "SELECT * FROM documents"
      input_columns: ["content"]
    output_table: insights  # Separate table for complex data
    schema:
      summary:
        type: "string"
        maxLength: 200
      
      topics:
        type: "array"
        items: {type: "string"}
        maxItems: 5
      
      metrics:
        type: "object"
        properties:
          sentiment_score: {type: "number", minimum: -1, maximum: 1}
          readability: {enum: ["easy", "moderate", "difficult"]}
          word_count: {type: "integer", minimum: 0}
```

## Storage Modes

Doctrail automatically determines storage mode based on schema complexity via `EnrichmentStrategy.determine_enrichment_strategy()`:

### Direct Column Mode (Legacy Processing)

For single field schemas using legacy schema managers:

```yaml
# Simple enum list (most common)
schema: ["tech", "finance", "health", "other"]
# → Uses EnumSchemaManager
# → Creates: documents.{enrichment_name} column

# Boolean field  
schema: boolean
# → Uses SimpleSchemaManager
# → Creates: documents.{enrichment_name} column (INTEGER 0/1)

# Named single field
schema:
  category: {enum: ["tech", "finance", "health", "other"]}
# → Creates: documents.category column
```

### Separate Table Mode (Pydantic Processing)

For multi-field schemas requiring structured outputs:

```yaml
output_table: analysis_results  # REQUIRED for 2+ fields!
schema:
  sentiment: {enum: ["positive", "negative", "neutral"]}
  confidence: {type: "number"}
  topics: {type: "array", items: {type: "string"}, maxItems: 5}

# Processing:
# 1. Creates Pydantic model: AnalysisResultsModel
# 2. Uses structured outputs API (OpenAI/Gemini)
# 3. Creates: analysis_results table with columns:
#    - rowid (primary key)
#    - source_rowid (foreign key to documents.rowid)
#    - sentiment (TEXT)
#    - confidence (REAL)  
#    - topics (TEXT/JSON: '["topic1", "topic2"]')
#    - model_used (TEXT) - for multi-model support
```

### Storage Decision Logic

```python
# From src/enrichment_config.py
field_count = len(schema.keys()) if isinstance(schema, dict) else 1

if field_count == 1:
    storage_mode = "direct_column"
    # Uses: src/schema_managers.py (EnumSchemaManager, SimpleSchemaManager)
elif field_count > 1 and output_table:  
    storage_mode = "separate_table"
    # Uses: src/pydantic_schema.py (create_pydantic_model_from_schema)
else:
    raise EnrichmentConfigError("Complex schemas require output_table")
```

## Optional Fields

By default, all schema fields are required. You can make fields optional in two ways:

### Individual Optional Fields

```yaml
schema:
  required_field: {type: "string"}  # Required
  optional_field: 
    type: "string"
    optional: true  # Can be null
```

### All Fields Optional

```yaml
enrichments:
  - name: analysis
    all_fields_optional: true  # Makes all schema fields optional
    schema:
      field1: {type: "string"}  # Optional
      field2: {type: "number"}  # Optional
```

When fields are optional, the LLM will return `null` instead of placeholder text when information is not found in the document.

## Schema Types Reference

### Basic Types

#### String

```yaml
# Simple string
title: {type: "string"}

# With constraints
summary:
  type: "string"
  minLength: 10
  maxLength: 500
  pattern: "^[A-Z]"  # Must start with capital letter
```

#### Number

```yaml
# Float/decimal
score:
  type: "number"
  minimum: 0
  maximum: 100
  multipleOf: 0.5  # Must be multiple of 0.5

# With exclusive bounds
temperature:
  type: "number"
  exclusiveMinimum: -273.15
  exclusiveMaximum: 1000
```

#### Integer

```yaml
# Whole numbers only
count:
  type: "integer"
  minimum: 0
  maximum: 1000
```

#### Boolean

```yaml
# True/false
has_images: {type: "boolean"}
is_published: {type: "boolean"}
```

#### Enum

```yaml
# Verbose syntax
status:
  enum: ["draft", "review", "published", "archived"]

# Shorthand (array = enum)
priority: ["low", "medium", "high", "urgent"]
```

#### Enum List (Multiple Selection)

```yaml
# Simple syntax - returns array of values
topics:
  enum_list: ["tech", "science", "health", "finance", "politics"]
  min_items: 1      # Must select at least 1 (optional, default: 0)
  max_items: 3      # Can select at most 3 (optional, default: unlimited)
  unique_items: true  # No duplicates allowed (optional, default: true)

# Alternative syntax using type
tags:
  type: enum_list
  choices: ["urgent", "review", "archive", "followup"]
  max_items: 2
  # unique_items defaults to true - duplicates are automatically removed

# With case-insensitive matching and allowing duplicates
categories:
  enum_list: ["Article", "Report", "Memo", "Email"]
  case_sensitive: false  # "article" will match "Article"
  min_items: 1
  unique_items: false  # Allow duplicates if needed (rare use case)
```

### Complex Types

#### Arrays

```yaml
# Simple array
tags:
  type: "array"
  items: {type: "string"}
  minItems: 1
  maxItems: 10
  uniqueItems: true

# Array of objects
authors:
  type: "array"
  items:
    type: "object"
    properties:
      name: {type: "string"}
      affiliation: {type: "string"}
      is_corresponding: {type: "boolean"}
```

#### Objects

```yaml
# Nested structure
metadata:
  type: "object"
  properties:
    publication_date: {type: "string"}
    journal: {type: "string"}
    doi: {type: "string", pattern: "^10\\.\\d{4,}/.+$"}
  required: ["publication_date"]

# Deeply nested
analysis:
  type: "object"
  properties:
    sentiment:
      type: "object"
      properties:
        overall: {enum: ["positive", "negative", "neutral"]}
        confidence: {type: "number", minimum: 0, maximum: 1}
        aspects:
          type: "array"
          items:
            type: "object"
            properties:
              aspect: {type: "string"}
              sentiment: {enum: ["positive", "negative", "neutral"]}
```

## Advanced Schemas

### Language Validation

Enforce language requirements using custom validators:

```yaml
schema:
  chinese_summary:
    type: "string"
    x-language: "chinese"  # Must contain Chinese characters
    maxLength: 500
  
  english_translation:
    type: "string"
    x-language: "english"  # ASCII characters only
  
  mixed_content:
    type: "string"  # No language restriction
```

### Conditional Schemas

Use schema composition:

```yaml
schema:
  document_type: {enum: ["article", "report", "memo"]}
  
  # Different fields based on type
  article_metadata:
    type: "object"
    properties:
      journal: {type: "string"}
      peer_reviewed: {type: "boolean"}
  
  report_metadata:
    type: "object"  
    properties:
      department: {type: "string"}
      classification: {enum: ["public", "internal", "confidential"]}
```

### Default Values

```yaml
schema:
  status:
    type: "string"
    enum: ["draft", "review", "published"]
    default: "draft"
  
  priority:
    type: "integer"
    minimum: 1
    maximum: 5
    default: 3
```

### Schema Reuse (Via YAML Anchors)

```yaml
# Define reusable schema components
definitions:
  sentiment_enum: &sentiment_enum
    enum: ["very_positive", "positive", "neutral", "negative", "very_negative"]
  
  confidence_score: &confidence_score
    type: "number"
    minimum: 0
    maximum: 1

enrichments:
  - name: quick_sentiment
    schema:
      sentiment: *sentiment_enum
  
  - name: detailed_sentiment
    schema:
      overall_sentiment: *sentiment_enum
      confidence: *confidence_score
      aspect_sentiments:
        type: "array"
        items:
          type: "object"
          properties:
            aspect: {type: "string"}
            sentiment: *sentiment_enum
            confidence: *confidence_score
```

## Migration from XML

If you're using the deprecated XML schema system, here's how to migrate:

### Old XML Schema

```yaml
xml_schemas:
  legal_extraction:
    root: document
    elements:
      case_type:
        type: enum
        values: [civil, criminal]
      parties:
        type: array
        table: case_parties
        elements:
          name: {type: text}
          role: {type: enum, values: [plaintiff, defendant]}
```

### New JSON Schema

```yaml
enrichments:
  - name: legal_extraction
    output_table: legal_cases
    schema:
      case_type: ["civil", "criminal"]
      parties:
        type: "array"
        items:
          type: "object"
          properties:
            name: {type: "string"}
            role: ["plaintiff", "defendant"]
```

### Key Differences

1. **No XML parsing**: Direct JSON output
2. **Better validation**: Pydantic models with type checking
3. **Simpler syntax**: Standard JSON Schema
4. **Automatic tables**: No manual SQL required

## Best Practices

### 1. Start Simple

Begin with basic schemas and add complexity gradually:

```yaml
# Start with this
schema:
  category: ["A", "B", "C"]

# Then expand to this
schema:
  category: ["A", "B", "C"]
  confidence: {type: "number", minimum: 0, maximum: 1}
  reasoning: {type: "string", maxLength: 200}
```

### 2. Use Meaningful Names

Choose descriptive field names:

```yaml
# Good
schema:
  publication_year: {type: "integer"}
  peer_review_status: ["pending", "completed", "rejected"]

# Avoid
schema:
  year: {type: "integer"}
  status: ["P", "C", "R"]
```

### 3. Add Descriptions

Document complex fields:

```yaml
schema:
  impact_score:
    type: "integer"
    minimum: 1
    maximum: 10
    description: "Estimated impact on scale 1-10, where 10 is groundbreaking"
```

### 4. Constrain Arrays

Always set limits on arrays:

```yaml
# Good - bounded
topics:
  type: "array"
  items: {type: "string"}
  maxItems: 5
  minItems: 1

# Risky - unbounded
topics:
  type: "array"
  items: {type: "string"}
```

### 5. Validate Enums

Use enums for classification tasks:

```yaml
# Ensures consistent categorization
document_type:
  enum: ["research_paper", "review", "editorial", "letter", "other"]
  description: "Primary document type following journal classifications"
```

### 6. Handle Optional Fields

Be explicit about required vs optional:

```yaml
schema:
  # Required fields (all fields are required by default in JSON Schema)
  title: {type: "string"}
  category: {enum: ["A", "B", "C"]}
  
  # Optional fields (use nullable or set required at object level)
  subtitle:
    type: "string"
    nullable: true  # Can be null
  
  metadata:
    type: "object"
    properties:
      author: {type: "string"}
      date: {type: "string"}
    required: ["author"]  # Only author is required
```

### 7. Plan for Queries

Design schemas with your queries in mind:

```yaml
# Good for filtering
schema:
  publication_year: {type: "integer"}  # Easy to query: WHERE publication_year > 2020
  has_data: {type: "boolean"}          # Easy to query: WHERE has_data = true

# Harder to query (but still possible with JSON functions)
schema:
  metadata:
    type: "object"
    properties:
      year: {type: "integer"}  # Requires: JSON_EXTRACT(metadata, '$.year') > 2020
```

## Troubleshooting

### Schema Validation Errors

If you see validation errors:

```bash
# Use verbose mode to see the exact error
./doctrail.py enrich --config config.yml --enrichments task --verbose

# Common issues:
# - Enum value not in allowed list
# - Number outside min/max range
# - Array with too many items
# - Missing required field
```

### Storage Mode Issues

If unsure which storage mode will be used:

1. Single field → Direct column
2. Multiple fields → Separate table
3. Arrays/objects → Separate table

Force separate table:
```yaml
output_table: my_custom_table  # Explicitly set table name
```

### Performance Considerations

For large-scale processing:

```yaml
# Use simpler schemas when possible
schema:
  # Simple - fast
  category: ["A", "B", "C"]
  
  # Complex - slower but more detailed
  analysis:
    type: "object"
    properties:
      category: ["A", "B", "C"]
      confidence: {type: "number"}
      reasoning: {type: "string"}
      evidence: {type: "array", items: {type: "string"}}
```

## Examples Gallery

### Research Paper Analysis

```yaml
schema:
  methodology: ["experimental", "theoretical", "review", "meta-analysis"]
  
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
    minItems: 1
  
  limitations:
    type: "string"
    maxLength: 500
    nullable: true
```

### Customer Feedback Analysis

```yaml
schema:
  sentiment: 
    enum: ["very_satisfied", "satisfied", "neutral", "dissatisfied", "very_dissatisfied"]
  
  # NEW: Using enum_list for multiple topic selection
  topics:
    enum_list: ["product_quality", "customer_service", "pricing", "delivery", "user_experience", "other"]
    maxItems: 3
    minItems: 1
  
  requires_followup: {type: "boolean"}
  
  priority:
    type: "integer"
    minimum: 1
    maximum: 5
```

### Legal Document Extraction

```yaml
schema:
  case_info:
    type: "object"
    properties:
      case_number: {type: "string", pattern: "^\\d{4}-[A-Z]{2}-\\d+$"}
      jurisdiction: {type: "string"}
      case_type: ["civil", "criminal", "administrative", "constitutional"]
  
  parties:
    type: "array"
    items:
      type: "object"
      properties:
        name: {type: "string"}
        role: ["plaintiff", "defendant", "witness", "counsel"]
        represented_by: 
          type: "string"
          nullable: true
  
  rulings:
    type: "array"
    items:
      type: "object"
      properties:
        issue: {type: "string"}
        decision: ["granted", "denied", "partially_granted", "dismissed"]
        reasoning_summary: 
          type: "string"
          maxLength: 500
```

---

Ready to build structured enrichments? Check out:
- [Configuration Guide](configuration.md) for full syntax
- [Examples](examples.md) for real-world use cases
- [API Reference](api-reference.md) for programmatic access
## Multi-Table Enrichments

Doctrail supports combining data from multiple tables using sha1 as the universal key:

### Basic Multi-Table Pattern

```yaml
sql_queries:
  # SQL returns sha1 values (not rowids)
  docs_to_review: |
    SELECT sha1 FROM initial_analysis
    WHERE confidence < 0.7
    
enrichments:
  - name: review_analysis
    input:
      query: docs_to_review
      input_columns:
        # table.column syntax for multi-table inputs
        - "documents.title"
        - "documents.raw_content:2000"
        - "initial_analysis.sentiment"
        - "initial_analysis.confidence"
        - "metadata.publication_date"
    output_table: refined_analysis
    schema:
      sentiment: {enum: ["positive", "negative", "neutral"]}
      confidence: {type: "number", minimum: 0, maximum: 1}
      changes_made: {type: "string"}
```

### How It Works

1. **SQL Query**: Returns sha1 values to process
2. **Data Fetching**: For each sha1, fetches columns from specified tables
3. **Missing Data**: Returns null if sha1 not found in a table
4. **No JOINs Needed**: Doctrail handles the multi-table fetching internally

### Benefits

- **Single YAML**: One config for multi-stage pipelines
- **Flexible**: Easy to add/remove tables and columns
- **Reusable**: Same enrichment config works with different SQL queries
- **Review Workflows**: Perfect for refining previous analyses

### Advanced Example: Model Comparison

```yaml
enrichments:
  - name: compare_models
    input:
      query: all_docs
      input_columns:
        - "documents.title"
        - "analysis_gpt4.sentiment"      # From GPT-4 analysis
        - "analysis_gpt4.confidence"
        - "analysis_claude.sentiment"    # From Claude analysis
        - "analysis_claude.confidence"
    output_table: model_comparison
    schema:
      best_sentiment: {enum: ["positive", "negative", "neutral"]}
      agreement: {type: "boolean"}
      confidence_diff: {type: "number"}
    prompt: |
      Compare the two model analyses and determine:
      1. Most likely correct sentiment
      2. Whether models agree
      3. Confidence difference (abs value)
```

## Multi-Model Comparison

Doctrail supports running the same enrichment with multiple models for comparison:

```yaml
enrichments:
  - name: analysis  
    model: [gpt-4o-mini, gemini-2.5-flash]  # List of models
    output_table: analysis_results  # Required for multi-model
    schema:
      sentiment: {enum: ["positive", "negative", "neutral"]}
      confidence: {type: "number", minimum: 0, maximum: 1}
```

Key points:
- Only works with derived tables (`output_table` specified)
- Creates `model_used` column automatically
- Each (sha1, model) combination stored separately
- Easy comparison queries across models

See the [Multi-Model Feature Guide](multi_model_feature.md) for detailed documentation.