"""
Core search functions for doctrail.

This module provides the actual search logic used by both CLI and server.
All functions are pure - they take a database connection and return data structures.
Formatting for output (text/JSON) is handled by the caller.
"""

import sqlite3
import struct
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db_operations import _quote_identifier

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """A single search result."""
    doc_id: str
    title: Optional[str] = None
    snippet: Optional[str] = None
    content: Optional[str] = None
    score: Optional[float] = None
    chunk_index: Optional[int] = None
    total_chunks: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResponse:
    """Response from a search operation."""
    query: str
    results: List[SearchResult]
    total: int
    mode: str  # "fts", "chroma", "sql"
    error: Optional[str] = None


@dataclass
class DocumentResult:
    """A full document."""
    doc_id: str
    columns: Dict[str, Any]


def get_connection(db_path: str) -> sqlite3.Connection:
    """Get a database connection with appropriate settings."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def fts_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    documents_table: str = "documents",
    pk_column: str = "id",
    title_column: str = "title",
    chunks_table: str = "chunks",
    collection: Optional[str] = None,
) -> SearchResponse:
    """
    Full-text search using FTS5 on chunks.

    Args:
        conn: Database connection
        query: FTS5 match expression
        limit: Max results
        documents_table: Name of documents table
        pk_column: Primary key column name
        title_column: Title column name
        collection: Optional collection filter

    Returns:
        SearchResponse with results
    """
    # Check if chunks table exists (chunk-based FTS)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (chunks_table,),
    )
    has_chunks = cursor.fetchone() is not None

    documents_table_ref = _quote_identifier(documents_table, "documents table")
    pk_column_ref = _quote_identifier(pk_column, "primary key column")
    title_column_ref = _quote_identifier(title_column, "title column")
    chunks_table_ref = _quote_identifier(chunks_table, "chunks table")

    if has_chunks:
        # Search chunks with FTS
        sql = f"""
            SELECT
                d.{pk_column_ref} as doc_id,
                d.{title_column_ref} as title,
                c.chunk_index,
                (SELECT COUNT(*) FROM {chunks_table_ref} WHERE document_id = c.document_id) as total_chunks,
                snippet(chunk_fts, -1, '**', '**', '...', 32) as snippet,
                rank
            FROM chunk_fts
            JOIN {chunks_table_ref} c ON chunk_fts.rowid = c.id
            JOIN {documents_table_ref} d ON c.document_id = d.id
            WHERE chunk_fts MATCH ?
        """
    else:
        # Fall back to document-level FTS if exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='document_fts'"
        )
        if cursor.fetchone() is None:
            return SearchResponse(
                query=query,
                results=[],
                total=0,
                mode="fts",
                error="No FTS index found. Run 'doctrail sync' first."
            )

        sql = f"""
            SELECT
                d.{pk_column_ref} as doc_id,
                d.{title_column_ref} as title,
                snippet(document_fts, -1, '**', '**', '...', 32) as snippet,
                rank
            FROM document_fts
            JOIN {documents_table_ref} d ON document_fts.rowid = d.rowid
            WHERE document_fts MATCH ?
        """

    params: List[Any] = [query]

    if collection:
        sql += " AND d.collections LIKE ?"
        params.append(f"%{collection}%")

    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    try:
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
    except sqlite3.OperationalError as e:
        return SearchResponse(
            query=query,
            results=[],
            total=0,
            mode="fts",
            error=f"FTS query error: {e}"
        )

    results = []
    for row in rows:
        results.append(SearchResult(
            doc_id=str(row["doc_id"]),
            title=row["title"] if "title" in row.keys() else None,
            snippet=row["snippet"] if "snippet" in row.keys() else None,
            score=row["rank"] if "rank" in row.keys() else None,
            chunk_index=row["chunk_index"] if "chunk_index" in row.keys() else None,
            total_chunks=row["total_chunks"] if "total_chunks" in row.keys() else None,
        ))

    return SearchResponse(
        query=query,
        results=results,
        total=len(results),
        mode="fts",
    )


def chroma_search(
    conn: sqlite3.Connection,
    query: str,
    chroma_path: str,
    limit: int = 10,
    documents_table: str = "documents",
    pk_column: str = "id",
    title_column: str = "title",
    collection: Optional[str] = None,
    year_min: Optional[int] = None,
    year_max: Optional[int] = None,
) -> SearchResponse:
    """
    Semantic search using Chroma vector store.

    Requires OPENAI_API_KEY environment variable for embedding generation.

    Args:
        conn: Database connection
        query: Natural language query
        chroma_path: Path to Chroma database directory
        limit: Max results
        documents_table: Name of documents table
        pk_column: Primary key column name
        title_column: Title column name
        collection: Optional collection filter
        year_min: Optional minimum year filter
        year_max: Optional maximum year filter

    Returns:
        SearchResponse with results
    """
    import os

    # Check for OpenAI API key
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return SearchResponse(
            query=query,
            results=[],
            total=0,
            mode="chroma",
            error="OPENAI_API_KEY not set. Required for semantic search."
        )

    # Check Chroma path
    if not Path(chroma_path).exists():
        return SearchResponse(
            query=query,
            results=[],
            total=0,
            mode="chroma",
            error=f"Chroma database not found: {chroma_path}. Run 'doctrail sync' first."
        )

    try:
        import chromadb
        from openai import OpenAI
    except ImportError as e:
        return SearchResponse(
            query=query,
            results=[],
            total=0,
            mode="chroma",
            error=f"Missing dependency: {e}. Install with 'pip install chromadb openai'."
        )

    # Initialize clients
    openai_client = OpenAI(api_key=api_key)
    chroma_client = chromadb.PersistentClient(path=chroma_path)

    try:
        chroma_collection = chroma_client.get_collection("chunks")
    except Exception as e:
        return SearchResponse(
            query=query,
            results=[],
            total=0,
            mode="chroma",
            error=f"Chroma collection 'chunks' not found: {e}"
        )

    # Generate query embedding
    try:
        response = openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=[query]
        )
        embedding = response.data[0].embedding
    except Exception as e:
        return SearchResponse(
            query=query,
            results=[],
            total=0,
            mode="chroma",
            error=f"Embedding generation failed: {e}"
        )

    # Build filter for eligible document IDs if needed
    filter_ids = None
    if collection or year_min or year_max:
        conditions = []
        params = []
        if collection:
            conditions.append("collections LIKE ?")
            params.append(f"%{collection}%")
        if year_min:
            conditions.append("year >= ?")
            params.append(year_min)
        if year_max:
            conditions.append("year <= ?")
            params.append(year_max)

        where_clause = " AND ".join(conditions)
        rows = conn.execute(
            f"SELECT id FROM {documents_table} WHERE {where_clause}",
            params
        ).fetchall()
        eligible_doc_ids = {row["id"] for row in rows}

        if not eligible_doc_ids:
            return SearchResponse(
                query=query,
                results=[],
                total=0,
                mode="chroma",
            )

        # Get chunk IDs for these documents
        placeholders = ",".join("?" * len(eligible_doc_ids))
        chunk_rows = conn.execute(
            f"SELECT document_id, chunk_index FROM chunks WHERE document_id IN ({placeholders})",
            list(eligible_doc_ids)
        ).fetchall()
        filter_ids = [f"{row['document_id']}_{row['chunk_index']}" for row in chunk_rows]

        if not filter_ids:
            return SearchResponse(
                query=query,
                results=[],
                total=0,
                mode="chroma",
            )

    # Query Chroma
    if filter_ids:
        chroma_results = chroma_collection.query(
            query_embeddings=[embedding],
            n_results=min(limit, len(filter_ids)),
            ids=filter_ids,
            include=["metadatas", "distances"]
        )
    else:
        chroma_results = chroma_collection.query(
            query_embeddings=[embedding],
            n_results=limit,
            include=["metadatas", "distances"]
        )

    if not chroma_results["ids"] or not chroma_results["ids"][0]:
        return SearchResponse(
            query=query,
            results=[],
            total=0,
            mode="chroma",
        )

    # Fetch content from SQLite
    results = []
    for composite_id, distance in zip(chroma_results["ids"][0], chroma_results["distances"][0]):
        try:
            parts = composite_id.split("_")
            if len(parts) == 2:
                doc_id = int(parts[0])
                chunk_idx = int(parts[1])
        except (ValueError, IndexError):
            continue

        row = conn.execute(f"""
            SELECT
                c.content,
                c.chunk_index,
                c.document_id,
                d.{pk_column} as doc_pk,
                d.{title_column} as title,
                (SELECT COUNT(*) FROM chunks WHERE document_id = c.document_id) as total_chunks
            FROM chunks c
            JOIN {documents_table} d ON d.id = c.document_id
            WHERE c.document_id = ? AND c.chunk_index = ?
        """, (doc_id, chunk_idx)).fetchone()

        if row:
            results.append(SearchResult(
                doc_id=str(row["doc_pk"]),
                title=row["title"],
                content=row["content"],
                score=distance,
                chunk_index=row["chunk_index"],
                total_chunks=row["total_chunks"],
                metadata={"document_id": row["document_id"]},
            ))

    return SearchResponse(
        query=query,
        results=results,
        total=len(results),
        mode="chroma",
    )


def sql_query(
    conn: sqlite3.Connection,
    query: str,
) -> Dict[str, Any]:
    """
    Execute a read-only SQL query.

    Only SELECT and WITH queries are allowed for safety.

    Args:
        conn: Database connection
        query: SQL query string

    Returns:
        Dict with columns, rows, and count
    """
    query_upper = query.strip().upper()
    if not query_upper.startswith("SELECT") and not query_upper.startswith("WITH"):
        return {
            "error": "Only SELECT and WITH queries are allowed",
            "columns": [],
            "rows": [],
            "count": 0,
        }

    try:
        previous_query_only = conn.execute("PRAGMA query_only").fetchone()[0]
    except sqlite3.Error:
        previous_query_only = 0

    try:
        if not previous_query_only:
            conn.execute("PRAGMA query_only=ON")

        cursor = conn.execute(query)
        rows = cursor.fetchall()

        if not rows:
            return {
                "columns": [],
                "rows": [],
                "count": 0,
            }

        columns = list(rows[0].keys())
        return {
            "columns": columns,
            "rows": [dict(row) for row in rows],
            "count": len(rows),
        }
    except sqlite3.OperationalError as e:
        return {
            "error": f"SQL error: {e}",
            "columns": [],
            "rows": [],
            "count": 0,
        }
    finally:
        if not previous_query_only:
            try:
                conn.execute("PRAGMA query_only=OFF")
            except sqlite3.Error:
                logger.debug("Could not restore PRAGMA query_only to OFF", exc_info=True)


def get_document(
    conn: sqlite3.Connection,
    doc_id: str,
    documents_table: str = "documents",
    pk_column: str = "id",
) -> Optional[DocumentResult]:
    """
    Get a single document by ID.

    Args:
        conn: Database connection
        doc_id: Document ID (primary key value)
        documents_table: Name of documents table
        pk_column: Primary key column name

    Returns:
        DocumentResult or None if not found
    """
    try:
        cursor = conn.execute(
            f"SELECT * FROM {documents_table} WHERE {pk_column} = ?",
            (doc_id,)
        )
        row = cursor.fetchone()

        if not row:
            return None

        return DocumentResult(
            doc_id=str(doc_id),
            columns=dict(row),
        )
    except sqlite3.OperationalError:
        return None


def get_stats(
    conn: sqlite3.Connection,
    documents_table: str = "documents",
) -> Dict[str, Any]:
    """
    Get database statistics.

    Args:
        conn: Database connection
        documents_table: Name of documents table

    Returns:
        Dict with counts and table info
    """
    stats = {}

    try:
        # Document count
        cursor = conn.execute(f"SELECT COUNT(*) FROM {documents_table}")
        stats["document_count"] = cursor.fetchone()[0]

        # Chunk count
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'"
        )
        if cursor.fetchone():
            cursor = conn.execute("SELECT COUNT(*) FROM chunks")
            stats["chunk_count"] = cursor.fetchone()[0]

        # Embedding count
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chunk_vectors'"
        )
        if cursor.fetchone():
            cursor = conn.execute("SELECT COUNT(*) FROM chunk_vectors")
            stats["embedding_count"] = cursor.fetchone()[0]

        # Tables
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        stats["tables"] = [row[0] for row in cursor.fetchall()]

    except sqlite3.OperationalError as e:
        stats["error"] = str(e)

    return stats


# Output formatting utilities

def format_search_results_text(response: SearchResponse) -> str:
    """Format search results as plain text (token-efficient for LLMs)."""
    if response.error:
        return f"Error: {response.error}"

    lines = [
        f"# {response.mode.upper()} Results for '{response.query}'",
        f"Found {response.total} results",
        "",
    ]

    for i, r in enumerate(response.results, 1):
        lines.append(f"=== Result {i}/{response.total} ===")
        lines.append(f"ID: {r.doc_id}")
        if r.title:
            lines.append(f"Title: {r.title}")
        if r.chunk_index is not None and r.total_chunks:
            lines.append(f"Chunk: {r.chunk_index}/{r.total_chunks}")
        if r.score is not None:
            score_label = "Distance" if response.mode == "chroma" else "Score"
            lines.append(f"{score_label}: {r.score:.3f}")
        if r.snippet:
            lines.append(f"Snippet: {r.snippet}")
        if r.content:
            lines.append("")
            lines.append(r.content)
        lines.append("")

    return "\n".join(lines)


def format_sql_results_text(result: Dict[str, Any]) -> str:
    """Format SQL results as plain text."""
    if result.get("error"):
        return f"Error: {result['error']}"

    if not result["rows"]:
        return "No results"

    lines = [f"# SQL Results ({result['count']} rows)", ""]

    # Header
    lines.append(" | ".join(result["columns"]))
    lines.append("-" * 80)

    # Rows (truncate long values)
    for row in result["rows"]:
        values = []
        for col in result["columns"]:
            val = str(row.get(col, ""))
            if len(val) > 50:
                val = val[:47] + "..."
            values.append(val)
        lines.append(" | ".join(values))

    return "\n".join(lines)


def format_document_text(doc: Optional[DocumentResult]) -> str:
    """Format a document as plain text."""
    if not doc:
        return "Document not found"

    lines = [f"# Document: {doc.doc_id}", ""]

    for key, value in doc.columns.items():
        if value is not None:
            val_str = str(value)
            if len(val_str) > 500:
                # Truncate very long values but show beginning
                val_str = val_str[:500] + f"\n... ({len(val_str)} chars total)"
            lines.append(f"**{key}**: {val_str}")

    return "\n".join(lines)


__all__ = [
    "SearchResult",
    "SearchResponse",
    "DocumentResult",
    "get_connection",
    "fts_search",
    "chroma_search",
    "sql_query",
    "get_document",
    "get_stats",
    "format_search_results_text",
    "format_sql_results_text",
    "format_document_text",
]
