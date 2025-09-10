"""Base repository for database operations."""

from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any
from contextlib import contextmanager
import sqlite3

from ..db_operations import get_db_connection
from ..types import RowDict


class BaseRepository(ABC):
    """Base repository class for database operations."""
    
    def __init__(self, db_path: str):
        """Initialize repository with database path.
        
        Args:
            db_path: Path to SQLite database
        """
        self.db_path = db_path
    
    @contextmanager
    def get_connection(self):
        """Get database connection context manager."""
        with get_db_connection(self.db_path) as conn:
            yield conn
    
    def execute_query(self, query: str, params: Optional[tuple] = None) -> List[RowDict]:
        """Execute a query and return results as list of dicts.
        
        Args:
            query: SQL query
            params: Optional query parameters
            
        Returns:
            List of row dictionaries
        """
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            
            return [dict(row) for row in cursor.fetchall()]
    
    def execute_scalar(self, query: str, params: Optional[tuple] = None) -> Any:
        """Execute a query and return scalar result.
        
        Args:
            query: SQL query
            params: Optional query parameters
            
        Returns:
            Scalar value or None
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            if params:
                result = cursor.execute(query, params).fetchone()
            else:
                result = cursor.execute(query).fetchone()
            
            return result[0] if result else None
    
    def execute_update(self, query: str, params: Optional[tuple] = None) -> int:
        """Execute an update query and return affected rows.
        
        Args:
            query: SQL update/insert/delete query
            params: Optional query parameters
            
        Returns:
            Number of affected rows
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            
            conn.commit()
            return cursor.rowcount
    
    def table_exists(self, table_name: str) -> bool:
        """Check if a table exists.
        
        Args:
            table_name: Name of table to check
            
        Returns:
            True if table exists
        """
        query = "SELECT name FROM sqlite_master WHERE type='table' AND name=?"
        result = self.execute_scalar(query, (table_name,))
        return result is not None
    
    def column_exists(self, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a table.
        
        Args:
            table_name: Table name
            column_name: Column name
            
        Returns:
            True if column exists
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = [info[1] for info in cursor.fetchall()]
            return column_name in columns