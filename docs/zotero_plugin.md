# Zotero Plugin

The Zotero plugin enables ingesting academic papers from your Zotero collections directly into Doctrail, extracting full text from PDFs and storing complete bibliographic metadata.

## Quick Start

```bash
./doctrail.py ingest --plugin zotero \
  --db-path ./literature.db \
  --collection "My Research Collection"
```

## Features

- **Direct Zotero Integration**: Connect to your Zotero library via API
- **Local File Access**: Reads PDFs from your local Zotero storage (no downloading)
- **Full Text Extraction**: Uses Doctrail's reliable extractors (pdftotext, etc.)
- **Complete Metadata**: Stores authors, year, DOI, abstract, tags, and more
- **BibTeX Support**: Preserves citation keys (full BibTeX export coming soon)
- **FTS Enabled**: Full-text search on content and abstracts

## Configuration

The plugin automatically finds credentials in this order:

1. **Command line options**:
   ```bash
   --api-key YOUR_KEY --user-id YOUR_ID
   ```

2. **Environment variables**:
   ```bash
   export ZOTERO_API_KEY="your_key"
   export ZOTERO_USER_ID="your_id"
   ```

3. **Existing .env file** at `/Users/m/libs/zotero_enricher/.env`

## Zotero Storage Location

The plugin automatically checks common Zotero locations:
- `~/docs/Zotero` (default on this system)
- `~/Zotero`
- `~/Documents/Zotero`

Override with `--zotero-dir /custom/path` if needed.

## Database Schema

Creates a `literature` table with:

| Column | Description |
|--------|-------------|
| `sha1` | Content hash (primary key) |
| `zotero_key` | Zotero item identifier |
| `title` | Paper title |
| `authors` | Semicolon-separated author list |
| `year` | Publication year |
| `publication` | Journal/conference name |
| `doi` | Digital Object Identifier |
| `abstract` | Paper abstract |
| `raw_content` | Full text from PDF |
| `file_path` | Local file location |
| `tags` | JSON array of Zotero tags |
| `zotero_metadata` | Complete Zotero item data (JSON) |

## Usage Examples

### Basic ingestion
```bash
./doctrail.py ingest --plugin zotero \
  --db-path ./research.db \
  --collection "PhD Research"
```

### With custom table name
```bash
./doctrail.py ingest --plugin zotero \
  --db-path ./papers.db \
  --collection "Machine Learning Papers" \
  --table ml_papers
```

### Limited ingestion for testing
```bash
./doctrail.py ingest --plugin zotero \
  --db-path ./test.db \
  --collection "Test Collection" \
  --limit 10
```

### Overwrite existing entries
```bash
./doctrail.py ingest --plugin zotero \
  --db-path ./papers.db \
  --collection "Updated Papers" \
  --overwrite
```

## Enrichment Workflow

After ingestion, you can enrich the literature data:

```yaml
# literature_enrichment.yml
database: ./literature.db

sql_queries:
  papers: SELECT rowid, title, abstract, raw_content FROM literature

enrichments:
  - name: summarize_papers
    input:
      query: papers
      input_columns: ["title", "abstract", "raw_content:1000"]
    schema:
      summary: {type: string}
      key_findings: {type: array, items: {type: string}}
      methodology: {type: string}
    prompt: |
      Summarize this academic paper:
      Title: {title}
      Abstract: {abstract}
      Content preview: {raw_content}
```

## Troubleshooting

### "Collection not found"
The plugin will list available collections. Check spelling and case.

### "Zotero storage directory not found"
Specify your Zotero data location:
```bash
--zotero-dir ~/Library/Application\ Support/Zotero
```

### Missing text from PDFs
- Ensure PDFs are downloaded in Zotero (not just linked)
- Check file permissions in Zotero storage
- Some PDFs may be image-only (OCR not yet supported)

### No attachments found
Items without PDF/HTML attachments are skipped unless they have abstracts.