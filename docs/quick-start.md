# Quick Start Guide

Get up and running with Doctrail in 5 minutes!

## Prerequisites

- Python 3.8+ with UV package manager
- SQLite3
- An OpenAI API key (for GPT models) or Google AI API key (for Gemini models)

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/doctrail.git
cd doctrail

# Install dependencies with UV
uv pip install -r requirements.txt

# Make the script executable
chmod +x doctrail.py

# Set your API key
export OPENAI_API_KEY="your-api-key-here"
# OR for Gemini:
export GOOGLE_AI_API_KEY="your-gemini-key-here"
```

## Your First Enrichment

### Step 1: Create a Test Database

First, let's create a simple SQLite database with some documents:

```bash
# Create a test database
sqlite3 test.db << 'EOF'
CREATE TABLE documents (
    sha1 TEXT PRIMARY KEY,
    filename TEXT,
    raw_content TEXT
);

INSERT INTO documents (sha1, filename, raw_content) VALUES 
    ('abc123', 'doc1.txt', 'The new product launch was incredibly successful. Customer feedback has been overwhelmingly positive.'),
    ('def456', 'doc2.txt', 'Technical issues continue to plague the system. Users report frequent crashes and data loss.'),
    ('ghi789', 'doc3.txt', 'Market analysis shows neutral trends. Competition remains stable with no major changes.');
.exit
EOF
```

### Step 2: Create a Configuration File

Create `config.yml`:

```yaml
# Basic Doctrail configuration
database: ./test.db
default_model: gpt-4o-mini

# Define a simple sentiment analysis enrichment
enrichments:
  - name: sentiment
    description: "Analyze document sentiment"
    table: documents
    input:
      query: "SELECT rowid, sha1, * FROM documents"
      input_columns: ["raw_content"]
    output_column: sentiment
    prompt: |
      Analyze the sentiment of this text.
      Respond with only one word: positive, negative, or neutral.
```

### Step 3: Run the Enrichment

```bash
# Run sentiment analysis on all documents
doctrail enrich --config config.yml --enrichments sentiment

# Output:
# 🚀 Starting enrichment task: sentiment
# ➕ APPEND MODE: Will skip rows that already have values
# 📊 Retrieved 3 rows from database
# 🔄 Processing 3 rows...
# 🤖 sentiment 100%|████████████| 3/3 docs [00:02<00:00,  1.23 docs/s]
```

### Step 4: Check the Results

```bash
# View the enriched data
sqlite3 test.db "SELECT filename, sentiment FROM documents"

# Output:
# doc1.txt|positive
# doc2.txt|negative
# doc3.txt|neutral
```

## A More Advanced Example

Let's create a schema-driven enrichment for structured data extraction:

### Create an Advanced Config

Create `advanced_config.yml`:

```yaml
database: ./test.db
default_model: gpt-4o-mini

# Define reusable SQL queries
sql_queries:
  unprocessed: "SELECT rowid, sha1, * FROM documents WHERE analysis_complete IS NULL"

# Schema-driven enrichment with structured output
enrichments:
  - name: document_analysis
    description: "Comprehensive document analysis"
    input:
      query: unprocessed
      input_columns: ["raw_content"]
    output_table: document_insights  # Separate table for complex data
    schema:
      sentiment: 
        enum: ["very_positive", "positive", "neutral", "negative", "very_negative"]
      key_topics:
        type: array
        items: {type: string}
        maxItems: 3
      action_required:
        type: boolean
      priority_score:
        type: integer
        minimum: 1
        maximum: 5
    prompt: |
      Analyze this document and provide:
      1. Detailed sentiment (very_positive to very_negative)
      2. Up to 3 key topics discussed
      3. Whether action is required (true/false)
      4. Priority score (1-5, where 5 is highest priority)

# Export configuration
exports:
  - name: analysis_report
    description: "Export analysis results"
    query: |
      SELECT 
        d.filename,
        d.raw_content,
        di.sentiment,
        di.key_topics,
        di.action_required,
        di.priority_score
      FROM documents d
      JOIN document_insights di ON d.sha1 = di.sha1
      ORDER BY di.priority_score DESC
    output_file: "analysis_report.csv"
    format: csv
```

### Run the Advanced Analysis

```bash
# Run the analysis
doctrail enrich --config advanced_config.yml --enrichments document_analysis

# Export results
doctrail export --config advanced_config.yml --export-type analysis_report
```

## Working with Real Documents

### Ingest PDF Documents

```bash
# Create a more realistic example with PDFs
mkdir documents
# (Copy some PDF files into the documents directory)

# Ingest the PDFs
doctrail ingest --input-dir ./documents --db-path ./research.db

# Create a config for the research database
cat > research_config.yml << 'EOF'
database: ./research.db
default_model: gpt-4o-mini

enrichments:
  - name: summarize
    description: "Generate document summaries"
    table: documents
    input:
      query: "SELECT rowid, sha1, * FROM documents"
      input_columns: ["raw_content:2000"]  # First 2000 chars only
    output_column: summary
    prompt: |
      Summarize this document in 2-3 sentences.
      Focus on the main points and key findings.
EOF

# Run summarization
doctrail enrich --config research_config.yml --enrichments summarize --limit 5
```

## Key Concepts to Remember

### 1. Append vs Overwrite Mode

By default, Doctrail runs in **append mode** - it skips documents that already have values:

```bash
# First run: processes all documents
doctrail enrich --config config.yml --enrichments sentiment

# Second run: skips documents (nothing to do)
doctrail enrich --config config.yml --enrichments sentiment

# Force reprocessing with --overwrite
doctrail enrich --config config.yml --enrichments sentiment --overwrite
```

### 2. Testing with Limits

Always test on a small sample first:

```bash
# Test on 5 documents
doctrail enrich --config config.yml --enrichments complex_analysis --limit 5

# If results look good, run on all
doctrail enrich --config config.yml --enrichments complex_analysis
```

### 3. Model Selection

Different models have different strengths and costs:

```bash
# Fast and cheap
--model gpt-4o-mini

# More capable but slower
--model gpt-4o

# Google's model (requires GOOGLE_AI_API_KEY)
--model gemini-2.0-flash-exp
```

### 4. Input Column Limits

Prevent token limit errors by truncating input:

```yaml
input_columns: 
  - "raw_content:1000"    # First 1000 characters only
  - "title"               # Full title (no limit)
```

## Next Steps

1. **Explore Examples**: Check out the `examples/` directory for real-world configurations
2. **Read the Docs**: 
   - [Configuration Guide](configuration.md) - Understand all config options
   - [Schema-Driven Enrichment](schema-driven.md) - Learn about structured outputs
   - [CLI Reference](cli-reference.md) - Complete command documentation
3. **Join the Community**: Share your use cases and get help

## Troubleshooting

### "Database not found"
```bash
# Make sure you run ingest first:
doctrail ingest --input-dir ./docs --db-path ./database.db
```

### "No enrichments found"
```bash
# Check your config file has enrichments defined
# Check spelling of enrichment names
doctrail enrich --config config.yml --enrichments sentiment  # exact name
```

### "API key not found"
```bash
# Set your API key
export OPENAI_API_KEY="sk-..."
# Or add to your shell profile for persistence
echo 'export OPENAI_API_KEY="sk-..."' >> ~/.bashrc
```

### Token limit errors
```yaml
# Use input column limits
input_columns: ["raw_content:500"]

# Or use --truncate flag
doctrail enrich --config config.yml --enrichments task --truncate
```

---

Ready to build something amazing? 🚀