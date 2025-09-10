# Doctrail Plugin System

The plugin system allows you to create custom ingestion connectors for bringing data into Doctrail from various sources.

## Creating a Plugin

To create a custom ingestion plugin, create a Python file with a `Plugin` class that implements the required interface:

```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "your-dependencies-here",
# ]
# ///

from typing import Dict, Optional

class Plugin:
    """Your custom ingestion plugin"""
    
    @property
    def name(self) -> str:
        """Unique identifier for the plugin"""
        return "my_plugin"
    
    @property
    def description(self) -> str:
        """Human-readable description"""
        return "Ingests data from my custom source"
    
    @property
    def target_table(self) -> str:
        """Default table name for ingested data"""
        return "my_data"
    
    async def ingest(
        self,
        db_path: str,
        config: Dict,
        verbose: bool = False,
        overwrite: bool = False,
        limit: Optional[int] = None,
        **kwargs  # Plugin-specific arguments
    ) -> Dict[str, int]:
        """
        Main ingestion method.
        
        Returns:
            Dictionary with stats: {
                "total": total_processed,
                "success_count": successful,
                "error_count": errors,
                "skipped_count": skipped
            }
        """
        # Your ingestion logic here
        pass
```

## Using Plugins

### Built-in Plugins

Doctrail comes with these built-in plugins:

- `doi_connector` - Ingests academic literature from DOI resolver cache
- `zotero_custom` - Example custom Zotero connector (skeleton)

### Running a Plugin

```bash
# Basic usage
./doctrail ingest --plugin doi_connector --db-path ./my_database.db

# With plugin-specific arguments
./doctrail ingest --plugin doi_connector \
    --db-path ./literature.db \
    --cache-db=/path/to/cache.sqlite \
    --project=my_project \
    --limit=100 \
    --verbose
```

### Custom Plugin Locations

Plugins are discovered in these locations (in order):

1. Built-in plugins directory: `src/plugins/`
2. Current working directory: `./plugins/`
3. Custom directory: specified with `--plugin-dir`

## Example: DOI Connector

The DOI connector demonstrates a complete plugin implementation:

- Connects to an external SQLite cache database
- Resolves relative file paths
- Extracts text content using Tika
- Creates a specialized `lit` table for literature
- Handles DOI as primary identifier
- Supports project-based filtering

Key features:
- Custom schema for secondary sources (DOI, bibtex, etc.)
- Integration with existing text extraction pipeline
- Progress tracking and error handling
- Graceful handling of missing files

## Plugin Development Tips

1. **Schema Design**: Create appropriate table schemas for your data type
2. **Error Handling**: Handle missing files, network errors gracefully
3. **Progress Tracking**: Use tqdm for long-running operations
4. **Logging**: Use loguru for consistent logging
5. **Configuration**: Accept both CLI arguments and config file options
6. **Reuse Components**: Import from `ingester` for text extraction

## Advanced Plugin Features

### Custom Schema Creation

```python
def _ensure_schema(self, db_path: str) -> bool:
    """Create custom table schema"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {self.target_table} (
            id TEXT PRIMARY KEY,
            source_id TEXT UNIQUE,
            content TEXT,
            metadata JSON,
            created_at TEXT
        )
    """)
    
    conn.commit()
    conn.close()
    return True
```

### Batch Processing

```python
async def process_batch(items):
    tasks = []
    for item in items:
        tasks.append(process_single_item(item))
    
    results = await asyncio.gather(*tasks)
    return results
```

### Integration with Doctrail Features

Plugins can leverage Doctrail's existing functionality:
- Text extraction via Tika
- Database utilities from sqlite-utils
- Rich console output
- Configuration system
- Logging infrastructure 