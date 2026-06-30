"""SQL query utilities."""

import re
import logging
from typing import Optional


def ensure_rowid_in_query(query: str) -> str:
    """
    Ensure that the query includes rowid in the SELECT clause.
    
    This is critical because the enrichment processing code expects rowid to be available
    for database updates. If a query uses 'SELECT *' without explicitly including rowid,
    it won't be available in the results, causing "NO_ROWID" errors.
    
    Args:
        query: The SQL query to check and modify if needed
        
    Returns:
        Modified query that includes rowid in the SELECT clause
    """
    # Check if query already includes rowid explicitly
    if re.search(r'\browid\b', query, re.IGNORECASE):
        logging.debug("Query already includes rowid explicitly")
        return query
    
    # Look for SELECT * pattern and replace with SELECT rowid, *
    select_star_pattern = r'(SELECT\s+)\*(\s+FROM)'
    if re.search(select_star_pattern, query, re.IGNORECASE):
        modified_query = re.sub(
            select_star_pattern, 
            r'\1rowid, *\2', 
            query, 
            flags=re.IGNORECASE
        )
        logging.debug(f"Modified query to include rowid: {query} -> {modified_query}")
        return modified_query
    
    # If it's not a SELECT * query, we assume rowid is already included or not needed
    logging.debug("Query doesn't use SELECT *, assuming rowid is handled appropriately")
    return query


def apply_null_filters(query: str, column: str, overwrite: bool) -> str:
    """Apply NULL filters for append/overwrite mode.
    
    Args:
        query: SQL query to modify
        column: Column name to filter
        overwrite: If True, remove NULL filters; if False, add NULL filters
        
    Returns:
        Modified query
    """
    if overwrite:
        # Remove existing NULL filters
        patterns = [
            rf'WHERE\s+{re.escape(column)}\s+IS\s+NULL\s+AND',
            rf'WHERE\s+{re.escape(column)}\s+IS\s+NULL(?=\s|$)',
            rf'AND\s+{re.escape(column)}\s+IS\s+NULL(?=\s|$)'
        ]
        for pattern in patterns:
            if re.search(pattern, query, re.IGNORECASE):
                query = re.sub(pattern, 'WHERE 1=1', query, flags=re.IGNORECASE)
    else:
        # Add NULL filter for append mode
        if not re.search(rf'{re.escape(column)}\s+IS\s+NULL', query, re.IGNORECASE):
            if 'WHERE' in query.upper():
                query = re.sub(r'(\s+WHERE\s+)', rf'\1{column} IS NULL AND ', 
                             query, flags=re.IGNORECASE)
            else:
                query += f' WHERE {column} IS NULL'
    
    return query


def add_order_and_limit(query: str, limit: Optional[int] = None) -> str:
    """Add ORDER BY and LIMIT clauses to query.
    
    Args:
        query: SQL query to modify
        limit: Optional limit value
        
    Returns:
        Modified query with ORDER BY and optional LIMIT
    """
    # Add ORDER BY if not present
    if 'ORDER BY' not in query.upper():
        if 'LIMIT' in query.upper():
            query = re.sub(r'(\s+LIMIT\s+)', r' ORDER BY rowid\1', 
                         query, flags=re.IGNORECASE)
        else:
            query += ' ORDER BY rowid'
    
    # Add or update LIMIT
    if limit:
        if 'LIMIT' not in query.upper():
            query += f' LIMIT {limit}'
        else:
            query = re.sub(r'LIMIT\s+\d+', f'LIMIT {limit}', 
                         query, flags=re.IGNORECASE)
    
    return query