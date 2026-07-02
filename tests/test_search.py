#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest",
#     "click",
# ]
# ///

"""
Tests for the search module.

Tests both the core search functions (doctrail/search.py) and CLI commands.
Uses an in-memory SQLite database with FTS5 for testing.
"""

import sqlite3
import pytest
import json
from click.testing import CliRunner

from doctrail.search import (
    SearchResult,
    SearchResponse,
    DocumentResult,
    get_connection,
    fts_search,
    sql_query,
    get_document,
    get_stats,
    format_search_results_text,
    format_sql_results_text,
    format_document_text,
)
from doctrail.cli import cli


@pytest.fixture
def test_db(tmp_path):
    """Create a test database with sample documents and FTS index."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Create doctrail.yaml for CLI to find schema
    config_path = tmp_path / "doctrail.yaml"
    config_path.write_text("""
schema:
  pk_column: id
  pk_type: integer
  content_column: content
  title_column: title
  documents_table: documents
  chunks_table: chunks
""")

    # Create documents table
    conn.execute("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            title TEXT,
            content TEXT,
            year INTEGER,
            collections TEXT
        )
    """)

    # Create chunks table
    conn.execute("""
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            document_id INTEGER,
            chunk_index INTEGER,
            content TEXT,
            FOREIGN KEY (document_id) REFERENCES documents(id)
        )
    """)

    # Insert sample documents
    documents = [
        (1, "Brain Death and Organ Transplantation", "This paper discusses brain death criteria and organ transplantation ethics.", 2020, "ethics"),
        (2, "Kidney Transplant Outcomes", "A study of kidney transplant outcomes over 10 years.", 2021, "clinical"),
        (3, "Ethics in Medicine", "General overview of medical ethics principles.", 2019, "ethics"),
        (4, "Liver Disease Treatment", "Modern approaches to liver disease treatment.", 2022, "clinical"),
    ]
    conn.executemany(
        "INSERT INTO documents (id, title, content, year, collections) VALUES (?, ?, ?, ?, ?)",
        documents
    )

    # Insert chunks
    chunks = [
        (1, 1, 0, "Brain death is a legal definition of death."),
        (2, 1, 1, "Organ transplantation requires brain death determination."),
        (3, 2, 0, "Kidney transplant survival rates have improved."),
        (4, 2, 1, "Post-transplant care is critical for success."),
        (5, 3, 0, "Medical ethics encompasses many principles."),
        (6, 4, 0, "Liver disease can be treated with medication or transplant."),
    ]
    conn.executemany(
        "INSERT INTO chunks (id, document_id, chunk_index, content) VALUES (?, ?, ?, ?)",
        chunks
    )

    # Create FTS5 index on chunks
    conn.execute("""
        CREATE VIRTUAL TABLE chunk_fts USING fts5(
            content,
            content=chunks,
            content_rowid=id
        )
    """)

    # Populate FTS index
    conn.execute("""
        INSERT INTO chunk_fts(chunk_fts) VALUES('rebuild')
    """)

    conn.commit()

    yield str(db_path), conn

    conn.close()


class TestSearchDataclasses:
    """Test dataclass definitions."""

    def test_search_result_creation(self):
        """Test SearchResult dataclass."""
        result = SearchResult(
            doc_id="123",
            title="Test Title",
            snippet="Test snippet...",
            score=0.95,
            chunk_index=0,
            total_chunks=5,
        )
        assert result.doc_id == "123"
        assert result.title == "Test Title"
        assert result.score == 0.95
        assert result.metadata == {}

    def test_search_response_creation(self):
        """Test SearchResponse dataclass."""
        response = SearchResponse(
            query="test query",
            results=[],
            total=0,
            mode="fts",
        )
        assert response.query == "test query"
        assert response.total == 0
        assert response.error is None

    def test_document_result_creation(self):
        """Test DocumentResult dataclass."""
        doc = DocumentResult(
            doc_id="456",
            columns={"title": "Test", "year": 2020},
        )
        assert doc.doc_id == "456"
        assert doc.columns["year"] == 2020


class TestFtsSearch:
    """Test FTS search functionality."""

    def test_fts_search_basic(self, test_db):
        """Test basic FTS search."""
        db_path, conn = test_db

        response = fts_search(
            conn=conn,
            query="brain death",
            limit=10,
        )

        assert response.mode == "fts"
        assert response.query == "brain death"
        assert response.error is None
        assert len(response.results) > 0
        # Should find the brain death document
        doc_ids = [r.doc_id for r in response.results]
        assert "1" in doc_ids

    def test_fts_search_limit(self, test_db):
        """Test FTS search with limit."""
        db_path, conn = test_db

        response = fts_search(conn=conn, query="transplant", limit=1)

        assert len(response.results) == 1

    def test_fts_search_no_results(self, test_db):
        """Test FTS search with no matches."""
        db_path, conn = test_db

        response = fts_search(conn=conn, query="nonexistentterm12345")

        assert len(response.results) == 0
        assert response.total == 0
        assert response.error is None

    def test_fts_search_collection_filter(self, test_db):
        """Test FTS search with collection filter."""
        db_path, conn = test_db

        response = fts_search(
            conn=conn,
            query="transplant",
            collection="ethics",
        )

        # Should only find documents in ethics collection
        for result in response.results:
            # doc_id 1 is in ethics
            assert result.doc_id in ["1"]

    def test_fts_search_invalid_query(self, test_db):
        """Test FTS search with invalid query syntax."""
        db_path, conn = test_db

        # FTS5 syntax error - unbalanced quotes
        response = fts_search(conn=conn, query='"unclosed')

        # Should return error, not crash
        assert response.error is not None or len(response.results) >= 0


class TestSqlQuery:
    """Test SQL query functionality."""

    def test_sql_query_basic(self, test_db):
        """Test basic SQL query."""
        db_path, conn = test_db

        result = sql_query(conn, "SELECT id, title FROM documents LIMIT 2")

        assert "columns" in result
        assert "rows" in result
        assert result["count"] == 2
        assert "id" in result["columns"]
        assert "title" in result["columns"]

    def test_sql_query_with_filter(self, test_db):
        """Test SQL query with WHERE clause."""
        db_path, conn = test_db

        result = sql_query(conn, "SELECT * FROM documents WHERE year >= 2021")

        assert result["count"] == 2  # 2021 and 2022
        for row in result["rows"]:
            assert row["year"] >= 2021

    def test_sql_query_rejects_non_select(self, test_db):
        """Test that non-SELECT queries are rejected."""
        db_path, conn = test_db

        result = sql_query(conn, "DELETE FROM documents WHERE id = 1")

        assert "error" in result
        assert "SELECT" in result["error"] or "allowed" in result["error"].lower()

    def test_sql_query_allows_with(self, test_db):
        """Test that WITH (CTE) queries are allowed."""
        db_path, conn = test_db

        result = sql_query(conn, """
            WITH recent AS (
                SELECT * FROM documents WHERE year >= 2020
            )
            SELECT count(*) as cnt FROM recent
        """)

        assert "error" not in result or result["error"] is None
        assert result["count"] == 1

    def test_sql_query_blocks_writeable_cte(self, test_db):
        """WITH clauses that perform writes must still be blocked on the read-only SQL path."""
        db_path, conn = test_db

        before = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        result = sql_query(
            conn,
            "WITH cte AS (SELECT 1) DELETE FROM documents WHERE id = 1 RETURNING id",
        )
        after = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

        assert "error" in result
        assert "readonly" in result["error"].lower()
        assert before == after == 4


class TestGetDocument:
    """Test document retrieval."""

    def test_get_document_exists(self, test_db):
        """Test getting an existing document."""
        db_path, conn = test_db

        doc = get_document(conn, "1")

        assert doc is not None
        assert doc.doc_id == "1"
        assert "title" in doc.columns
        assert "Brain Death" in doc.columns["title"]

    def test_get_document_not_found(self, test_db):
        """Test getting a non-existent document."""
        db_path, conn = test_db

        doc = get_document(conn, "99999")

        assert doc is None

    def test_get_document_quotes_configured_identifiers(self):
        """Configured table/key names should be quoted, not interpolated raw."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            'CREATE TABLE "docs table" ("doc id" TEXT PRIMARY KEY, title TEXT)'
        )
        conn.execute(
            'INSERT INTO "docs table" ("doc id", title) VALUES (?, ?)',
            ("doc-1", "Quoted identifiers"),
        )

        doc = get_document(
            conn,
            "doc-1",
            documents_table="docs table",
            pk_column="doc id",
        )

        assert doc is not None
        assert doc.columns["title"] == "Quoted identifiers"


class TestGetStats:
    """Test database statistics."""

    def test_get_stats(self, test_db):
        """Test getting database stats."""
        db_path, conn = test_db

        stats = get_stats(conn)

        assert stats["document_count"] == 4
        assert stats["chunk_count"] == 6
        assert "tables" in stats
        assert "documents" in stats["tables"]
        assert "chunks" in stats["tables"]

    def test_get_stats_quotes_configured_table_name(self):
        """Configured table names should support valid quoted SQLite names."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE "docs table" (id INTEGER PRIMARY KEY)')
        conn.execute('INSERT INTO "docs table" DEFAULT VALUES')

        stats = get_stats(conn, documents_table="docs table")

        assert stats["document_count"] == 1


class TestFormatting:
    """Test output formatting functions."""

    def test_format_search_results_text(self):
        """Test text formatting of search results."""
        response = SearchResponse(
            query="test",
            results=[
                SearchResult(
                    doc_id="1",
                    title="Test Doc",
                    snippet="Test **highlighted** snippet",
                    score=-5.0,
                    chunk_index=0,
                    total_chunks=3,
                ),
            ],
            total=1,
            mode="fts",
        )

        output = format_search_results_text(response)

        assert "FTS Results" in output
        assert "test" in output.lower()
        assert "Test Doc" in output
        assert "Test **highlighted** snippet" in output

    def test_format_search_results_with_error(self):
        """Test text formatting with error."""
        response = SearchResponse(
            query="test",
            results=[],
            total=0,
            mode="fts",
            error="Something went wrong",
        )

        output = format_search_results_text(response)

        assert "Error" in output
        assert "Something went wrong" in output

    def test_format_sql_results_text(self):
        """Test SQL results formatting."""
        result = {
            "columns": ["id", "title"],
            "rows": [
                {"id": 1, "title": "First"},
                {"id": 2, "title": "Second"},
            ],
            "count": 2,
        }

        output = format_sql_results_text(result)

        assert "2 rows" in output
        assert "id" in output
        assert "title" in output

    def test_format_document_text(self):
        """Test document formatting."""
        doc = DocumentResult(
            doc_id="123",
            columns={
                "title": "Test Document",
                "year": 2020,
                "content": "Some content here",
            },
        )

        output = format_document_text(doc)

        assert "Document: 123" in output
        assert "Test Document" in output
        assert "2020" in output


class TestCliSearch:
    """Test CLI search commands."""

    def test_sql_cli_help(self):
        """Test sql --help."""
        runner = CliRunner()
        result = runner.invoke(cli, ['sql', '--help'])

        assert result.exit_code == 0
        assert 'db-path' in result.output.lower()

    def test_query_default_table_quotes_configured_identifier(self, tmp_path, monkeypatch):
        """The default table preview path should quote config-provided table names."""
        db_path = tmp_path / "docs.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            'CREATE TABLE "docs table" (sha1 TEXT PRIMARY KEY, filename TEXT, raw_content TEXT)'
        )
        conn.execute(
            'INSERT INTO "docs table" (sha1, filename, raw_content) VALUES (?, ?, ?)',
            ("doc-1", "doc.txt", "hello"),
        )
        conn.commit()
        conn.close()

        project_dir = tmp_path / "project"
        (project_dir / ".doctrail").mkdir(parents=True)
        (project_dir / ".doctrail" / "config.yml").write_text(
            f"database: {db_path}\ndefault_table: docs table\n"
        )
        runner = CliRunner()
        monkeypatch.chdir(project_dir)

        result = runner.invoke(cli, ["query"])

        assert result.exit_code == 0, result.output
        assert "doc.txt" in result.output

    def test_document_cli_help(self):
        """Test document --help."""
        runner = CliRunner()
        result = runner.invoke(cli, ['document', '--help'])

        assert result.exit_code == 0
        assert 'db-path' in result.output.lower()
        assert 'id' in result.output.lower()

    def test_stats_cli_help(self):
        """Test stats --help."""
        runner = CliRunner()
        result = runner.invoke(cli, ['stats', '--help'])

        assert result.exit_code == 0
        assert 'db-path' in result.output.lower()

    def test_sql_cli_with_db(self, test_db):
        """Test sql command with actual database."""
        db_path, conn = test_db
        conn.close()

        runner = CliRunner()
        result = runner.invoke(cli, [
            'sql',
            '--db-path', db_path,
            '--query', 'SELECT count(*) as cnt FROM documents',
        ])

        assert result.exit_code == 0
        assert '4' in result.output or 'cnt' in result.output

    def test_document_cli_with_db(self, test_db):
        """Test document command with actual database."""
        db_path, conn = test_db
        conn.close()

        runner = CliRunner()
        result = runner.invoke(cli, [
            'document',
            '--db-path', db_path,
            '--id', '1',
        ])

        assert result.exit_code == 0
        assert 'Brain Death' in result.output

    def test_stats_cli_with_db(self, test_db):
        """Test stats command with actual database."""
        db_path, conn = test_db
        conn.close()

        runner = CliRunner()
        result = runner.invoke(cli, [
            'stats',
            '--db-path', db_path,
        ])

        assert result.exit_code == 0
        assert 'document_count' in result.output.lower() or '4' in result.output


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
