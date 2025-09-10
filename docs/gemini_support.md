# Gemini Model Support

Doctrail now supports Google's Gemini models with full structured output capabilities, enabling you to use Gemini alongside OpenAI models for enrichment tasks.

## Supported Gemini Models

- `gemini-2.0-flash` - Fast, efficient model with 1M token context
- `gemini-2.5-flash` - Latest flash model with enhanced capabilities
- `gemini-1.5-flash` - Previous generation flash model
- `gemini-1.5-pro` - Pro model with 2M token context

## Configuration

### API Key Setup

Set your Gemini API key as an environment variable:

```bash
export GEMINI_API_KEY="your-api-key-here"
# or
export GOOGLE_API_KEY="your-api-key-here"
```

### Using Gemini Models

Simply specify a Gemini model in your enrichment configuration:

```yaml
enrichments:
  - name: document_analysis
    model: gemini-2.0-flash  # Use Gemini instead of GPT
    schema:
      topic: {enum: ["tech", "health", "finance", "other"]}
      summary: {type: "string", maxLength: 200}
    prompt: "Analyze this document"
```

## Structured Output Support

Gemini models now support structured outputs using the same Pydantic schema definitions as OpenAI:

```yaml
schema:
  # Simple enum
  category:
    enum: ["A", "B", "C"]
  
  # Numeric with constraints
  score:
    type: "number"
    minimum: 0
    maximum: 100
  
  # Arrays with items
  keywords:
    type: "array"
    items:
      type: "string"
    maxItems: 5
  
  # Nested objects
  metadata:
    type: "object"
    properties:
      author: {type: "string"}
      date: {type: "string"}
```

## Multi-Model Comparison

You can run the same enrichment with multiple models (including mixing OpenAI and Gemini) for comparison:

```yaml
enrichments:
  - name: sentiment_analysis
    model: [gpt-4o-mini, gemini-2.0-flash]  # Compare both
    output_table: sentiment_comparisons  # Required for multi-model
    schema:
      sentiment: {enum: ["positive", "negative", "neutral"]}
      confidence: {type: "number", minimum: 0, maximum: 1}
```

This will create results in the output table with a `model_used` column to distinguish between models.

## Context Window Sizes

Gemini models have generous context windows:
- Flash models: 1M tokens (~4M characters)
- Pro models: 2M tokens (~8M characters)

This makes them excellent for processing long documents without truncation.

## Example: Complete Multi-Model Configuration

```yaml
database: "analysis.db"

enrichments:
  # Multi-model comparison
  - name: document_classification
    description: "Compare classification across models"
    model: [gpt-4o-mini, gemini-2.0-flash, gemini-2.5-flash]
    output_table: classification_results
    input:
      query: "SELECT rowid, sha1, content FROM documents"
      input_columns: ["content:2000"]
    schema:
      document_type:
        enum: ["research", "news", "blog", "report", "other"]
      confidence:
        type: "number"
        minimum: 0
        maximum: 1
      key_topics:
        type: "array"
        items: {type: "string"}
        maxItems: 3
    prompt: |
      Classify this document and identify its key topics.
      Be specific and accurate in your classification.

# Query results by model
exports:
  - name: model_comparison
    query: |
      SELECT 
        sha1,
        model_used,
        document_type,
        confidence,
        key_topics
      FROM classification_results
      ORDER BY sha1, model_used
    format: csv
```

## Best Practices

1. **Model Selection**: 
   - Use `gemini-2.0-flash` for fast, cost-effective processing
   - Use `gemini-2.5-flash` for latest capabilities
   - Use GPT models for specific OpenAI features

2. **Structured Output**: Gemini handles complex schemas well, including nested objects and arrays

3. **Multi-Model Workflows**: Great for:
   - Comparing model performance
   - Validating results across providers
   - A/B testing different models

4. **Large Documents**: Gemini's 1M+ context window eliminates most truncation needs

## Limitations

- Gemini models require proper API key configuration
- Response format must be JSON when using structured output
- Some advanced Pydantic validators may not be fully supported