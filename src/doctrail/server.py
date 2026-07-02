"""
FastAPI server for Doctrail operations.

This server provides HTTP endpoints for:
- Multi-database search (FTS, semantic, SQL)
- Enrichment operations
- Database management
- Self-documenting help system

Architecture:
    /                           → List databases, server info
    /help                       → Doctrail overview
    /help/search                → Search API docs (generic)
    /help/enrich                → Enrichment API docs (generic)
    /db/{name}/help             → Database-specific help
    /db/{name}/fts              → Full-text search
    /db/{name}/chroma           → Semantic search
    /db/{name}/sql              → Raw SQL queries
    /db/{name}/document/{id}    → Get document
    /db/{name}/text/{id}        → Get raw text
    /db/{name}/collections      → List collections
    /db/{name}/stats            → Database statistics
    /db/{name}/enrich           → Run enrichment (POST)

Legacy endpoints (backward compatible):
    /enrich                     → Run enrichment with config file
    /ingest                     → Ingest documents
    /export                     → Export data
"""

import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, Header, HTTPException, Query, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from .core import (
    run_enrichment,
    run_ingest,
    run_export,
    list_enrichments,
    ConfigurationError,
    EnrichmentError,
    DatabaseError,
)

# Import core search functions (shared with CLI)
from .search import (
    fts_search as core_fts_search,
    chroma_search as core_chroma_search,
    sql_query as core_sql_query,
    get_document as core_get_document,
    get_stats as core_get_stats,
    format_search_results_text,
    format_sql_results_text,
    format_document_text,
    SearchResponse as CoreSearchResponse,
)

logger = logging.getLogger(__name__)

try:
    DOCTRAIL_VERSION = version("doctrail")
except PackageNotFoundError:
    DOCTRAIL_VERSION = "0.0.0+unknown"

# Global state
server_config = None  # ServerConfig instance
chroma_clients: Dict[str, Any] = {}  # name -> (client, collection)
embedding_cache: Dict[str, List[float]] = {}  # query -> embedding
openai_client = None


# Pydantic models for requests/responses

class DatabaseInfo(BaseModel):
    """Basic database information."""
    name: str
    description: str
    corpus_type: str
    has_chunks: bool
    has_chroma: bool


class ServerInfoResponse(BaseModel):
    """Response for root endpoint."""
    service: str = "doctrail"
    version: str = DOCTRAIL_VERSION
    databases: List[DatabaseInfo]
    usage: str = "GET /db/{database_name}/help for database-specific documentation"


class SearchResult(BaseModel):
    """A single search result."""
    doc_id: str
    title: Optional[str] = None
    snippet: Optional[str] = None
    score: Optional[float] = None
    metadata: Dict[str, Any] = {}


class SearchResponse(BaseModel):
    """Response for search endpoints."""
    query: str
    database: str
    results: List[SearchResult]
    total: int
    format: str = "json"


class SqlRequest(BaseModel):
    """Request for SQL endpoint."""
    query: str
    format: str = "text"


# Legacy request/response models (for backward compatibility)

class EnrichmentRequest(BaseModel):
    """Request model for enrichment operations."""
    config_path: str = Field(..., description="Path to YAML configuration file")
    enrichments: List[str] = Field(..., description="List of enrichment task names to run")
    db_path: Optional[str] = Field(None, description="Optional override for database path from config")
    model: Optional[str] = Field(None, description="Optional model override for all enrichments")
    limit: Optional[int] = Field(None, description="Limit number of rows to process")
    rowid: Optional[int] = Field(None, description="Process only specific row by rowid")
    sha1: Optional[str] = Field(None, description="Process only specific row by sha1 hash")
    overwrite: bool = Field(False, description="Overwrite existing data in output columns")
    batch_size: Optional[int] = Field(None, description="Override batch size for processing")
    truncate: bool = Field(False, description="Truncate long inputs to fit model context window")
    skip_cost_check: bool = Field(False, description="Skip cost estimation and confirmation")
    cost_threshold: float = Field(5.0, description="Cost threshold for confirmation prompt")
    dry_run: bool = Field(False, description="Preview without calling model or updating database")
    verbose: bool = Field(False, description="Enable detailed logging")
    dedupe_scope: str = Field("query", description="Append-mode dedupe scope: prompt or query")
    materialize_inputs: bool = Field(True, description="Persist the exact input rowset for each run")
    allow_column_collision: bool = Field(False, description="Allow enrichment field names that match source table columns")


class EnrichmentResponse(BaseModel):
    """Response model for enrichment operations."""
    status: str
    enrichments_run: List[str]
    total_processed: int
    results: Optional[List[Dict[str, Any]]] = None
    errors: Optional[List[str]] = None
    run_artifacts: Optional[List[Dict[str, Any]]] = None


class IngestRequest(BaseModel):
    """Request model for ingestion operations."""
    db_path: str = Field(..., description="Path to SQLite database")
    input_dirs: Optional[List[str]] = Field(None, description="List of directories containing documents to ingest")
    table: str = Field("documents", description="Target table name")
    force: bool = Field(False, description="Force operation even if schema mismatch detected")
    overwrite: bool = Field(False, description="Overwrite existing documents")
    limit: Optional[int] = Field(None, description="Limit number of files to process")
    include_pattern: Optional[str] = Field(None, description="Only process files matching this glob pattern")
    exclude_pattern: Optional[str] = Field(None, description="Skip files matching this glob pattern")
    readability: bool = Field(False, description="Use readability library for HTML extraction")
    html_extractor: str = Field("default", description="HTML extraction method")
    skip_garbage_check: bool = Field(False, description="Skip garbage content detection")
    fulltext: bool = Field(False, description="Create full-text search index after ingestion")
    manifest_path: Optional[str] = Field(None, description="Path to manifest.json for metadata")
    labels: Optional[List[str]] = Field(None, description="Labels to apply to ingested documents")
    pdf_engine: str = Field("auto", description="PDF extraction engine")
    ocr_engine: str = Field("auto", description="OCR engine to use when needed")
    workers: Optional[int] = Field(None, description="Number of extraction worker processes")
    verbose: bool = Field(False, description="Enable detailed logging")
    plugin_name: Optional[str] = Field(None, description="Name of ingestion plugin to use")
    plugin_args: Optional[Dict[str, Any]] = Field(None, description="Arguments for the plugin")
    zotero_api_key: Optional[str] = Field(None, description="Zotero API key")
    zotero_library_id: Optional[str] = Field(None, description="Zotero library ID")
    zotero_library_type: str = Field("user", description="Zotero library type")
    zotero_collection: Optional[str] = Field(None, description="Zotero collection to ingest")


class IngestResponse(BaseModel):
    """Response model for ingestion operations."""
    status: str
    mode: Optional[str] = None
    plugin: Optional[str] = None
    directories_processed: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    results: Optional[List[Dict[str, Any]]] = None


class ExportRequest(BaseModel):
    """Request model for export operations."""
    config_path: str = Field(..., description="Path to YAML configuration file")
    export_type: str = Field(..., description="Type of export to run")
    output_dir: Optional[str] = Field(None, description="Optional override for output directory")
    verbose: bool = Field(False, description="Enable detailed logging")


class ExportResponse(BaseModel):
    """Response model for export operations."""
    status: str
    export_type: str
    output_dir: str
    result: Optional[Dict[str, Any]] = None


class HealthResponse(BaseModel):
    """Response model for health check."""
    status: str
    version: str


class ListEnrichmentsRequest(BaseModel):
    """Request model for listing enrichments."""
    config_path: str = Field(..., description="Path to YAML configuration file")


class EnrichmentInfo(BaseModel):
    """Information about a single enrichment."""
    name: str
    description: str
    model: str
    output_column: Optional[str] = None
    output_table: Optional[str] = None


class ListEnrichmentsResponse(BaseModel):
    """Response model for listing enrichments."""
    status: str
    enrichments: List[EnrichmentInfo]
    count: int


# Lifespan management

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown handling."""
    global server_config, chroma_clients, openai_client

    # Try to load server config for multi-database mode
    config_path = os.environ.get("DOCTRAIL_SERVER_CONFIG")
    if config_path:
        try:
            from .server_config import load_server_config
            server_config = load_server_config(config_path)
            logger.info(f"Loaded server config from {config_path}")

            # Validate database connections without keeping cross-request handles.
            for name, db_config in server_config.databases.items():
                try:
                    with db_config.get_connection() as conn:
                        conn.execute("SELECT 1")
                    logger.info(f"Connected to database '{name}' at {db_config.db_file}")

                    # Initialize Chroma if available
                    if db_config.has_chroma():
                        try:
                            import chromadb
                            chroma_client = chromadb.PersistentClient(path=str(db_config.chroma_path))
                            chroma_collection = chroma_client.get_collection("chunks")
                            chroma_clients[name] = (chroma_client, chroma_collection)
                            logger.info(f"Loaded Chroma for '{name}' with {chroma_collection.count():,} vectors")
                        except Exception as e:
                            logger.warning(f"Failed to load Chroma for '{name}': {e}")

                except Exception as e:
                    logger.error(f"Failed to connect to database '{name}': {e}")

            # Initialize OpenAI client if needed (for semantic search)
            api_key = os.environ.get("OPENAI_API_KEY")
            if api_key and chroma_clients:
                try:
                    from openai import OpenAI
                    openai_client = OpenAI(api_key=api_key)
                    logger.info("OpenAI client initialized for semantic search")
                except ImportError:
                    logger.warning("OpenAI package not installed, semantic search disabled")

            # Print startup banner
            print("\n" + "=" * 60)
            print("Doctrail Server READY (multi-database mode)")
            print("=" * 60)
            print(f"Databases: {list(server_config.databases.keys())}")
            print(f"Listening on: http://{server_config.host}:{server_config.port}")
            print("\nEndpoints:")
            print("  /              - List databases")
            print("  /help          - Server documentation")
            print("  /db/{name}/fts - Full-text search")
            print("  /db/{name}/sql - SQL queries")
            print("=" * 60 + "\n")

        except FileNotFoundError:
            logger.info(f"Server config not found: {config_path}")
            logger.info("Running in legacy mode (single-database endpoints only)")
    else:
        logger.info("No DOCTRAIL_SERVER_CONFIG set, running in legacy mode")

    yield

    logger.info("Server shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Doctrail API",
    description="Multi-database search and enrichment server",
    version=DOCTRAIL_VERSION,
    lifespan=lifespan,
)


# Dependency to get database config and request-scoped connection

def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


def _host_requires_write_token() -> bool:
    host = os.environ.get("DOCTRAIL_SERVER_HOST", "127.0.0.1")
    return host not in {"127.0.0.1", "localhost", "::1"}


async def require_write_api(authorization: Optional[str] = Header(None)) -> None:
    """Gate legacy write-capable HTTP endpoints."""
    if not _env_flag("DOCTRAIL_ENABLE_WRITE_API"):
        raise HTTPException(
            status_code=403,
            detail=(
                "Legacy write API disabled. Start the server with "
                "--enable-write-api to use this endpoint."
            ),
        )

    token = os.environ.get("DOCTRAIL_SERVER_TOKEN")
    if _host_requires_write_token() and not token:
        raise HTTPException(
            status_code=403,
            detail="A bearer token is required when write endpoints are enabled on a non-loopback host.",
        )

    if token and authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token.")

def _get_db_config(db_name: str):
    """Get database config by name."""
    if server_config is None:
        raise HTTPException(
            status_code=503,
            detail="Server not configured for multi-database mode. Set DOCTRAIL_SERVER_CONFIG."
        )

    if db_name not in server_config.databases:
        available = list(server_config.databases.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Database '{db_name}' not found. Available: {available}"
        )

    return server_config.databases[db_name]


@asynccontextmanager
async def open_db(db_name: str):
    """Open a SQLite connection for one request."""
    db_config = _get_db_config(db_name)
    conn = db_config.get_connection()
    try:
        yield db_config, conn
    finally:
        conn.close()


# Root endpoints

@app.get("/")
async def root():
    """List all databases and server info."""
    if server_config is None:
        return {
            "status": "ok",
            "service": "doctrail",
            "version": DOCTRAIL_VERSION,
            "mode": "legacy",
            "databases": [],
            "usage": "Use /enrich, /ingest, /export endpoints. Or set DOCTRAIL_SERVER_CONFIG for multi-db mode."
        }

    databases = [
        {
            "name": name,
            "description": db.description,
            "corpus_type": db.corpus_type,
            "has_chunks": db.has_chunks(),
            "has_chroma": db.has_chroma(),
        }
        for name, db in server_config.databases.items()
    ]

    return {
        "status": "ok",
        "service": "doctrail",
        "version": DOCTRAIL_VERSION,
        "mode": "multi-database",
        "databases": databases,
        "usage": "GET /db/{database_name}/help for database-specific documentation"
    }


@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    return {"status": "ok", "version": DOCTRAIL_VERSION}


# Help endpoints

@app.get("/help", response_class=PlainTextResponse)
async def help_overview():
    """Server-level help documentation."""
    help_path = Path(__file__).resolve().parents[2] / "docs" / "help.md"
    if help_path.exists():
        return help_path.read_text()

    # Fallback to generated help
    lines = [
        "# Doctrail Search & Enrichment Server",
        "",
        "## Quick start",
        "",
        "1. List databases: GET /",
        "2. Get database help: GET /db/{name}/help",
        "3. Search: GET /db/{name}/fts?q=keyword",
        "4. Semantic search: GET /db/{name}/chroma?q=concept",
        "",
        "## Available databases",
        "",
    ]

    if server_config:
        for name, db in server_config.databases.items():
            lines.append(f"- **{name}**: {db.description}")
            lines.append(f"  - Path: {db.db_file}")
            lines.append(f"  - Chunks: {'✓' if db.has_chunks() else '✗'}")
            lines.append(f"  - Vectors: {'✓' if db.has_chroma() else '✗'}")
            lines.append("")
    else:
        lines.append("No databases configured. Set DOCTRAIL_SERVER_CONFIG.")

    lines.extend([
        "",
        "## Endpoints",
        "",
        "- GET /help/search - Search API documentation",
        "- GET /help/enrich - Enrichment API documentation",
        "- GET /db/{name}/help - Database-specific help",
    ])

    return "\n".join(lines)


@app.get("/help/search", response_class=PlainTextResponse)
async def help_search():
    """Search API documentation."""
    help_path = Path(__file__).resolve().parents[2] / "docs" / "help-search.md"
    if help_path.exists():
        return help_path.read_text()

    return """# Search API

## Full-text search (FTS)

Search document content using SQLite FTS5:

```
GET /db/{name}/fts?q=keyword&limit=20
```

Parameters:
- q: Search query (FTS5 syntax)
- limit: Max results (default: 20)
- collection: Filter by collection name
- format: "text" (default) or "json"

Example queries:
- Simple: `?q=democracy`
- Phrase: `?q="civil society"`
- Boolean: `?q=china AND politics`
- Prefix: `?q=democ*`

## Semantic search (Chroma)

Conceptual search using embeddings:

```
GET /db/{name}/chroma?q=concept&limit=10
```

Parameters:
- q: Natural language query
- limit: Max results (default: 10)
- collection: Filter by collection
- format: "text" or "json"

## SQL queries

Direct SQL access (SELECT only):

```
GET /db/{name}/sql?query=SELECT...
POST /db/{name}/sql (body: {"query": "SELECT..."})
```

Security: Only SELECT and WITH queries allowed.

## Document retrieval

Get full document:
```
GET /db/{name}/document/{doc_id}
```

Get raw text only:
```
GET /db/{name}/text/{doc_id}
```
"""


@app.get("/help/enrich", response_class=PlainTextResponse)
async def help_enrich():
    """Enrichment API documentation."""
    help_path = Path(__file__).resolve().parents[2] / "docs" / "help-enrich.md"
    if help_path.exists():
        return help_path.read_text()

    return """# Enrichment API

## Run enrichment

POST /db/{name}/enrich

Request body:
```json
{
    "config_path": "/path/to/config.yaml",
    "enrichment_name": "detect_language",
    "limit": 10,
    "overwrite": false
}
```

Or inline enrichment:
```json
{
    "prompt": "Classify the language of this text",
    "schema": {"enum": ["English", "Chinese", "Other"]},
    "model": "gpt-4o-mini",
    "limit": 10
}
```

## List enrichments

GET /db/{name}/enrichments

Returns available enrichment configurations.

## Get enrichment result

GET /db/{name}/enrichment/{doc_id}/{enrichment_name}

Returns stored enrichment result for a document.
"""


# Database-specific help

@app.get("/db/{db_name}/help", response_class=PlainTextResponse)
async def db_help(db_name: str):
    """Database-specific help documentation."""
    db_config = _get_db_config(db_name)

    # Check for custom help.md
    if db_config.help_path and db_config.help_path.exists():
        return db_config.help_path.read_text()

    # Generate help from database
    stats = db_config.get_stats()

    lines = [
        f"# {db_name}",
        "",
        db_config.description or "No description available.",
        "",
        "## Statistics",
        "",
        f"- Documents: {stats.get('document_count', 'unknown'):,}",
    ]

    if stats.get("chunk_count"):
        lines.append(f"- Chunks: {stats['chunk_count']:,}")

    lines.extend([
        f"- Corpus type: {db_config.corpus_type}",
        f"- FTS enabled: {'✓' if db_config.has_chunks() else '✗'}",
        f"- Semantic search: {'✓' if db_config.has_chroma() else '✗'}",
        "",
        "## Schema",
        "",
        f"- Primary key: {db_config.schema.pk_column} ({db_config.schema.pk_type})",
        f"- Content column: {db_config.schema.content_column}",
        f"- Title column: {db_config.schema.title_column}",
        "",
        "## Tables",
        "",
    ])

    for table in stats.get("tables", []):
        if not table.startswith("_") and not table.endswith("_fts"):
            lines.append(f"- {table}")

    lines.extend([
        "",
        "## Endpoints",
        "",
        f"- GET /db/{db_name}/fts?q=... - Full-text search",
        f"- GET /db/{db_name}/sql?query=... - SQL query",
        f"- GET /db/{db_name}/document/{{id}} - Get document",
        f"- GET /db/{db_name}/stats - Database statistics",
    ])

    if db_config.has_chroma():
        lines.append(f"- GET /db/{db_name}/chroma?q=... - Semantic search")

    return "\n".join(lines)


# Search endpoints

@app.get("/db/{db_name}/fts")
async def fts_search(
    db_name: str,
    q: str = Query(..., description="FTS5 search query"),
    limit: int = Query(20, ge=1, le=100),
    collection: Optional[str] = None,
    format: str = Query("text", pattern="^(text|json)$"),
):
    """Full-text search using FTS5."""
    async with open_db(db_name) as (db_config, conn):
        response = core_fts_search(
            conn=conn,
            query=q,
            limit=limit,
            documents_table=db_config.schema.documents_table,
            pk_column=db_config.schema.pk_column,
            title_column=db_config.schema.title_column,
            chunks_table=db_config.schema.chunks_table or "chunks",
            collection=collection,
        )

    if response.error:
        raise HTTPException(status_code=400, detail=response.error)

    if format == "json":
        # Convert core response to server response format
        results = [
            SearchResult(
                doc_id=r.doc_id,
                title=r.title,
                snippet=r.snippet,
                score=r.score,
                metadata={"chunk_index": r.chunk_index} if r.chunk_index is not None else {},
            )
            for r in response.results
        ]
        return SearchResponse(
            query=q,
            database=db_name,
            results=results,
            total=response.total,
        )

    # Text format (more token-efficient)
    return PlainTextResponse(format_search_results_text(response))


@app.get("/db/{db_name}/chroma")
async def chroma_search(
    db_name: str,
    q: str = Query(..., description="Natural language query"),
    limit: int = Query(10, ge=1, le=50),
    collection: Optional[str] = None,
    year_min: Optional[int] = None,
    year_max: Optional[int] = None,
    format: str = Query("text", pattern="^(text|json)$"),
):
    """Semantic search using Chroma vector store."""
    async with open_db(db_name) as (db_config, conn):
        response = core_chroma_search(
            conn=conn,
            query=q,
            chroma_path=str(db_config.chroma_path),
            limit=limit,
            documents_table=db_config.schema.documents_table,
            pk_column=db_config.schema.pk_column,
            title_column=db_config.schema.title_column,
            collection=collection,
            year_min=year_min,
            year_max=year_max,
        )

    if response.error:
        raise HTTPException(status_code=400, detail=response.error)

    if format == "json":
        # Convert core response to server response format
        results = [
            SearchResult(
                doc_id=r.doc_id,
                title=r.title,
                snippet=r.content[:200] + "..." if r.content and len(r.content) > 200 else r.content,
                score=r.score,
                metadata={
                    "chunk_index": r.chunk_index,
                    "total_chunks": r.total_chunks,
                    **r.metadata,
                },
            )
            for r in response.results
        ]
        return SearchResponse(
            query=q,
            database=db_name,
            results=results,
            total=response.total,
        )

    # Text format
    return PlainTextResponse(format_search_results_text(response))


@app.get("/db/{db_name}/sql")
@app.post("/db/{db_name}/sql")
async def sql_query_endpoint(
    db_name: str,
    query: Optional[str] = Query(None, description="SQL SELECT query"),
    format: str = Query("text", pattern="^(text|json)$"),
    request_body: Optional[SqlRequest] = None,
):
    """Execute SQL query (SELECT only)."""
    # Get query from either query param or request body
    sql = query or (request_body.query if request_body else None)
    fmt = (request_body.format if request_body else None) or format

    if not sql:
        raise HTTPException(status_code=400, detail="Query parameter required")

    async with open_db(db_name) as (_db_config, conn):
        result = core_sql_query(conn, sql)

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    if fmt == "json":
        return result

    # Text format
    return PlainTextResponse(format_sql_results_text(result))


# Document retrieval

@app.get("/db/{db_name}/document/{doc_id}")
async def get_document_endpoint(
    db_name: str,
    doc_id: str,
    format: str = Query("text", pattern="^(text|json)$"),
):
    """Get full document by ID."""
    async with open_db(db_name) as (db_config, conn):
        doc = core_get_document(
            conn=conn,
            doc_id=doc_id,
            documents_table=db_config.schema.documents_table,
            pk_column=db_config.schema.pk_column,
        )

    if not doc:
        raise HTTPException(status_code=404, detail=f"Document not found: {doc_id}")

    if format == "json":
        return doc.columns

    # Text format
    return PlainTextResponse(format_document_text(doc))


@app.get("/db/{db_name}/text/{doc_id}")
async def get_text(db_name: str, doc_id: str):
    """Get raw text content of a document."""
    async with open_db(db_name) as (db_config, conn):
        pk_col = db_config.schema.pk_column
        doc_table = db_config.schema.documents_table
        content_col = db_config.schema.content_column

        cursor = conn.execute(
            f"SELECT {content_col} FROM {doc_table} WHERE {pk_col} = ?",
            (doc_id,)
        )
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"Document not found: {doc_id}")

    return PlainTextResponse(row[0] or "")


# Statistics and metadata

@app.get("/db/{db_name}/stats")
async def db_stats(db_name: str):
    """Get database statistics."""
    db_config = _get_db_config(db_name)
    return db_config.get_stats()


@app.get("/db/{db_name}/collections")
async def list_collections(db_name: str):
    """List all collections in the database."""
    async with open_db(db_name) as (db_config, conn):
        # Try document_collections table first (normalized)
        try:
            cursor = conn.execute("""
                SELECT collection_name, COUNT(*) as count
                FROM document_collections
                GROUP BY collection_name
                ORDER BY count DESC
            """)
            collections = [{"name": row[0], "count": row[1]} for row in cursor.fetchall()]
            return {"collections": collections, "total": len(collections)}
        except sqlite3.OperationalError:
            pass

        # Fall back to collections column (denormalized)
        try:
            doc_table = db_config.schema.documents_table
            cursor = conn.execute(f"SELECT DISTINCT collections FROM {doc_table} WHERE collections IS NOT NULL")
            all_collections = set()
            for row in cursor.fetchall():
                if row[0]:
                    for coll in row[0].split(";"):
                        coll = coll.strip()
                        if coll:
                            all_collections.add(coll)
            return {"collections": sorted(all_collections), "total": len(all_collections)}
        except sqlite3.OperationalError:
            return {"collections": [], "total": 0}


# Legacy endpoints (backward compatible with original Doctrail server)

@app.post("/enrich", response_model=EnrichmentResponse, dependencies=[Depends(require_write_api)])
async def enrich(request: EnrichmentRequest):
    """
    Run enrichment tasks on database content using LLM processing.
    (Legacy endpoint for backward compatibility)
    """
    try:
        result = await run_enrichment(
            config_path=request.config_path,
            enrichments=request.enrichments,
            db_path=request.db_path,
            model=request.model,
            limit=request.limit,
            rowid=request.rowid,
            sha1=request.sha1,
            overwrite=request.overwrite,
            batch_size=request.batch_size,
            truncate=request.truncate,
            skip_cost_check=request.skip_cost_check,
            cost_threshold=request.cost_threshold,
            dry_run=request.dry_run,
            verbose=request.verbose,
            dedupe_scope=request.dedupe_scope,
            materialize_inputs=request.materialize_inputs,
            allow_column_collision=request.allow_column_collision,
        )
        return result

    except ConfigurationError as e:
        raise HTTPException(status_code=400, detail=f"Configuration error: {str(e)}")
    except DatabaseError as e:
        raise HTTPException(status_code=404, detail=f"Database error: {str(e)}")
    except EnrichmentError as e:
        raise HTTPException(status_code=422, detail=f"Enrichment error: {str(e)}")
    except Exception as e:
        logging.error(f"Unexpected error in /enrich: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.post("/ingest", response_model=IngestResponse, dependencies=[Depends(require_write_api)])
async def ingest(request: IngestRequest):
    """
    Ingest documents into database from various sources.
    (Legacy endpoint for backward compatibility)
    """
    try:
        result = await run_ingest(
            db_path=request.db_path,
            input_dirs=request.input_dirs,
            table=request.table,
            force=request.force,
            overwrite=request.overwrite,
            limit=request.limit,
            include_pattern=request.include_pattern,
            exclude_pattern=request.exclude_pattern,
            readability=request.readability,
            html_extractor=request.html_extractor,
            skip_garbage_check=request.skip_garbage_check,
            fulltext=request.fulltext,
            manifest_path=request.manifest_path,
            labels=request.labels,
            pdf_engine=request.pdf_engine,
            ocr_engine=request.ocr_engine,
            workers=request.workers,
            verbose=request.verbose,
            plugin_name=request.plugin_name,
            plugin_args=request.plugin_args,
            zotero_api_key=request.zotero_api_key,
            zotero_library_id=request.zotero_library_id,
            zotero_library_type=request.zotero_library_type,
            zotero_collection=request.zotero_collection,
        )
        return result

    except ConfigurationError as e:
        raise HTTPException(status_code=400, detail=f"Configuration error: {str(e)}")
    except DatabaseError as e:
        raise HTTPException(status_code=404, detail=f"Database error: {str(e)}")
    except Exception as e:
        logging.error(f"Unexpected error in /ingest: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.post("/export", response_model=ExportResponse, dependencies=[Depends(require_write_api)])
async def export(request: ExportRequest):
    """
    Export enriched data in various formats.
    (Legacy endpoint for backward compatibility)
    """
    try:
        result = await run_export(
            config_path=request.config_path,
            export_type=request.export_type,
            output_dir=request.output_dir,
            verbose=request.verbose,
        )
        return result

    except ConfigurationError as e:
        raise HTTPException(status_code=400, detail=f"Configuration error: {str(e)}")
    except Exception as e:
        logging.error(f"Unexpected error in /export: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.post("/list-enrichments", response_model=ListEnrichmentsResponse)
async def list_enrichments_endpoint(request: ListEnrichmentsRequest):
    """
    List all available enrichments from a configuration file.
    (Legacy endpoint for backward compatibility)
    """
    try:
        result = await list_enrichments(config_path=request.config_path)
        return result

    except ConfigurationError as e:
        raise HTTPException(status_code=400, detail=f"Configuration error: {str(e)}")
    except Exception as e:
        logging.error(f"Unexpected error in /list-enrichments: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    os.environ.setdefault("DOCTRAIL_SERVER_HOST", "127.0.0.1")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
