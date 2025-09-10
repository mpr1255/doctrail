"""Unit tests for query utilities."""

import pytest
from src.utils.query_utils import (
    ensure_rowid_in_query, apply_null_filters, add_order_and_limit
)


class TestEnsureRowidInQuery:
    """Test ensure_rowid_in_query function."""
    
    def test_query_with_rowid(self):
        """Test query that already has rowid."""
        query = "SELECT rowid, * FROM documents"
        result = ensure_rowid_in_query(query)
        assert result == query
    
    def test_query_without_rowid(self):
        """Test query without rowid."""
        query = "SELECT * FROM documents"
        result = ensure_rowid_in_query(query)
        assert result == "SELECT rowid, * FROM documents"
    
    def test_query_with_specific_columns(self):
        """Test query with specific columns."""
        query = "SELECT id, name FROM documents"
        result = ensure_rowid_in_query(query)
        assert result == query  # Doesn't modify non-SELECT * queries
    
    def test_case_insensitive(self):
        """Test case insensitive matching."""
        query = "select * from documents"
        result = ensure_rowid_in_query(query)
        assert result == "select rowid, * from documents"


class TestApplyNullFilters:
    """Test apply_null_filters function."""
    
    def test_overwrite_mode_removes_null_filter(self):
        """Test overwrite mode removes NULL filters."""
        query = "SELECT * FROM docs WHERE content IS NULL"
        result = apply_null_filters(query, "content", overwrite=True)
        assert result == "SELECT * FROM docs WHERE 1=1"
    
    def test_append_mode_adds_null_filter(self):
        """Test append mode adds NULL filter."""
        query = "SELECT * FROM docs"
        result = apply_null_filters(query, "content", overwrite=False)
        assert result == "SELECT * FROM docs WHERE content IS NULL"
    
    def test_append_mode_with_existing_where(self):
        """Test append mode with existing WHERE clause."""
        query = "SELECT * FROM docs WHERE active = 1"
        result = apply_null_filters(query, "content", overwrite=False)
        assert "content IS NULL AND" in result
    
    def test_append_mode_with_existing_null_filter(self):
        """Test append mode doesn't duplicate NULL filter."""
        query = "SELECT * FROM docs WHERE content IS NULL"
        result = apply_null_filters(query, "content", overwrite=False)
        assert result == query


class TestAddOrderAndLimit:
    """Test add_order_and_limit function."""
    
    def test_add_order_by(self):
        """Test adding ORDER BY clause."""
        query = "SELECT * FROM docs"
        result = add_order_and_limit(query, None)
        assert result == "SELECT * FROM docs ORDER BY rowid"
    
    def test_add_limit(self):
        """Test adding LIMIT clause."""
        query = "SELECT * FROM docs"
        result = add_order_and_limit(query, 10)
        assert result == "SELECT * FROM docs ORDER BY rowid LIMIT 10"
    
    def test_existing_order_by(self):
        """Test with existing ORDER BY."""
        query = "SELECT * FROM docs ORDER BY created_at"
        result = add_order_and_limit(query, 10)
        assert result == "SELECT * FROM docs ORDER BY created_at LIMIT 10"
    
    def test_update_existing_limit(self):
        """Test updating existing LIMIT."""
        query = "SELECT * FROM docs LIMIT 100"
        result = add_order_and_limit(query, 10)
        assert "LIMIT 10" in result
        assert "LIMIT 100" not in result
    
    def test_order_before_limit(self):
        """Test ORDER BY is added before LIMIT."""
        query = "SELECT * FROM docs LIMIT 50"
        result = add_order_and_limit(query, None)
        assert "ORDER BY rowid LIMIT 50" in result