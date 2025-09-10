# Document Ingestion Guide

This guide covers all ingestion functionality in doctrail - how to import documents from files and directories into your database for processing and enrichment.

## Quick Start

```bash
# Single directory
./doctrail.py ingest --db-path ./research.db --input-dir ./documents

# Multiple directories (processed sequentially)
./doctrail.py ingest --db-path ./research.db --input-dir ./papers --input-dir ./reports --input-dir ./articles
```

## Core Concepts

### What is Ingestion?
Ingestion is the process of:
1. **Discovering** documents in specified directories
2. **Extracting** text content from various file formats
3. **Storing** documents and metadata in your SQLite database
4. **Preparing** documents for enrichment with LLM analysis

### Supported File Formats
- **PDF**: Including automatic OCR for scanned documents
- **DOC**: Legacy Microsoft Word documents (using antiword)
- **DOCX**: Modern Microsoft Word documents  
- **HTML/MHTML**: Web pages and saved web content
- **Plain Text**: .txt files
- **EPUB/MOBI**: E-book formats
- **DJVU**: Document format

### Automatic File Filtering
Doctrail automatically skips system and sync files:
- **Hidden files**: Files starting with `.` (like `.DS_Store`)
- **Sync files**: `.sync` directories, `IgnoreList`, `StreamsList`, `FolderType`
- **Conflict files**: Files containing "conflict" in the name
- **System files**: Platform-specific files like `Thumbs.db`, `desktop.ini`
- **Unsupported formats**: Video, audio, archive files

## Command Reference

### Basic Command Structure
```bash
./doctrail.py ingest [OPTIONS]
```

### Required Options
- `--db-path PATH` - SQLite database file path (will be created if doesn't exist)
- `--input-dir PATH` - Directory containing documents to ingest

### Multi-Directory Support
You can specify multiple directories to process them all in one command:

```bash
./doctrail.py ingest \
    --db-path ./database.db \
    --input-dir ./academic_papers \
    --input-dir ./news_articles \
    --input-dir ./government_reports
```

**How it works:**
- Directories are processed **sequentially** in the order specified
- All documents go into the **same database table**
- Only the **first directory** will prompt for confirmation
- Progress shows "Processing directory 1/3", "Processing directory 2/3", etc.

### File Filtering Options

#### Include Patterns
```bash
# Only process PDF files
--include-pattern "*.pdf"

# Multiple patterns (comma-separated)
--include-pattern "*.pdf,*.docx,*.txt"

# Pattern examples
--include-pattern "research_*.pdf"    # Files starting with "research_"
--include-pattern "*_final.docx"      # Files ending with "_final"
```

#### Exclude Patterns
```bash
# Skip draft files
--exclude-pattern "*draft*,*temp*"

# Skip hidden files and directories
--exclude-pattern ".*"

# Complex exclusions
--exclude-pattern "*draft*,*temp*,*backup*,*old*"
```

### Processing Options

#### Skip Confirmation
```bash
--yes    # Skip "Do you want to continue?" prompts
```

#### Overwrite Existing
```bash
--overwrite    # Re-process documents that are already in the database
```

#### Verbose Output
```bash
--verbose      # Show detailed processing information
--debug        # Show debug-level information
```

### Table and Schema Options

#### Custom Table Name
```bash
--table-name custom_documents    # Default is "documents"
```

#### Custom Schema
```bash
--schema path/to/schema.json    # Use custom document schema
```

## Common Usage Patterns

### Academic Research Project
```bash
./doctrail.py ingest \
    --db-path ./literature_review.db \
    --input-dir ./downloaded_papers \
    --input-dir ./reference_materials \
    --input-dir ./supplementary_docs \
    --include-pattern "*.pdf" \
    --exclude-pattern "*preprint*,*draft*" \
    --yes \
    --verbose
```

### News and Media Analysis
```bash
./doctrail.py ingest \
    --db-path ./media_analysis.db \
    --input-dir ./news_articles \
    --input-dir ./press_releases \
    --input-dir ./social_media_exports \
    --include-pattern "*.pdf,*.html,*.txt" \
    --yes
```

### Legal Document Processing
```bash
./doctrail.py ingest \
    --db-path ./legal_docs.db \
    --input-dir ./contracts \
    --input-dir ./court_filings \
    --input-dir ./legislation \
    --include-pattern "*.pdf,*.docx" \
    --exclude-pattern "*draft*,*template*" \
    --overwrite \
    --verbose
```

### Mixed Format Archive
```bash
./doctrail.py ingest \
    --db-path ./archive.db \
    --input-dir ./historical_documents \
    --input-dir ./scanned_materials \
    --input-dir ./digital_records \
    --include-pattern "*" \
    --exclude-pattern "*.zip,*.rar,*.exe" \
    --yes
```

## Best Practices

### Directory Organization
- **Separate by source**: Different `--input-dir` for different document sources
- **Group by type**: Academic papers in one directory, news in another
- **Use descriptive names**: `./court_cases` not `./docs1`

### File Naming
- Use consistent naming conventions
- Avoid special characters that might cause issues
- Include dates or version numbers when relevant

### Performance Tips
- **Large collections**: Use `--yes` to avoid manual confirmation for each directory
- **Incremental updates**: Run without `--overwrite` to only process new files
- **Selective processing**: Use `--include-pattern` to focus on specific file types
- **Monitor progress**: Use `--verbose` for large ingestion jobs

### Database Management
- **Backup before large ingests**: Copy your .db file before major ingestion runs
- **Check disk space**: Large document collections require significant storage
- **Use absolute paths**: Avoid relative paths for databases you'll access from different directories

## Troubleshooting

### Common Issues

#### "No documents found"
- Check directory paths are correct and accessible
- Verify include/exclude patterns aren't too restrictive
- Use `--verbose` to see which files are being scanned

#### "Permission denied"
- Ensure doctrail has read access to source directories
- Check that the database directory is writable
- On macOS/Linux: check file permissions with `ls -la`

#### OCR failures
- Install required OCR dependencies (see installation guide)
- Some scanned PDFs may have poor quality images
- Try `--skip-ocr` if OCR is causing issues

#### Memory issues with large files
- Very large PDF files may cause memory problems
- Consider splitting large files or using `--limit` for testing

### Getting Help
- Use `./doctrail.py ingest --help` for command reference
- Check `./doctrail.py --help` for global options
- Enable `--verbose` for detailed processing information
- Check `/tmp/doctrail.log` for detailed error messages

## Next Steps

After ingestion:
1. **Verify your data**: Run `./doctrail.py query --db-path ./your.db --limit 10` to check ingested documents
2. **Set up enrichments**: Create enrichment configurations to analyze your documents
3. **Export results**: Use export functionality to get your enriched data

See the [enrichment guide](./enrichment.md) for next steps in document analysis.