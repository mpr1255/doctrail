# Multi-Model Comparison Feature

DocTrail now supports running the same enrichment with multiple models for comparison purposes. This is particularly useful for:

- Comparing output quality between models
- Testing new models against established baselines
- Cost/performance optimization
- Ensuring consistency across different LLM providers

## How It Works

### Configuration

Instead of specifying a single model, you can provide a list:

```yaml
enrichments:
  - name: my_enrichment
    model: 
      - gpt-4o-mini
      - gemini-2.5-flash
      - gpt-4o  # Compare mini vs full model
    output_table: my_enrichment_results  # Required for multi-model
```

### Table Schema

For derived tables (those with `output_table` specified), DocTrail automatically:

1. Adds a `model_used` column
2. Creates a composite unique constraint on `(sha1, model_used)`
3. Stores each model's output as a separate row

Example table structure:
```sql
CREATE TABLE my_enrichment_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha1 TEXT NOT NULL,
    model_used TEXT NOT NULL,
    enrichment_id TEXT,
    -- your schema columns here --
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sha1, model_used)
);
```

### Deduplication

- Each `(sha1, model)` combination is processed once
- Use `--overwrite` to reprocess existing model outputs
- Progress tracking shows which model is currently running

### Important Notes

1. **Derived Tables Only**: Multi-model comparison only works with derived tables (`output_table` specified). Direct column enrichments on the main table don't support multiple models.

2. **Model Support**: Currently supports:
   - OpenAI models (gpt-4o, gpt-4o-mini, etc.)
   - Google Gemini models (gemini-2.5-flash, gemini-2.0-flash)

3. **Structured Outputs**: Only OpenAI models support structured outputs. Gemini models use JSON parsing.

## Example: Comparing Models

```yaml
enrichments:
  - name: sentiment_analysis
    model: [gpt-4o-mini, gemini-2.5-flash]
    output_table: sentiment_results
    
    schema:
      sentiment:
        enum: ["positive", "negative", "neutral"]
      confidence:
        type: "number"
        minimum: 0
        maximum: 1
```

## Querying Results

To compare outputs across models:

```sql
-- Compare sentiment analysis results
SELECT 
  s1.sha1,
  s1.sentiment as gpt_sentiment,
  s2.sentiment as gemini_sentiment,
  s1.confidence as gpt_confidence,
  s2.confidence as gemini_confidence,
  CASE 
    WHEN s1.sentiment = s2.sentiment THEN 'agree'
    ELSE 'disagree'
  END as agreement
FROM sentiment_results s1
JOIN sentiment_results s2 
  ON s1.sha1 = s2.sha1
WHERE s1.model_used = 'gpt-4o-mini'
  AND s2.model_used = 'gemini-2.5-flash';
```

## Migration Notes

Existing tables can be migrated to support multi-model:

1. The system automatically adds `model_used` column when needed
2. Existing data won't have `model_used` set (will be NULL)
3. New enrichments will properly track the model

To backfill model information for existing data:
```sql
-- Update based on enrichment_responses audit table
UPDATE my_table 
SET model_used = (
  SELECT model_used 
  FROM enrichment_responses 
  WHERE enrichment_responses.sha1 = my_table.sha1
  AND enrichment_name = 'my_enrichment'
  ORDER BY created_at DESC 
  LIMIT 1
)
WHERE model_used IS NULL;
```