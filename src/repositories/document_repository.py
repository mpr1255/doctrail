"""Repository for document-related database operations."""

from typing import List, Dict, Optional, Any
from datetime import datetime

from .base_repository import BaseRepository
from ..types import RowDict, DatabaseUpdate


class DocumentRepository(BaseRepository):
    """Repository for document operations."""
    
    def get_documents(
        self,
        table: str,
        where_clause: Optional[str] = None,
        limit: Optional[int] = None,
        order_by: str = "rowid"
    ) -> List[RowDict]:
        """Get documents from table.
        
        Args:
            table: Table name
            where_clause: Optional WHERE clause (without WHERE keyword)
            limit: Optional limit
            order_by: Order by clause
            
        Returns:
            List of document records
        """
        query = f"SELECT rowid, * FROM {table}"
        
        if where_clause:
            query += f" WHERE {where_clause}"
        
        query += f" ORDER BY {order_by}"
        
        if limit:
            query += f" LIMIT {limit}"
        
        return self.execute_query(query)
    
    def update_document_column(
        self,
        table: str,
        column: str,
        rowid: int,
        value: Any
    ) -> None:
        """Update a single column in a document.
        
        Args:
            table: Table name
            column: Column name
            rowid: Row ID
            value: New value
        """
        # Ensure column exists
        if not self.column_exists(table, column):
            self.add_column(table, column, "TEXT")
        
        # Update value
        query = f"UPDATE {table} SET {column} = ?, metadata_updated = ? WHERE rowid = ?"
        params = (value, datetime.now().isoformat(), rowid)
        
        self.execute_update(query, params)
    
    def batch_update_column(
        self,
        table: str,
        column: str,
        updates: List[DatabaseUpdate]
    ) -> int:
        """Batch update a column for multiple documents.
        
        Args:
            table: Table name
            column: Column name
            updates: List of update dictionaries
            
        Returns:
            Number of rows updated
        """
        # Ensure column exists
        if not self.column_exists(table, column):
            self.add_column(table, column, "TEXT")
        
        # Ensure metadata column exists
        if not self.column_exists(table, "metadata_updated"):
            self.add_column(table, "metadata_updated", "TIMESTAMP")
        
        # Perform updates
        updated_count = 0
        current_time = datetime.now().isoformat()
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            for update in updates:
                if update.get('updated') is not None:
                    query = f"""
                        UPDATE {table} 
                        SET {column} = ?, metadata_updated = ?
                        WHERE rowid = ?
                    """
                    cursor.execute(
                        query,
                        (update['updated'], current_time, update['rowid'])
                    )
                    updated_count += cursor.rowcount
            
            conn.commit()
        
        return updated_count
    
    def add_column(self, table: str, column: str, column_type: str = "TEXT") -> None:
        """Add a column to a table.
        
        Args:
            table: Table name
            column: Column name
            column_type: SQL column type
        """
        query = f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
        self.execute_update(query)
    
    def ensure_table_columns(
        self,
        table: str,
        columns: List[str],
        column_types: Optional[Dict[str, str]] = None
    ) -> None:
        """Ensure columns exist in table.
        
        Args:
            table: Table name
            columns: List of column names
            column_types: Optional dict of column name to SQL type
        """
        column_types = column_types or {}
        
        for column in columns:
            if not self.column_exists(table, column):
                col_type = column_types.get(column, "TEXT")
                self.add_column(table, column, col_type)
    
    def get_document_by_sha1(self, table: str, sha1: str) -> Optional[RowDict]:
        """Get document by SHA1.
        
        Args:
            table: Table name
            sha1: Document SHA1
            
        Returns:
            Document record or None
        """
        query = f"SELECT rowid, * FROM {table} WHERE sha1 = ?"
        results = self.execute_query(query, (sha1,))
        return results[0] if results else None
    
    def get_document_by_rowid(self, table: str, rowid: int) -> Optional[RowDict]:
        """Get document by rowid.
        
        Args:
            table: Table name
            rowid: Row ID
            
        Returns:
            Document record or None
        """
        query = f"SELECT rowid, * FROM {table} WHERE rowid = ?"
        results = self.execute_query(query, (rowid,))
        return results[0] if results else None