# Doctrail


> **⚠️ Research Prototype**: This is a research tool in active development. APIs and configuration formats may change. Suitable for academic research and experimentation.

A command-line tool for enriching SQLite databases using Large Language Models (LLMs). Define enrichment tasks in a YAML configuration file, and the tool will process database content through LLM APIs to extract structured information or generate new content, saving the results back to your database.

## Who's it for?

Doctrail is designed for mixed-methods scholars and researchers who work with large document corpora. If you've ever found yourself:
- Opening hundreds of PDFs one by one to annotate them
- Copying and pasting document excerpts into spreadsheets
- Manually coding documents for qualitative analysis
- Struggling to test theories across thousands of documents
- Needing to extract structured data from unstructured text for quantitative analysis

Then Doctrail can transform your research workflow by putting LLMs in the loop to help you understand, annotate, and extract insights from your documents at scale.

## Research workflows it facilitates

1. **Document Corpus Annotation**: Process thousands of PDFs, Word docs, and web pages to add structured annotations based on your research questions
2. **Theory Testing at Scale**: Apply theoretical frameworks consistently across large document collections
3. **Mixed-Methods Analysis**: Extract structured data from qualitative sources for quantitative analysis
4. **Iterative Research**: Refine your analysis by re-running with different prompts, models, or schemas
5. **Cross-Language Research**: Translate and analyze documents in multiple languages (particularly useful for area studies)

## High level technical details

The tool implements these workflows through:
1. **Document Ingestion**: Bulk import documents into an SQLite database with metadata extraction
2. **SQL-Based Selection**: Use queries to target specific subsets of your corpus
3. **LLM-Powered Analysis**: Send documents through LLM APIs with your research-specific prompts
4. **Structured Storage**: Save responses as new database columns for easy querying
5. **Flexible Export**: Generate reports or datasets from your enriched data

## Features

-   **YAML Configuration:** Define database connections, LLM models, SQL queries, enrichment prompts, output schemas, and export formats in a single config file.
-   **Modular:** Easily define multiple, independent enrichment tasks.
-   **Targeted Processing:** Use SQL queries to select precisely which rows to process for each task.
-   **Schema-Driven JSON:** Define schemas in YAML that automatically generate Pydantic models, ensuring type-safe structured outputs from LLMs.
-   **Dual Storage:** All LLM responses stored as raw JSON for audit trails, plus parsed columns for easy querying.
-   **Flexible Output:** Automatically choose between direct column updates or separate tables based on schema complexity.
-   **Configurable Prompts:** Tailor system and user prompts for specific LLM tasks.
-   **Model Selection:** Choose different LLMs for different tasks (requires API key environment variables, e.g., `OPENAI_API_KEY`).
-   **Batch Processing:** Efficiently handles large numbers of rows.
-   **Progress Tracking:** Uses `tqdm` for progress bars.
-   **Logging:** Detailed logging for debugging (`/tmp/doctrail.log`).
-   **Overwrite Control:** Option to re-run enrichments and overwrite previous results.
-   **Ingestion:** Basic document ingestion capability (using Apache Tika for text extraction).
-   **Plugin System:** Extensible ingestion system with custom connectors for specialized data sources.
-   **Exporting:** Generate reports or datasets from enriched data using templates.

## External dependencies

This tool relies on several external binaries that must be installed on your system and available in your PATH:

### Required
- **Apache Tika** - Document parsing server
  ```bash
  pip install tika
  ```

### Optional (for better performance/quality)
- **pdftotext** (from poppler-utils) - PDF text extraction
  ```bash
  # macOS
  brew install poppler
  
  # Ubuntu/Debian
  sudo apt-get install poppler-utils
  
  # CentOS/RHEL
  sudo yum install poppler-utils
  ```

- **antiword** - Microsoft Word (.doc) text extraction
  ```bash
  # macOS
  brew install antiword
  
  # Ubuntu/Debian
  sudo apt-get install antiword
  
  # CentOS/RHEL
  sudo yum install antiword
  ```

- **mutool** (from mupdf-tools) - Alternative PDF text extraction
  ```bash
  # macOS
  brew install mupdf-tools
  
  # Ubuntu/Debian
  sudo apt-get install mupdf-tools
  
  # CentOS/RHEL
  sudo yum install mupdf
  ```

- **ocrmypdf** - OCR for scanned PDFs
  ```bash
  pip install ocrmypdf
  
  # Also requires tesseract:
  # macOS
  brew install tesseract tesseract-lang-chi-sim
  
  # Ubuntu/Debian
  sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim
  ```

- **Google Chrome** - For advanced HTML/MHTML processing (optional)
  - Download from [chrome.google.com](https://chrome.google.com)

- **w3m** - Text-mode web browser for HTML text extraction fallback
  ```bash
  # macOS
  brew install w3m
  
  # Ubuntu/Debian
  sudo apt-get install w3m
  
  # CentOS/RHEL
  sudo yum install w3m
  ```

### File Type Support Matrix

| File Type | Primary Tool | Fallback | Notes |
|-----------|-------------|----------|--------|
| `.pdf` | pdftotext → mutool → ocrmypdf | Tika | Best quality text extraction |
| `.doc` | antiword | Tika | Legacy Word documents |
| `.docx` | Tika | - | Modern Word documents |
| `.html/.htm` | readability → w3m | Tika | Clean article extraction, w3m fallback for encoding issues |
| `.mhtml/.mht` | mhtml-to-html-py | Tika | Web archive files |
| Others | Tika | - | General document support |

### Installation check

You can verify which tools are available:
```bash
# Check what's installed
which pdftotext antiword mutool ocrmypdf tika
```

## Installation & setup

The script uses `uv` for dependency management defined in the shebang. Ensure `uv` is installed and available in your `PATH`.

1.  **Clone the repository:**
    ```bash
    git clone <repository_url>
    cd sqlite-enricher
    ```
2.  **Set up API Keys (if using LLMs):**
    ```bash
    export OPENAI_API_KEY='your_openai_api_key'
    # Add other keys if using different model providers
    ```
3.  **Create a `config.yaml` file:** (See Configuration section below for details).
4.  **Run the script:**
    ```bash
    doctrail <COMMAND> [OPTIONS]
    ```

## Usage

The tool operates via commands: `enrich`, `ingest`, `export`.

```bash
doctrail --help # Shows main help
doctrail <COMMAND> --help # Shows help for a specific command
```

**Common Options:**

*   `--config FILE`: **Required** for most commands. Path to your `config.yaml` file.
*   `--verbose`: Enable detailed debug logging to the console and log file.
*   `--db-path FILE`: Optionally override the database path specified in the `config.yaml`.

### 1. `enrich` Command

This is the core command for processing data with LLMs.

**Syntax:**

```bash
./doctrail enrich --config <your_config.yaml> --enrichments <task_name_1>,<task_name_2> [OPTIONS]
```

**Required Options:**

*   `--config FILE`: Path to your configuration file.
*   `--enrichments LIST`: Comma-separated list of enrichment task *names* (defined in your `config.yaml`) to run.

**Optional Options:**

*   `--limit INT`: Process only the first N rows returned by the query (useful for testing).
*   `--overwrite`: If specified, existing data in the output column(s) for the processed rows will be overwritten. Otherwise, rows with existing data are skipped.
*   `--table NAME`: Process enrichments only for a specific table defined in the config (useful if an enrichment template applies to multiple tables).

**Examples:**

```bash
# Run a single enrichment task named 'extract_summary' defined in config.yml
doctrail enrich --config config.yml --enrichments extract_summary --verbose

# Run two tasks, 'extract_keywords' and 'translate_title', limiting to 10 rows each
doctrail enrich --config config.yml --enrichments extract_keywords,translate_title --limit 10

# Re-run the 'analyze_sentiment' task and overwrite any previous results
doctrail enrich --config config.yml --enrichments analyze_sentiment --overwrite
```

### 2. `ingest` Command

Ingests documents from various sources into a database table. Supports local directories, Zotero collections, and custom plugins.

**Syntax:**

```bash
# Local files
doctrail ingest --config <your_config.yaml> --input-dir <path/to/docs> --table <table_name> [OPTIONS]

# Zotero
doctrail ingest --config <your_config.yaml> --zotero --collection <collection_name> [OPTIONS]

# Plugin
doctrail ingest --plugin <plugin_name> --db-path <database.db> [PLUGIN_OPTIONS]
```

**Modes:**

1. **Local Directory Mode** (`--input-dir`): Ingests files from a local directory using Apache Tika for text extraction.
2. **Zotero Mode** (`--zotero`): Ingests references and attachments from a Zotero collection.
3. **Plugin Mode** (`--plugin`): Uses a custom ingestion plugin for specialized data sources.

**Plugin Mode Example:**

```bash
# Use the DOI connector plugin to ingest academic literature
doctrail ingest --plugin doi_connector \
    --db-path ./literature.db \
    --cache-db=/path/to/doi_cache.sqlite \
    --project=my_research \
    --limit=100 \
    --verbose
```

**Available Plugins:**
- `doi_connector`: Ingests academic literature from DOI resolver cache databases
- Custom plugins can be added to `./plugins/` directory

**Required Options:**

*   **Local mode**: `--config FILE` or `--db-path`, `--input-dir DIR`, `--table NAME`
*   **Zotero mode**: `--config FILE` or `--db-path`, `--zotero`, `--collection NAME`
*   **Plugin mode**: `--plugin NAME`, `--db-path FILE` (plugin-specific options vary)

**Optional Options:**

*   `--force`: Force operation even if schema mismatch detected (local mode)
*   `--overwrite`: Overwrite existing documents
*   `--limit INT`: Limit number of items to process
*   `--verbose`: Enable detailed logging
*   `--plugin-dir DIR`: Directory containing custom plugins

**Example:**

```bash
# Ingest all documents from ./new_papers into the 'papers' table
doctrail ingest --config my_config.yaml --input-dir ./new_papers --table papers --verbose

# Ingest from Zotero collection
doctrail ingest --config my_config.yaml --zotero --collection "PhD Literature" --verbose

# Use custom plugin (e.g., DOI connector for academic literature)
doctrail ingest --plugin doi_connector --db-path ./lit.db --cache-db=/path/to/cache.sqlite --project=paying_for_organs

# Plugin-specific options can be passed directly (no -- separator needed)
doctrail ingest --plugin doi_connector --db-path ./literature.db --cache-db=~/.config/doi_resolver/cache.sqlite --project=paying_for_organs --limit 100

# Import ALL projects (use with caution!)
doctrail ingest --plugin doi_connector --db-path ./literature.db --cache-db=~/.config/doi_resolver/cache.sqlite --project=ALL
```

#### Plugin System

Doctrail supports custom ingestion plugins for specialized data sources. Plugins are Python modules that implement the `IngesterPlugin` protocol.

**Available Built-in Plugins:**

1. **doi_connector**: Ingests academic literature from a DOI resolver cache database
   - Options:
     - `--cache-db PATH`: Path to the cache.sqlite database (default: ~/.config/doi_resolver/cache.sqlite)
     - `--project NAME`: **REQUIRED** - Filter documents by project name (use "ALL" to import all projects)
     - `--base-path PATH`: Base path for resolving relative file paths

**Creating Custom Plugins:**

Place your plugin file in:
- `./plugins/` (current directory)
- Or specify with `--plugin-dir /path/to/plugins`

Example plugin structure:
```python
class Plugin:
    @property
    def name(self) -> str:
        return "my_plugin"
    
    @property
    def description(self) -> str:
        return "My custom ingestion plugin"
    
    @property
    def target_table(self) -> str:
        return "documents"
    
    async def ingest(self, db_path: str, config: Dict, **kwargs) -> Dict[str, int]:
        # Your ingestion logic here
        pass
```

### 3. `export` Command

Exports enriched data based on predefined export configurations in `config.yaml`.

**Syntax:**

```bash
doctrail export --config <your_config.yaml> --export-type <export_name> [OPTIONS]
```

**Required Options:**

*   `--config FILE`: Path to your configuration file.
*   `--export-type NAME`: The name of the export configuration (defined in `config.yaml`) to run.

**Optional Options:**

*   `--output-dir DIR`: Override the default output directory specified in the config file.

**Example:**

```bash
# Run the 'generate_report' export defined in project_config.yaml
doctrail export --config project_config.yaml --export-type generate_report
```

## Configuration (`config.yaml`)

This file defines how the tool operates. The configuration uses a **schema-driven approach** where:
- Simple schemas (single field) update columns directly in the source table
- Complex schemas (multiple fields) automatically create separate output tables
- All LLM responses are stored as raw JSON for audit trails
- OpenAI structured outputs ensure 100% valid JSON responses

Here's a breakdown of the sections:

```yaml
# --- Core Settings ---
database: path/to/your/database.db  # REQUIRED: Path to the SQLite database file
default_table: documents          # Default input table for enrichments
default_model: gpt-4o-mini        # Default LLM model if not specified per-task
verbose: true                     # Default verbosity (can be overridden by --verbose flag)
log_updates: true                 # Log successful database updates to a JSON file

# --- Reusable SQL Queries ---
# Define named SQL queries used by enrichments.
sql_queries:
  get_all_articles: >             # Name of the query
    SELECT rowid, *               # The actual SQL query
    FROM articles                 # Use multi-line strings for readability
    WHERE published = 1
  
  find_specific_docs: >
    SELECT rowid, doc_id, text_content
    FROM documents
    WHERE source = 'pubmed'

# --- LLM Model Definitions ---
# Define available LLM models and their parameters.
models:
  gpt-4o-mini:
    name: gpt-4o-mini             # Model name as used by the API provider (e.g., OpenAI)
    max_tokens: 4096              # Max tokens for the model
    temperature: 0.1              # Sampling temperature
  gpt-4-turbo:
    name: gpt-4-1106-preview
    max_tokens: 8192
    temperature: 0.0

# --- Enrichment Tasks ---
# Schema-driven enrichments that automatically handle storage and validation.
enrichments:
  # Example 1: Simple enrichment (direct column in source table)
  - name: extract_sentiment         # UNIQUE name for this task
    description: "Analyze document sentiment"
    input:
      query: get_all_articles       # REQUIRED: SQL query to select rows
      input_columns: ["content"]    # REQUIRED: Columns to pass to LLM
    # NEW: Schema-driven approach - single field -> direct column
    schema:
      sentiment: {enum: ["positive", "negative", "neutral"]}
    model: gpt-4o-mini              # Optional: Override default_model
    
  # Example 2: Complex enrichment (automatic separate table)
  - name: comprehensive_analysis    
    description: "Multi-field document analysis"
    input:
      query: find_specific_docs
      input_columns: ["text_content"]
    # NEW: Complex schema automatically creates separate table
    output_table: doc_analysis      # Custom table name (auto-created)
    key_column: sha1                # Foreign key back to source (default: sha1)
    schema:
      sentiment: {enum: ["positive", "negative", "neutral"]}
      confidence: {type: "number", minimum: 0, maximum: 1}
      topics: {type: "array", items: {type: "string"}, maxItems: 5}
      word_count: {type: "integer", minimum: 0}
    system_prompt: summarizer       # Optional: Name of the system prompt from 'system_prompts'
    prompt: |                       # REQUIRED: The prompt template sent to the LLM.
      Summarize the key points of the following text:

      {content} # Column names from 'input_columns' are available as variables

  - name: keyword_extraction
    description: "Extract keywords"
    table: articles
    input:
      query: get_all_articles
      input_columns: ["title", "content"] # Can use multiple input columns
    output_column: keywords
    prompt: |
      Extract the main keywords from the following text (title: {title}):
      {content}
      Return as a comma-separated list.

# --- Schema Types ---
# NEW: Schemas are now defined inline within enrichments for clarity.
# Supported schema types:

# 1. Simple types:
#    schema: {type: "string"}
#    schema: {type: "integer", minimum: 0}
#    schema: {type: "number", minimum: 0, maximum: 1}
#    schema: {type: "boolean"}

# 2. Enums (for classification):
#    schema: {enum: ["option1", "option2", "option3"]}
#    schema: ["choice1", "choice2", "choice3"]  # Shorthand

# 3. Complex schemas (multiple fields):
#    schema:
#      field1: {type: "string"}
#      field2: {enum: ["a", "b", "c"]}
#      field3: {type: "array", items: {type: "string"}}

# Complex schemas automatically create separate tables!

# --- Reusable System Prompts (Optional) ---
# Define system messages to guide LLM behavior.
system_prompts:
  summarizer: |
    You are an expert summarizer. Provide clear and concise summaries based on the input text.
  
  json_extractor: |
    You are a data extraction assistant. Respond ONLY with valid JSON matching the requested schema. Do not include explanations or markdown formatting.

# --- Export Configurations (Optional) ---
# Define how to export data using Jinja2 templates.
# (See bd-config.yml for a more complex example)
exports:
  simple_csv:
    description: "Export ID and Summary to CSV"
    query: >                      # SQL query to select data for export
      SELECT rowid, summary FROM articles WHERE summary IS NOT NULL
    template: "templates/simple_csv.j2" # Path to Jinja2 template file
    formats: ["csv"]              # Output format(s)
    required_fields: ["summary"]  # Ensure these columns exist and are not null
    output_naming: "article_summaries_{timestamp}" # Pattern for output file name

# --- Table Definitions (Optional, Advanced) ---
# Define metadata about tables, potentially useful for multi-table enrichments/exports.
tables:
  articles:
    base_query: "SELECT rowid, * FROM articles"
    description: "Main articles table"
  authors:
    base_query: "SELECT * FROM authors"
    description: "Author information"

# --- Enrichment Templates (Optional, Advanced) ---
# Define reusable enrichment structures that can be applied to multiple tables.
# (See bd-config.yml for examples)

# --- XML Schemas (For Complex Extractions) ---
# Define XML structures that automatically generate:
# 1. XML templates for LLM prompts
# 2. SQL tables and columns
# 3. Parsing logic for hierarchical data
xml_schemas:
  person_extraction:
    root: document
    elements:
      # Single-value fields (stored in parent table)
      date:
        type: text
        sql_type: TEXT
      location:
        type: text
        sql_type: TEXT
      
      # Multi-value fields (create child table)
      person:
        type: array
        table: extracted_persons  # Creates this table
        elements:
          name:
            type: text
            sql_type: TEXT
          role:
            type: enum
            values: [plaintiff, defendant, witness, judge, other]
            sql_type: TEXT
          age:
            type: integer
            sql_type: INTEGER

# Use XML schema in enrichment
enrichments:
  - name: extract_persons
    table: documents
    input:
      query: "SELECT rowid, sha1, * FROM documents"
      input_columns: ["content"]
    output_format: xml              # Enable XML mode
    xml_schema: person_extraction   # Reference schema
    prompt_append_template: true    # Append generated XML template
    prompt: "Extract all persons mentioned in this document."

```

## How it works (simplified)

1.  **Load Config:** Reads the specified `config.yaml`.
2.  **Select Task(s):** Identifies the enrichment task(s) requested via `--enrichments`.
3.  **Fetch Data:** For each task, executes the SQL query defined in `input.query`.
4.  **Prepare Inputs:** Formats the data from the specified `input_columns` into the `prompt` template.
5.  **Call LLM:** Sends the formatted prompt (and `system_prompt`) to the specified `model`.
6.  **Validate Output (Optional):** If a `schema` is defined, validates the LLM response.
7.  **Update Database:** Saves the validated LLM response to the `output_column`(s) in the target `table` for the corresponding `rowid`. Skips or overwrites based on the `--overwrite` flag and existing data.
8.  **Log:** Records actions and errors to `/tmp/doctrail.log`.

## Best practices

1.  **Start Simple:** Begin with one enrichment task and a small dataset (`--limit`).
2.  **Use `--verbose`:** Essential for debugging configuration and prompt issues.
3.  **Check Logs:** Examine `/tmp/doctrail.log` for detailed error messages.
4.  **Define Schemas:** For structured LLM outputs (like JSON), define a schema for validation. This catches errors early.
5.  **Refine Prompts:** LLM output quality heavily depends on prompt clarity. Iterate on your prompts.
6.  **Backup Database:** Especially before running with `--overwrite`.

## Contributing

Contributions are welcome! Please submit a Pull Request.

## License

MIT License


## Configuration examples

### Inline enum schema

```yaml
enrichments:
  # Example 1: Compensation type classification with aggressive enum validation
  - name: compensation_type
    description: "Classify compensation mentioned in documents"
    table: documents
    input:
      query: compensation_docs
      input_columns: ["content"]
    output_column: compensation_type
    schema:
      enum: ["neither", "compensation", "reimbursement"]
    model: gpt-4o-mini
    prompt: |
      Analyze this document and determine what type of financial arrangement is mentioned.

      Focus on:
      - Direct compensation (salary, wages, bonuses)
      - Reimbursement (expense reimbursement, cost recovery)
      - Neither (no financial arrangements mentioned)

  # Example 2: Document sentiment with simple enum
  - name: document_sentiment
    description: "Classify overall document sentiment"
    table: documents
    input:
    # ... (truncated for brevity)
```

### Complex schema with external rubric

```yaml
  # Step 4: Comprehensive analysis with external rubric
  - name: detailed_analysis
    description: "Perform comprehensive document analysis"
    input:
      query: needs_analysis
      input_columns: ["title", "abstract", "content:3000"]
    output_table: document_analysis
    system_prompt: analyst
    model: gpt-4o  # Use more capable model for complex analysis
```

For complete examples, see:
- `examples/enum_schema_demo.yml` - Demonstrates all enum schema features
- `examples/comprehensive_example.yml` - Complex multi-step workflow
- `tests/test_config_full.yml` - Comprehensive test configuration

### Inline enum schema (new feature!)

```yaml
enrichments:
  # Example 1: Compensation type classification with aggressive enum validation
  - name: compensation_type
    description: "Classify compensation mentioned in documents"
    table: documents
    input:
      query: compensation_docs
      input_columns: ["content"]
    output_column: compensation_type
    schema:
      enum: ["neither", "compensation", "reimbursement"]
    model: gpt-4o-mini
    prompt: |
      Analyze this document and determine what type of financial arrangement is mentioned.

      Focus on:
      - Direct compensation (salary, wages, bonuses)
      - Reimbursement (expense reimbursement, cost recovery)
      - Neither (no financial arrangements mentioned)

  # Example 2: Document sentiment with simple enum
  - name: document_sentiment
    description: "Classify overall document sentiment"
    table: documents
    input:
    # ... (truncated for brevity)
```

### Real-world example: policy analysis

```yaml
  # Step 1: Boolean filter
  - name: compensation_type
    description: "Quick filter – does article mention compensation, reimbursement, or neither"
    table: documents
    input:
      query: unprocessed_docs_with_prc_source
      input_columns: ["raw_content"]
    output_column: compensation_type
    schema:
      enum: ["neither", "compensation", "reimbursement"]
    # model: gpt-4.1-nano
    model: gpt-4o-mini
```

For complete examples, see:
- `examples/enum_schema_demo.yml` - Demonstrates all enum schema features
- `examples/paying_for_organs_config.yml` - Production research workflow
- `tests/test_config_full.yml` - Comprehensive test configuration
