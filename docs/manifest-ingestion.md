# Manifest-Based Ingestion

Doctrail supports enriching documents with external metadata during ingestion using manifest files. This is particularly useful for web scraping projects, document migrations, or any scenario where metadata isn't embedded in the files themselves.

## Quick Start

Place a `manifest.json` file in your document directory and run:

```bash
./doctrail ingest --db-path database.db --input-dir /path/to/documents
```

The manifest will be auto-detected and metadata will be added to each file.

## Manifest Format

The manifest must be a JSON file with this structure:

```json
{
  "filename.html": {
    "url": "https://example.com/page",
    "scraped_at": "2025-07-14 16:40:47",
    "author": "John Doe",
    "category": "research"
  },
  "document.pdf": {
    "source": "internal_system",
    "department": "engineering",
    "project_id": "PROJ-123"
  }
}
```

### Rules:
- Top-level keys must be exact filenames (including extension)
- Each filename maps to a flat metadata object
- All metadata values must be simple types (string, number, boolean)
- No nested objects or arrays allowed
- Files not in the manifest are ingested normally without extra metadata

## Storage

All manifest metadata is stored with the `metadata_` prefix in your SQLite database:

```sql
-- Example: Query documents with their manifest metadata
SELECT 
  filename,
  content,
  metadata_url,
  metadata_scraped_at,
  metadata_author
FROM documents
WHERE metadata_url IS NOT NULL;
```

## Command Options

### Auto-detection (default)
```bash
# Looks for manifest.json in the input directory
./doctrail ingest --db-path db.sqlite --input-dir /path/to/docs
```

### Custom manifest path
```bash
# Specify a different manifest file
./doctrail ingest --db-path db.sqlite --input-dir /path/to/docs --manifest /path/to/custom-manifest.json
```

### Skip manifest
```bash
# If you have a manifest.json but want to ignore it
./doctrail ingest --db-path db.sqlite --input-dir /path/to/docs --manifest ""
```

## Use Cases

### Web Scraping
Perfect for scraped content where you need to preserve:
- Original URLs
- Scrape timestamps
- Page metadata
- Navigation structure

```json
{
  "article_123.html": {
    "url": "https://news.site/article/123",
    "scraped_at": "2025-07-14 10:30:00",
    "headline": "Breaking News",
    "author": "Jane Reporter",
    "section": "politics"
  }
}
```

### Document Migration
When migrating from other systems:
```json
{
  "DOC-2024-001.pdf": {
    "legacy_id": "OLD-SYS-45678",
    "department": "Legal",
    "classification": "Confidential",
    "migration_date": "2025-07-14"
  }
}
```

### Research Projects
Organizing research materials:
```json
{
  "paper_smith_2024.pdf": {
    "doi": "10.1234/example",
    "journal": "Nature",
    "year": 2024,
    "topic": "Climate Change",
    "relevance_score": 0.95
  }
}
```

## Integration with Enrichments

After ingestion, you can use the manifest metadata in your enrichments:

```yaml
# enrichment_config.yml
sql_queries:
  scraped_content: |
    SELECT sha1, content, metadata_url, metadata_scraped_at
    FROM documents
    WHERE metadata_url LIKE '%falundafa.org%'

enrichments:
  - name: analyze_content
    input:
      query: scraped_content
      input_columns:
        - content
        - metadata_url
    prompt: |
      Analyze this content from {{ metadata_url }}:
      {{ content }}
    output_column: analysis
```

## Tips

1. **Validation**: The manifest loader validates structure and will error on:
   - Non-string filenames as keys
   - Non-dictionary metadata values
   - Nested objects or arrays in metadata

2. **Performance**: The manifest is loaded once into memory, so it's efficient even for thousands of files

3. **Updates**: To update metadata, modify the manifest and re-run with `--overwrite`

4. **Debugging**: Use `--verbose` to see which files get manifest metadata:
   ```bash
   ./doctrail ingest --db-path db.sqlite --input-dir docs/ --verbose
   ```