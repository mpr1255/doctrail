# Enum List Feature Documentation

## Overview

The `enum_list` schema type allows LLMs to return multiple categorical values from a predefined list. This is useful for multi-label classification tasks where documents may belong to multiple categories.

## Syntax

### Basic Syntax

```yaml
schema:
  topics:
    enum_list: ["tech", "health", "finance", "politics", "science"]
```

### With Constraints

```yaml
schema:
  tags:
    enum_list: ["urgent", "important", "review", "archive", "followup"]
    min_items: 1      # Must select at least 1 tag
    max_items: 3      # Can select at most 3 tags
    case_sensitive: false  # Allow case-insensitive matching
    unique_items: true  # No duplicates (default behavior)
```

### Alternative Type Syntax

```yaml
schema:
  categories:
    type: enum_list
    choices: ["Article", "Report", "Memo", "Email", "Other"]
    min_items: 1
```

## Example Usage

### Document Tagging

```yaml
enrichments:
  - name: tag_documents
    description: "Apply multiple tags to documents"
    input:
      query: all_documents
      input_columns: ["content"]
    output_column: document_tags
    schema:
      enum_list: ["technical", "business", "legal", "financial", "personal", "confidential"]
      min_items: 1
      max_items: 3
    prompt: |
      Analyze this document and select 1-3 tags that best describe its content and nature.
```

### Language Detection

```yaml
enrichments:
  - name: detect_languages
    description: "Identify all languages in the document"
    input:
      query: multilingual_docs
      input_columns: ["content"]
    output_column: languages
    schema:
      enum_list: ["english", "spanish", "french", "german", "chinese", "japanese", "other"]
      min_items: 1
    prompt: |
      Identify all languages present in this document.
```

### Research Methods Classification

```yaml
enrichments:
  - name: classify_methods
    description: "Identify research methods used"
    input:
      query: research_papers
      input_columns: ["abstract", "methodology"]
    output_column: research_methods
    schema:
      enum_list: [
        "qualitative",
        "quantitative", 
        "mixed_methods",
        "experimental",
        "observational",
        "meta_analysis",
        "case_study",
        "survey"
      ]
      max_items: 4
    prompt: |
      Identify the research methods used in this paper.
```

## Storage

The enum_list values are stored as JSON arrays in the database:

```sql
-- Example data
SELECT sha1, document_tags FROM documents;
-- Results:
-- abc123 | ["technical", "confidential"]
-- def456 | ["business", "financial", "legal"]
-- ghi789 | ["personal"]
```

## Querying Results

To query documents with specific tags:

```sql
-- Find documents with "technical" tag
SELECT * FROM documents 
WHERE json_extract(document_tags, '$') LIKE '%"technical"%';

-- Find documents with either "legal" or "financial" tags
SELECT * FROM documents 
WHERE json_extract(document_tags, '$') LIKE '%"legal"%' 
   OR json_extract(document_tags, '$') LIKE '%"financial"%';

-- Count tags per document
SELECT sha1, json_array_length(document_tags) as tag_count 
FROM documents;
```

## Validation

The schema enforces:
- All returned values must be from the allowed list
- Minimum number of items (if specified)
- Maximum number of items (if specified)
- Case sensitivity (configurable)
- **Unique items by default** (duplicates are automatically removed)

Invalid responses will be rejected with clear error messages.

### Duplicate Handling

**Automatic Deduplication**: Doctrail automatically removes duplicate values from enum_list responses:
- If model returns `["tech", "health", "tech"]`, it's stored as `["tech", "health"]`
- This happens post-processing, not as validation
- No errors are thrown for duplicates - they're simply cleaned
- This approach is more forgiving to LLM outputs

**Note**: The `unique_items` parameter is not supported in the YAML schema as deduplication is always applied automatically.

## Model Instructions

The enum_list schema generates specific instructions for the LLM:

```
CRITICAL INSTRUCTION: You MUST respond with a JSON array containing ONLY these allowed values: "tech", "health", "finance", "politics", "science"

Format your response as a JSON array, for example: ["tech", "finance"]
- You MUST return between 1 and 3 items
- Duplicates are allowed but will be automatically removed
- Ensure you have at least 1 UNIQUE values

DO NOT:
- Add any explanation or additional text
- Use values not in the allowed list
- Use different capitalization
- Add punctuation to the values
- Use synonyms or variations

Valid values: "tech", "health", "finance", "politics", "science"
```

Note: The instructions automatically adapt based on your schema configuration (min/max items, unique_items setting).

## Best Practices

1. **Set Reasonable Limits**: Use `min_items` and `max_items` to prevent overly broad or narrow tagging
2. **Choose Clear Categories**: Use unambiguous category names that don't overlap
3. **Consider Case Sensitivity**: Set `case_sensitive: false` for more flexible matching
4. **Use with Arrays**: Enum_list returns an array, so plan your queries accordingly
5. **Provide Examples**: In your prompts, give examples of when to use each category

## Migration from Array of Enums

If you were using the more complex array syntax:

```yaml
# Old way
topics:
  type: "array"
  items:
    enum: ["tech", "health", "finance"]
  maxItems: 3
```

You can now use the simpler enum_list:

```yaml
# New way
topics:
  enum_list: ["tech", "health", "finance"]
  max_items: 3
```

Both produce the same result, but enum_list is clearer and more concise.