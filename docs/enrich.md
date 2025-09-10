# Document Enrichment Guide

This guide covers all enrichment functionality in doctrail - how to analyze and extract insights from your ingested documents using Large Language Models (LLMs).

## Quick Start

```bash
# Basic enrichment with single model
./doctrail.py enrich --config ./config.yml --enrichments sentiment --db-path ./research.db

# Multi-model comparison
./doctrail.py enrich --config ./config.yml --enrichments analysis --db-path ./research.db
```

## Core Concepts

### What is Enrichment?
Enrichment uses LLMs to:
1. **Analyze** document content systematically
2. **Extract** structured data according to your specifications  
3. **Store** results for further analysis and export
4. **Compare** results across different AI models

### Multi-Model Support
Doctrail supports running the same enrichment with multiple models for comparison:

```yaml
# In your config.yml
enrichments:
  sentiment_analysis:
    model: [gpt-4o-mini, gemini-2.5-flash]  # Multiple models
    prompt: "Analyze the sentiment of this document..."
    storage_mode: separate_table
    # ... other config
```

**Key Features:**
- **Model-based deduplication**: Each model's results stored separately
- **Parallel processing**: Models run concurrently for speed
- **Derived tables only**: Multi-model support only works with `separate_table` storage mode
- **Progress tracking**: Shows progress per model

## Command Reference

### Basic Command Structure
```bash
./doctrail.py enrich [OPTIONS]
```

### Required Options
- `--config PATH` - YAML configuration file path
- `--db-path PATH` - SQLite database file (must contain ingested documents)

### Enrichment Selection
```bash
# Run specific enrichments
--enrichments sentiment,keywords,classification

# Run all enrichments in config
# (omit --enrichments flag)
```

### Processing Options

#### Model Override
```bash
--model gpt-4o-mini    # Override model specified in config
```

#### Batch Processing
```bash
--batch-size 50        # Process documents in batches (default: 100)
--limit 10             # Only process first 10 documents (for testing)
```

#### Data Management
```bash
--overwrite           # Re-process documents that already have results
--where "date > '2024-01-01'"    # SQL filter for which documents to process
```

**Important Note about --overwrite mode:**
- When using `--overwrite`, ALL rows returned by the query will be processed
- The `enrichment_responses` table stores EVERY LLM API call as an audit trail
- Each processed document creates a new entry in `enrichment_responses` with a unique `enrichment_id`
- The actual enrichment data (in output tables or columns) will be updated/replaced
- This audit trail allows you to track all LLM calls, costs, and responses over time

#### Performance Options
```bash
--truncate            # Automatically truncate long documents to fit model context
--verbose             # Show detailed processing information
--debug               # Show debug-level information
```

## Configuration File Format

### Basic Structure
```yaml
enrichments:
  enrichment_name:
    model: gpt-4o-mini                    # Single model
    # OR
    model: [gpt-4o-mini, gemini-2.5-flash]  # Multiple models (derived tables only)
    
    prompt: "Your analysis prompt here..."
    
    input:
      input_columns: [content, title]     # Which document fields to analyze
    
    output:
      storage_mode: separate_table        # or direct_column
      output_table: analysis_results      # For separate_table mode
      key_column: sha1                    # Primary key for linking
      
    schema:                               # Optional: structured output schema
      type: object
      properties:
        sentiment: 
          type: string
          enum: [positive, negative, neutral]
        confidence:
          type: number
          minimum: 0
          maximum: 1
```

### Storage Modes

#### Separate Table (`separate_table`)
Creates a dedicated table for enrichment results:

```yaml
output:
  storage_mode: separate_table
  output_table: sentiment_analysis
  key_column: sha1
```

**Benefits:**
- **Multi-model support**: Can store results from multiple models
- **Structured data**: Each schema field becomes a table column
- **Clean separation**: Enrichment data separate from source documents
- **Easy querying**: JOIN with documents table using key_column

#### Direct Column (`direct_column`)  
Stores results directly in the documents table:

```yaml
output:
  storage_mode: direct_column
  output_columns: [sentiment]
```

**Benefits:**
- **Simple structure**: Everything in one table
- **Quick access**: No JOINs needed
- **Legacy compatibility**: Works with older configurations

### Schema-Driven Enrichments

Define structured output schemas for consistent, validated results:

```yaml
enrichments:
  extract_entities:
    model: gpt-4o-mini
    prompt: "Extract key entities from this document..."
    
    input:
      input_columns: [content]
    
    output:
      storage_mode: separate_table
      output_table: entities
      key_column: sha1
      
    schema:
      type: object
      properties:
        people:
          type: array
          items:
            type: string
          description: "Names of people mentioned"
        organizations:
          type: array  
          items:
            type: string
          description: "Organizations mentioned"
        locations:
          type: array
          items:
            type: string
          description: "Locations mentioned"
        key_themes:
          type: array
          items:
            type: string
          description: "Main themes or topics"
        confidence_score:
          type: number
          minimum: 0
          maximum: 1
          description: "Confidence in extraction quality"
```

### Input Configuration

#### Basic Input Columns
```yaml
input:
  input_columns: [content, title, author]
```

#### Column Limits
Limit character count per column to manage context length:

```yaml
input:
  input_columns: 
    - content:2000      # First 2000 characters of content
    - title:200         # First 200 characters of title
    - summary          # Full summary (no limit)
```

#### File Appending
Include additional content from external files:

```yaml
append_file: ./prompts/additional_instructions.md
```

## Supported Models

### OpenAI Models
- `gpt-4o-mini` - Fast, cost-effective, good for most tasks
- `gpt-4o` - More capable, higher cost
- `gpt-4` - Previous generation
- `gpt-3.5-turbo` - Legacy model

### Google Gemini Models
- `gemini-2.5-flash` - Fast, efficient, good structured outputs
- `gemini-2.0-flash` - Alternative fast model
- `gemini-1.5-pro` - Higher capability model

**API Keys Required:**
- OpenAI: Set `OPENAI_API_KEY` environment variable
- Gemini: Set `GEMINI_API_KEY` or `GOOGLE_AI_API_KEY` environment variable

## Common Configuration Examples

### Sentiment Analysis
```yaml
enrichments:
  sentiment:
    model: gpt-4o-mini
    prompt: |
      Analyze the sentiment and emotional tone of this document.
      Consider both explicit statements and implicit tone.
      
    input:
      input_columns: [content]
      
    output:
      storage_mode: separate_table
      output_table: sentiment_analysis
      key_column: sha1
      
    schema:
      type: object
      properties:
        overall_sentiment:
          type: string
          enum: [very_positive, positive, neutral, negative, very_negative]
        confidence:
          type: number
          minimum: 0
          maximum: 1
        key_phrases:
          type: array
          items:
            type: string
          description: "Phrases that influenced sentiment"
        emotional_tone:
          type: string
          enum: [angry, sad, happy, excited, fearful, neutral]
```

### Entity Extraction
```yaml
enrichments:
  entities:
    model: [gpt-4o-mini, gemini-2.5-flash]  # Multi-model comparison
    prompt: |
      Extract and categorize all named entities from this document.
      Focus on people, organizations, locations, and dates.
      
    input:
      input_columns: 
        - content:3000    # Limit to first 3000 characters
        - title
        
    output:
      storage_mode: separate_table
      output_table: extracted_entities  
      key_column: sha1
      
    schema:
      type: object
      properties:
        people:
          type: array
          items:
            type: object
            properties:
              name: {type: string}
              role: {type: string}
              context: {type: string}
        organizations:
          type: array
          items:
            type: object
            properties:
              name: {type: string}
              type: {type: string}
              context: {type: string}
        locations:
          type: array
          items:
            type: string
        important_dates:
          type: array
          items:
            type: string
```

### Topic Classification
```yaml
enrichments:
  classification:
    model: gpt-4o-mini
    prompt: |
      Classify this document into relevant topic categories.
      Consider the main subject matter, target audience, and purpose.
      
    input:
      input_columns: [content, title, summary]
      
    output:
      storage_mode: separate_table
      output_table: document_classification
      key_column: sha1
      
    schema:
      type: object
      properties:
        primary_topic:
          type: string
          enum: [politics, economics, technology, health, education, environment, social, legal, other]
        secondary_topics:
          type: array
          items:
            type: string
        document_type:
          type: string  
          enum: [research_paper, news_article, opinion_piece, report, policy_document, other]
        target_audience:
          type: string
          enum: [academic, general_public, professionals, policymakers, other]
        complexity_level:
          type: string
          enum: [basic, intermediate, advanced, expert]
```

### Multi-Model Comparison Example
```yaml
enrichments:
  bias_analysis:
    model: [gpt-4o-mini, gemini-2.5-flash]  # Compare across models
    prompt: |
      Analyze this document for potential bias, including:
      - Political lean (if any)
      - Source credibility indicators  
      - Language that suggests bias
      - Overall objectivity assessment
      
    input:
      input_columns: [content, title, author]
      
    output:
      storage_mode: separate_table      # Required for multi-model
      output_table: bias_analysis
      key_column: sha1
      
    schema:
      type: object
      properties:
        political_lean:
          type: string
          enum: [left, center_left, center, center_right, right, unclear]
        bias_indicators:
          type: array
          items:
            type: string
        objectivity_score:
          type: number
          minimum: 0
          maximum: 10
        credibility_assessment:
          type: string
          enum: [high, medium, low, unclear]
```

## Running Enrichments

### Single Enrichment
```bash
./doctrail.py enrich \
    --config ./analysis_config.yml \
    --enrichments sentiment \
    --db-path ./documents.db
```

### Multiple Enrichments
```bash
./doctrail.py enrich \
    --config ./analysis_config.yml \
    --enrichments sentiment,entities,classification \
    --db-path ./documents.db
```

### All Enrichments
```bash
./doctrail.py enrich \
    --config ./analysis_config.yml \
    --db-path ./documents.db
```

### Testing with Limited Data
```bash
./doctrail.py enrich \
    --config ./analysis_config.yml \
    --enrichments sentiment \
    --db-path ./documents.db \
    --limit 5 \
    --verbose
```

### Filtering Documents
```bash
./doctrail.py enrich \
    --config ./analysis_config.yml \
    --enrichments analysis \
    --db-path ./documents.db \
    --where "author LIKE '%smith%'" \
    --verbose
```

### Overwriting Existing Results
```bash
./doctrail.py enrich \
    --config ./analysis_config.yml \
    --enrichments sentiment \
    --db-path ./documents.db \
    --overwrite
```

## Best Practices

### Configuration Design
- **Start simple**: Begin with basic prompts and schemas, then iterate
- **Use examples**: Include example outputs in your prompts
- **Test small**: Use `--limit` to test on small datasets first
- **Multi-model strategically**: Only use multiple models when comparison adds value

### Performance Optimization
- **Batch appropriately**: Larger batches are more efficient but use more memory
- **Enable truncation**: Use `--truncate` for long documents to avoid context limits
- **Monitor costs**: Track API usage, especially with multiple models
- **Use appropriate models**: gpt-4o-mini for most tasks, stronger models for complex analysis

### Schema Design
- **Be specific**: Use enums and constraints to ensure consistent outputs
- **Include descriptions**: Help the LLM understand what you want
- **Validate types**: Use proper JSON Schema types (string, number, array, object)
- **Plan for edge cases**: Consider how to handle unclear or missing information

### Data Management
- **Backup before major runs**: Copy your database before large enrichment jobs
- **Monitor progress**: Use `--verbose` for long-running jobs
- **Handle failures gracefully**: Individual document failures won't stop the batch
- **Review results**: Spot-check outputs to ensure quality

## Troubleshooting

### Common Issues

#### "No enrichment strategy found"
- Check enrichment name spelling in config vs command line
- Verify YAML syntax is correct
- Ensure enrichment is properly defined under `enrichments:` key

#### "Multi-model not supported for direct_column"
- Multi-model only works with `storage_mode: separate_table`
- Change to separate_table or use single model

#### Model authentication errors
- Verify API keys are set: `echo $OPENAI_API_KEY`
- Check API key permissions and quotas
- Ensure internet connectivity

#### Context length errors
- Use `--truncate` flag to automatically handle long documents
- Reduce input column character limits
- Use models with larger context windows

#### Schema validation failures
- Check that your schema matches expected LLM output format
- Use `--verbose` to see actual LLM responses
- Simplify schema for testing, then add complexity

### Performance Issues

#### Slow processing
- Reduce batch size with `--batch-size 25`
- Use faster models (gpt-4o-mini vs gpt-4o)
- Enable truncation to reduce context length
- Check internet connection speed

#### Memory issues
- Lower batch size
- Process in smaller chunks using `--limit` and `--where` filters
- Close other applications to free memory

### Getting Help
- Use `./doctrail.py enrich --help` for command reference
- Check `/tmp/doctrail.log` for detailed error messages  
- Test configurations with `--limit 1 --verbose` first
- Review example configurations in `./examples/` directory

## Next Steps

After enrichment:
1. **Query your results**: Use `./doctrail.py query` to explore enriched data
2. **Export for analysis**: Use export functionality to get data in CSV/JSON format
3. **Iterate and improve**: Refine prompts and schemas based on results
4. **Scale up**: Run on full datasets once you're satisfied with quality

See the [export guide](./export.md) for next steps in data extraction and analysis.