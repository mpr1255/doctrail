import sqlite3
import logging
import click
import time
import threading
import json
from datetime import datetime
from contextlib import contextmanager
from typing import List, Dict, Optional, Any, Tuple, Iterator, Union

from .constants import DEFAULT_BUSY_TIMEOUT, MAX_RETRY_ATTEMPTS, DEFAULT_KEY_COLUMN
from .types import RowDict, RowList, DatabaseUpdate

@contextmanager
def get_db_connection(db_path: str, timeout: float = DEFAULT_BUSY_TIMEOUT, retries: int = MAX_RETRY_ATTEMPTS) -> Iterator[sqlite3.Connection]:
    """Get a database connection with proper timeout and retry logic."""
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(db_path, timeout=timeout)
            # Enable WAL mode for better concurrency
            conn.execute("PRAGMA journal_mode=WAL")
            # Set busy timeout to 30 seconds
            conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            # Use NORMAL synchronous mode for better performance
            conn.execute("PRAGMA synchronous=NORMAL")
            # Increase cache size for better performance
            conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
            yield conn
            conn.close()
            return
        except sqlite3.OperationalError as e:
            error_str = str(e).lower()
            if "database is locked" in error_str or "unable to open database file" in error_str:
                if attempt < retries - 1:
                    wait_time = (attempt + 1) * 2  # Exponential backoff
                    logging.warning(f"Database locked/unavailable: {e}")
                    logging.warning(f"Retrying in {wait_time}s... (attempt {attempt + 1}/{retries})")
                    time.sleep(wait_time)
                else:
                    logging.error(f"Failed to connect after {retries} attempts: {e}")
                    logging.error(f"Database path: {db_path}")
                    logging.error("Possible causes:")
                    logging.error("  1. Too many concurrent connections")
                    logging.error("  2. Database file permissions issue")
                    logging.error("  3. Disk space or I/O issues")
                    logging.error("  4. Another process has exclusive lock")
                    raise
            else:
                raise
        except Exception as e:
            logging.error(f"Unexpected database connection error: {e}")
            if 'conn' in locals() and conn:
                conn.close()
            raise

def ensure_metadata_column(db_path: str, table: str) -> None:
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [col[1] for col in cursor.fetchall()]
            if 'metadata_updated' not in columns:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN metadata_updated TIMESTAMP")
                logging.info(f"Added 'metadata_updated' column to {table}")
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Database error while ensuring metadata column: {e}")
        raise

def execute_query(db_path: str, query: str, params: Optional[Union[Dict[str, Any], Tuple[Any, ...]]] = None) -> RowList:
    try:
        with get_db_connection(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Clean up any trailing LIMIT clause for the count query
            base_query = query.split('LIMIT')[0].strip()
            count_query = f"SELECT COUNT(*) FROM ({base_query})"

            if params:
                count = cursor.execute(count_query, params).fetchone()[0]
            else:
                count = cursor.execute(count_query).fetchone()[0]
            logging.debug(f"Query will return {count} rows")
            
            if params:
                results = cursor.execute(query, params).fetchall()
            else:
                results = cursor.execute(query).fetchall()
            dict_results = [dict(row) for row in results]
            
            # More concise debug logging
            logging.debug(f"Query executed: {query}")
            logging.debug(f"Rows returned: {len(dict_results)}")
            if dict_results:
                logging.debug(f"Sample row columns: {list(dict_results[0].keys())}")
                logging.debug(f"Sample rowid: {dict_results[0].get('rowid')}")
            return dict_results
    except sqlite3.Error as e:
        logging.error(f"Database error: {e}")
        
        # Provide helpful error messages for common issues
        error_msg = str(e).lower()
        if "no such column" in error_msg:
            column_name = error_msg.split("no such column: ")[-1]
            friendly_msg = f"""Database error: Column '{column_name}' doesn't exist.

💡 This usually means:
   1. You're referencing a column that hasn't been created yet by an enrichment
   2. Your SQL query has a typo in the column name
   3. You need to run a prerequisite enrichment first

💡 To fix this:
   - Check your SQL query for typos
   - Make sure prerequisite enrichments have been run
   - Use a query that doesn't depend on enrichment columns for initial runs

Query that failed: {query}"""
            raise click.UsageError(friendly_msg) from e
        elif "no such table" in error_msg:
            table_name = error_msg.split("no such table: ")[-1]
            friendly_msg = f"""Database error: Table '{table_name}' doesn't exist.

💡 This usually means:
   1. The database hasn't been created yet
   2. You need to run the 'ingest' command first
   3. The table name in your config is incorrect

💡 To fix this:
   - Run: doctrail ingest --input-dir /path/to/docs --db-path your_database.db
   - Check table names in your database
   - Verify the 'table' field in your enrichment config"""
            raise click.UsageError(friendly_msg) from e
        else:
            raise click.UsageError(f"Database error: {e}") from e

def execute_query_optimized(db_path: str, query: str, input_columns: List[str], params: Optional[Union[Dict[str, Any], Tuple[Any, ...]]] = None) -> RowList:
    """
    Optimized query execution that fetches only needed columns.
    
    Supports multi-table enrichments using sha1 as the universal key.
    Input columns can be specified as:
    - "column_name" (fetched from default table in query)
    - "table.column_name" (fetched from specific table using sha1)
    
    Args:
        db_path: Path to the database
        query: SQL query (must include sha1 for multi-table support)
        input_columns: List of column names, optionally prefixed with table names
        params: Optional query parameters
        
    Returns:
        List of dictionaries with sha1, rowid (if available) and requested columns
    """
    try:
        with get_db_connection(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # First, execute the query to get rowids
            if params:
                results = cursor.execute(query, params).fetchall()
            else:
                results = cursor.execute(query).fetchall()
            
            # Extract rowids and any other columns that were selected
            initial_results = [dict(row) for row in results]
            
            if not initial_results:
                return []
            
            # Check if we have sha1 in results (required for multi-table)
            has_sha1 = 'sha1' in initial_results[0] if initial_results else False
            has_rowid = 'rowid' in initial_results[0] if initial_results else False
            
            # Extract default table name from query (for backward compatibility)
            import re
            table_match = re.search(r'\bFROM\s+(\w+)', query, re.IGNORECASE)
            default_table = table_match.group(1) if table_match else 'documents'
            
            # Parse input columns to handle character limits and table prefixes
            from .core_utils import parse_input_columns_with_limits
            parsed_columns = parse_input_columns_with_limits(input_columns)
            
            # Organize columns by table
            columns_by_table = {}
            for col_spec, char_limit in parsed_columns:
                if '.' in col_spec:
                    table, column = col_spec.split('.', 1)
                else:
                    table = default_table
                    column = col_spec
                
                if table not in columns_by_table:
                    columns_by_table[table] = []
                columns_by_table[table].append((column, char_limit))
            
            # Build optimized results
            optimized_results = []
            
            for row in initial_results:
                result_row = dict(row)  # Start with query results
                
                # Multi-table fetch using sha1
                if has_sha1 and row.get('sha1'):
                    sha1 = row['sha1']
                    
                    # Fetch columns from each table
                    for table, table_columns in columns_by_table.items():
                        # Build column list for this table
                        col_names = [col for col, _ in table_columns]
                        
                        # Always include sha1 and rowid if fetching from the table
                        fetch_cols = ['rowid', 'sha1'] + [c for c in col_names if c not in ['rowid', 'sha1']]
                        columns_str = ', '.join(fetch_cols)
                        
                        try:
                            # Check if table exists
                            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
                            if not cursor.fetchone():
                                logging.debug(f"Table '{table}' not found, skipping columns: {col_names}")
                                # Set missing columns to None
                                for col, _ in table_columns:
                                    if col not in result_row:
                                        result_row[col] = None
                                continue
                            
                            # Fetch from this table using sha1
                            fetch_query = f"SELECT {columns_str} FROM {table} WHERE sha1 = ?"
                            cursor.execute(fetch_query, (sha1,))
                            table_row = cursor.fetchone()
                            
                            if table_row:
                                # Add columns from this table
                                table_data = dict(table_row)
                                for col, char_limit in table_columns:
                                    if col in table_data:
                                        # Apply character limit if specified
                                        value = table_data[col]
                                        if char_limit and isinstance(value, str) and len(value) > char_limit:
                                            value = value[:char_limit]
                                        result_row[col] = value
                                    else:
                                        result_row[col] = None
                                        
                                # Preserve rowid from the default table if this is the default table
                                if table == default_table and 'rowid' in table_data:
                                    result_row['rowid'] = table_data['rowid']
                            else:
                                # No matching row in this table
                                for col, _ in table_columns:
                                    if col not in result_row:
                                        result_row[col] = None
                                        
                        except sqlite3.Error as e:
                            logging.warning(f"Error fetching from table '{table}': {e}")
                            # Set missing columns to None
                            for col, _ in table_columns:
                                if col not in result_row:
                                    result_row[col] = None
                
                # Fallback: single-table fetch using rowid (backward compatibility)
                elif has_rowid and row.get('rowid'):
                    rowid = row['rowid']
                    # Original single-table logic
                    all_columns = []
                    for table, table_columns in columns_by_table.items():
                        if table == default_table:
                            all_columns.extend([col for col, _ in table_columns])
                    
                    if all_columns:
                        # Always include rowid and sha1
                        fetch_cols = ['rowid']
                        if 'sha1' not in fetch_cols:
                            fetch_cols.append('sha1')
                        fetch_cols.extend([c for c in all_columns if c not in fetch_cols])
                        
                        columns_str = ', '.join(fetch_cols)
                        fetch_query = f"SELECT {columns_str} FROM {default_table} WHERE rowid = ?"
                        
                        cursor.execute(fetch_query, (rowid,))
                        full_row = cursor.fetchone()
                        
                        if full_row:
                            result_row.update(dict(full_row))
                
                optimized_results.append(result_row)
            
            # Log summary
            total_cols = sum(len(cols) for cols in columns_by_table.values())
            tables_used = list(columns_by_table.keys())
            logging.debug(f"Multi-table query: fetched {len(optimized_results)} rows from tables {tables_used} with {total_cols} total columns")
            
            return optimized_results
            
    except sqlite3.Error as e:
        # Fall back to original execute_query if optimization fails
        logging.warning(f"Optimized query failed, falling back to standard execution: {e}")
        return execute_query(db_path, query, params)

def checkpoint_wal(db_path: str) -> None:
    """Run WAL checkpoint to prevent it from growing too large."""
    try:
        with get_db_connection(db_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            logging.debug("WAL checkpoint completed")
    except Exception as e:
        logging.warning(f"WAL checkpoint failed (non-critical): {e}")

def get_table_primary_key(db_path: str, table: str) -> str:
    """Get the primary key column for a table. Returns 'rowid' for tables without explicit PK."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table})")
        columns = cursor.fetchall()
        
        # Look for explicit primary key
        for col in columns:
            if col[5] == 1:  # col[5] is the pk flag
                return col[1]  # col[1] is the column name
        
        # No explicit primary key found, use rowid
        return 'rowid'

def update_database(db_path: str, table: str, output_col: str, results: List[DatabaseUpdate]) -> None:
    try:
        ensure_metadata_column(db_path, table)
        
        # Determine the appropriate key column for updates
        primary_key = get_table_primary_key(db_path, table)
        
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            
            # Check if output_col exists, if not, create it
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [col[1] for col in cursor.fetchall()]
            if output_col not in columns:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {output_col} TEXT")
                logging.info(f"Added '{output_col}' column to {table}")
            
            current_time = datetime.now().isoformat()
            updated_count = 0
            for i, row in enumerate(results, 1):
                try:
                    # Use the appropriate key column for WHERE clause
                    if primary_key == 'rowid':
                        key_value = row.get('rowid', 'NO_ROWID')
                    else:
                        key_value = row.get(primary_key, row.get('rowid', 'NO_KEY'))
                    
                    # Debug logging to see what's happening
                    logging.debug(f"Update row {i}: primary_key='{primary_key}', key_value='{key_value}', row keys={list(row.keys())}")
                    
                    query = f"UPDATE {table} SET {output_col} = ?, metadata_updated = ? WHERE {primary_key} = ?"
                    params = (row['updated'], current_time, key_value)
                    cursor.execute(query, params)
                    if cursor.rowcount > 0:
                        updated_count += 1
                        logging.debug(f"Executed query: {query} with params {params}")
                        logging.debug(f"Updated row {key_value}: {row['original']} -> {row['updated']}")
                    else:
                        logging.warning(f"No rows updated for {primary_key} {key_value}")
                except sqlite3.Error as e:
                    logging.error(f"Error updating row {i}: {e}")
            conn.commit()
        logging.debug(f"Database updated successfully. {updated_count} rows affected.")
    except sqlite3.Error as e:
        logging.error(f"Database update error: {e}")
        raise

def verify_updates(db_path: str, table: str, output_col: str, results: List[DatabaseUpdate]) -> None:
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            
            # Check if output_col exists, if not, log a warning and return
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [col[1] for col in cursor.fetchall()]
            if output_col not in columns:
                logging.warning(f"Column '{output_col}' does not exist in table '{table}'. Skipping verification.")
                return
            
            for row in results:
                if row['updated'] is not None and row['updated'] != row['original']:
                    query = f"SELECT {output_col}, metadata_updated FROM {table} WHERE rowid = ?"
                    cursor.execute(query, (row['rowid'],))
                    result = cursor.fetchone()
                    if result:
                        db_value, db_timestamp = result
                        logging.info(f"Verification for row {row['rowid']}: DB value: {db_value}, DB timestamp: {db_timestamp}")
                        if db_value != row['updated']:
                            logging.error(f"Mismatch for row {row['rowid']}: Expected {row['updated']}, got {db_value}")
                    else:
                        logging.error(f"Row {row['rowid']} not found in database")
    except sqlite3.Error as e:
        logging.error(f"Database verification error: {e}")
        raise

def ensure_output_column(db_path: str, table: str, column: str) -> None:
    """Ensure the output column exists in the table."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # Check if column exists
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [info[1] for info in cursor.fetchall()]
        
        if column not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            conn.commit()
            logging.info(f"Added '{column}' column to {table}")

def ensure_output_table(db_path: str, table_name: str, key_column: str = "sha1", output_columns: Optional[List[str]] = None, 
                       is_derived_table: bool = False) -> None:
    """Ensure the output table exists, creating it if necessary with proper schema.
    
    Args:
        db_path: Path to database
        table_name: Name of table to create/update
        key_column: Primary key column (usually sha1)
        output_columns: List of output columns to create
        is_derived_table: If True, adds model_used column and uses composite unique constraint
    """
    if output_columns is None:
        output_columns = []
    
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        table_exists = cursor.fetchone() is not None
        
        if not table_exists:
            # Create new table with appropriate schema
            if is_derived_table:
                # Derived tables use auto-increment ID and composite unique constraint
                columns_def = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
                columns_def.append(f"{key_column} TEXT NOT NULL")
                columns_def.append("model_used TEXT NOT NULL")
                columns_def.append("enrichment_id TEXT")
            else:
                # Main tables use sha1 as primary key
                columns_def = [f"{key_column} TEXT PRIMARY KEY"]
                columns_def.append("enrichment_id TEXT")
            
            # Add output columns
            for col in output_columns:
                columns_def.append(f"{col} TEXT")
            
            # Add metadata columns
            columns_def.extend([
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP",
                "updated_at TEXT DEFAULT CURRENT_TIMESTAMP"
            ])
            
            # Add unique constraint for derived tables
            if is_derived_table:
                columns_def.append(f"UNIQUE({key_column}, model_used)")
            
            create_sql = f"CREATE TABLE {table_name} (\n    " + ",\n    ".join(columns_def) + "\n)"
            cursor.execute(create_sql)
            
            # Create indices for performance
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_{key_column} ON {table_name}({key_column})")
            if is_derived_table:
                cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_model ON {table_name}(model_used)")
                cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_sha1_model ON {table_name}({key_column}, model_used)")
            
            conn.commit()
            logging.info(f"Created {'derived' if is_derived_table else 'output'} table '{table_name}' with key column '{key_column}' and columns: {output_columns}")
        else:
            # Table exists - ensure it has necessary columns
            cursor.execute(f"PRAGMA table_info({table_name})")
            existing_columns = [info[1] for info in cursor.fetchall()]
            
            # Add enrichment_id column if it doesn't exist
            if "enrichment_id" not in existing_columns:
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN enrichment_id TEXT")
                logging.info(f"Added enrichment_id column to existing table '{table_name}'")
            
            # Add model_used column for derived tables if it doesn't exist
            if is_derived_table and "model_used" not in existing_columns:
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN model_used TEXT")
                logging.info(f"Added model_used column to existing table '{table_name}'")
                
                # Create index on model_used for performance
                cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_model ON {table_name}(model_used)")
                cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_sha1_model ON {table_name}({key_column}, model_used)")
            
            for col in output_columns:
                if col not in existing_columns:
                    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} TEXT")
                    logging.info(f"Added '{col}' column to existing table {table_name}")
            
            conn.commit()

def ensure_enrichment_responses_table(db_path: str) -> None:
    """Ensure the enrichment_responses audit table exists."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # Create enrichment_responses table for audit trail
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS enrichment_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                enrichment_id TEXT UNIQUE,
                sha1 TEXT NOT NULL,
                enrichment_name TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                model_used TEXT NOT NULL,
                prompt_id TEXT,
                full_prompt TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Check if enrichment_id column exists, if not add it
        cursor.execute("PRAGMA table_info(enrichment_responses)")
        columns = [info[1] for info in cursor.fetchall()]
        if "enrichment_id" not in columns:
            cursor.execute("ALTER TABLE enrichment_responses ADD COLUMN enrichment_id TEXT")
            logging.info("Added enrichment_id column to existing enrichment_responses table")
        
        # Check if prompt_id column exists, if not add it
        if "prompt_id" not in columns:
            cursor.execute("ALTER TABLE enrichment_responses ADD COLUMN prompt_id TEXT")
            logging.info("Added prompt_id column to existing enrichment_responses table")
        
        # Check if full_prompt column exists, if not add it
        if "full_prompt" not in columns:
            cursor.execute("ALTER TABLE enrichment_responses ADD COLUMN full_prompt TEXT")
            logging.info("Added full_prompt column to existing enrichment_responses table")
        
        # Migrate existing table if needed - check for old constraints
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='enrichment_responses'")
        table_sql = cursor.fetchone()
        if table_sql and ('UNIQUE(sha1, enrichment_name)' in table_sql[0] or 
                         'UNIQUE(sha1, enrichment_name, model_used)' in table_sql[0]):
            logging.info("Migrating enrichment_responses table to remove unique constraints")
            # We need to recreate the table without the constraint
            cursor.execute("""
                CREATE TABLE enrichment_responses_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    enrichment_id TEXT UNIQUE,
                    sha1 TEXT NOT NULL,
                    enrichment_name TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    model_used TEXT NOT NULL,
                    prompt_id TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Copy data from old table
            cursor.execute("""
                INSERT INTO enrichment_responses_new (id, enrichment_id, sha1, enrichment_name, raw_json, model_used, created_at)
                SELECT id, enrichment_id, sha1, enrichment_name, raw_json, model_used, created_at FROM enrichment_responses
            """)
            # Drop old table and rename new one
            cursor.execute("DROP TABLE enrichment_responses")
            cursor.execute("ALTER TABLE enrichment_responses_new RENAME TO enrichment_responses")
            logging.info("Migration completed")
        
        # Create indices for performance
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_enrichment_responses_sha1 
            ON enrichment_responses(sha1)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_enrichment_responses_enrichment 
            ON enrichment_responses(enrichment_name)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_enrichment_responses_created 
            ON enrichment_responses(created_at)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_enrichment_responses_enrichment_id 
            ON enrichment_responses(enrichment_id)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_enrichment_responses_composite 
            ON enrichment_responses(sha1, enrichment_name, model_used)
        """)
        
        conn.commit()
        logging.debug("Ensured enrichment_responses table exists")

def store_raw_enrichment_response(db_path: str, sha1: str, enrichment_name: str, 
                                 raw_json: str, model_used: str, enrichment_id: Optional[str] = None, 
                                 prompt_id: Optional[str] = None, full_prompt: Optional[str] = None) -> None:
    """Store raw LLM response in audit table."""
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            
            current_time = datetime.now().isoformat()
            
            cursor.execute("""
                INSERT INTO enrichment_responses 
                (enrichment_id, sha1, enrichment_name, raw_json, model_used, prompt_id, full_prompt, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (enrichment_id, sha1, enrichment_name, raw_json, model_used, prompt_id, full_prompt, current_time))
            
            conn.commit()
            logging.debug(f"Stored raw response for {enrichment_name} on {sha1[:8]}")
            
    except sqlite3.Error as e:
        logging.error(f"Error storing raw enrichment response: {e}")
        raise

def get_enrichment_response_history(db_path: str, sha1: Optional[str] = None, 
                                   enrichment_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieve enrichment response history for debugging/audit."""
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            
            # Build query based on filters
            query = "SELECT * FROM enrichment_responses WHERE 1=1"
            params = []
            
            if sha1:
                query += " AND sha1 = ?"
                params.append(sha1)
                
            if enrichment_name:
                query += " AND enrichment_name = ?"
                params.append(enrichment_name)
                
            query += " ORDER BY created_at DESC"
            
            cursor.execute(query, params)
            
            columns = [desc[0] for desc in cursor.description]
            results = []
            for row in cursor.fetchall():
                result = dict(zip(columns, row))
                # Parse raw_json back to dict for easier inspection
                try:
                    result['parsed_json'] = json.loads(result['raw_json'])
                except json.JSONDecodeError:
                    result['parsed_json'] = None
                results.append(result)
                
            return results
            
    except sqlite3.Error as e:
        logging.error(f"Error retrieving enrichment history: {e}")
        return []

def update_output_table(db_path: str, output_table: str, key_column: str, key_value: str, output_data: Dict[str, Any], 
                       enrichment_id: Optional[str] = None, model_used: Optional[str] = None) -> None:
    """Update or insert data into a separate output table keyed by key_column.
    
    Args:
        db_path: Path to database
        output_table: Name of output table
        key_column: Key column name (usually sha1)
        key_value: Value for key column
        output_data: Dictionary of column:value pairs to store
        enrichment_id: UUID for this enrichment run
        model_used: Model name (for derived tables with multiple models)
    """
    try:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            
            # Check if table has model_used column (i.e., is a derived table)
            cursor.execute(f"PRAGMA table_info({output_table})")
            columns_info = cursor.fetchall()
            column_names = [info[1] for info in columns_info]
            has_model_column = "model_used" in column_names
            
            # Ensure updated_at column exists (add it if missing)
            if "updated_at" not in column_names:
                cursor.execute(f"ALTER TABLE {output_table} ADD COLUMN updated_at TEXT")
                logging.info(f"Added 'updated_at' column to {output_table}")
                column_names.append("updated_at")
            
            # Also ensure created_at column exists (add it if missing)
            if "created_at" not in column_names:
                cursor.execute(f"ALTER TABLE {output_table} ADD COLUMN created_at TEXT")
                logging.info(f"Added 'created_at' column to {output_table}")
                column_names.append("created_at")
            
            has_updated_at_column = True  # Now we know it exists
            
            # Check if record exists
            if has_model_column and model_used:
                # For derived tables, check using both key and model
                cursor.execute(f"SELECT 1 FROM {output_table} WHERE {key_column} = ? AND model_used = ?", 
                             (key_value, model_used))
            else:
                # For regular tables, check using just the key
                cursor.execute(f"SELECT 1 FROM {output_table} WHERE {key_column} = ?", (key_value,))
            exists = cursor.fetchone() is not None
            
            # First check if there's any meaningful data
            has_meaningful_data = False
            cleaned_data = {}
            
            for col, val in output_data.items():
                # Skip null-like values
                if val is None or val == "null" or val == "" or (isinstance(val, str) and val.lower() == "null"):
                    continue
                    
                # For lists/dicts, check if they're empty
                if isinstance(val, (list, dict)) and not val:
                    continue
                    
                # If we get here, we have some data
                has_meaningful_data = True
                cleaned_data[col] = val
            
            # Don't insert rows with no meaningful data
            if not has_meaningful_data:
                logging.debug(f"Skipping insert for {key_column}={key_value} (model={model_used}) - no meaningful data")
                return
            
            
            current_time = datetime.now().isoformat()
            
            # Serialize complex types to JSON strings
            serialized_data = {}
            for col, val in cleaned_data.items():
                if isinstance(val, (list, dict)):
                    serialized_data[col] = json.dumps(val, ensure_ascii=False)
                elif hasattr(val, 'value'):  # Handle enum values
                    serialized_data[col] = val.value
                else:
                    serialized_data[col] = val
            
            if exists:
                # Update existing record
                set_clauses = []
                values = []
                for col, val in serialized_data.items():
                    set_clauses.append(f"{col} = ?")
                    values.append(val)
                
                # Add enrichment_id if provided
                if enrichment_id:
                    set_clauses.append("enrichment_id = ?")
                    values.append(enrichment_id)
                
                # Add updated_at timestamp if column exists
                if has_updated_at_column:
                    set_clauses.append("updated_at = ?")
                    values.append(current_time)
                
                # Build WHERE clause based on table type
                if has_model_column and model_used:
                    where_clause = f"WHERE {key_column} = ? AND model_used = ?"
                    values.extend([key_value, model_used])
                else:
                    where_clause = f"WHERE {key_column} = ?"
                    values.append(key_value)
                
                update_query = f"UPDATE {output_table} SET {', '.join(set_clauses)} {where_clause}"
                cursor.execute(update_query, values)
                logging.debug(f"Updated {output_table} for {key_column}={key_value}" + 
                            (f" with model={model_used}" if model_used else ""))
            else:
                # Insert new record
                columns = [key_column]
                values = [key_value]
                
                # Add model_used for derived tables
                if has_model_column and model_used:
                    columns.append("model_used")
                    values.append(model_used)
                
                # Add enrichment_id if provided
                if enrichment_id:
                    columns.append("enrichment_id")
                    values.append(enrichment_id)
                
                # Add output data columns
                columns.extend(list(serialized_data.keys()))
                values.extend(list(serialized_data.values()))
                
                # Add timestamps
                columns.extend(["created_at", "updated_at"])
                values.extend([current_time, current_time])
                
                placeholders = ", ".join(["?"] * len(columns))
                
                insert_query = f"INSERT INTO {output_table} ({', '.join(columns)}) VALUES ({placeholders})"
                cursor.execute(insert_query, values)
                logging.debug(f"Inserted into {output_table} for {key_column}={key_value}" + 
                            (f" with model={model_used}" if model_used else ""))
            
            conn.commit()
            
    except sqlite3.Error as e:
        logging.error(f"Error updating output table {output_table}: {e}")
        raise

def ensure_prompts_table(db_path: str) -> None:
    """Ensure the prompts table exists for tracking prompt versions."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS prompts (
                prompt_id TEXT PRIMARY KEY,
                enrichment_name TEXT NOT NULL,
                prompt_text TEXT NOT NULL,
                system_prompt TEXT,
                prompt_hash TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(enrichment_name, prompt_hash)
            )
        """)
        
        # Create indices for performance
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_prompts_enrichment 
            ON prompts(enrichment_name)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_prompts_hash 
            ON prompts(prompt_hash)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_prompts_created 
            ON prompts(created_at)
        """)
        
        conn.commit()
        logging.debug("Ensured prompts table exists")

def get_or_create_prompt_id(db_path: str, enrichment_name: str, prompt_text: str, 
                           system_prompt: Optional[str] = None, model_used: Optional[str] = None) -> str:
    """Get existing prompt_id or create new prompt record.
    
    Returns prompt_id for use in enrichment_responses.
    Note: model_used parameter is ignored - prompts are model-agnostic.
    """
    import hashlib
    import uuid
    
    # Create a hash of the prompt content for deduplication (excluding model)
    prompt_content = f"{enrichment_name}|{prompt_text}|{system_prompt or ''}"
    prompt_hash = hashlib.sha256(prompt_content.encode()).hexdigest()
    
    ensure_prompts_table(db_path)
    
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        
        # Check if this exact prompt already exists
        cursor.execute("""
            SELECT prompt_id FROM prompts 
            WHERE enrichment_name = ? AND prompt_hash = ?
        """, (enrichment_name, prompt_hash))
        
        result = cursor.fetchone()
        if result:
            return result[0]
        
        # Create new prompt record
        prompt_id = str(uuid.uuid4())
        current_time = datetime.now().isoformat()
        
        cursor.execute("""
            INSERT INTO prompts (prompt_id, enrichment_name, prompt_text, system_prompt, 
                               prompt_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (prompt_id, enrichment_name, prompt_text, system_prompt, 
              prompt_hash, current_time))
        
        conn.commit()
        logging.debug(f"Created new prompt record: {prompt_id[:8]} for {enrichment_name}")
        
        return prompt_id

def get_prompt_by_id(db_path: str, prompt_id: str) -> Dict[str, Any]:
    """Retrieve prompt details by prompt_id."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row
        
        cursor.execute("SELECT * FROM prompts WHERE prompt_id = ?", (prompt_id,))
        result = cursor.fetchone()
        
        if result:
            return dict(result)
        return None

def get_enrichment_prompts_history(db_path: str, enrichment_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get history of all prompts used for an enrichment."""
    # Ensure tables exist
    ensure_prompts_table(db_path)
    ensure_enrichment_responses_table(db_path)
    
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row
        
        if enrichment_name:
            cursor.execute("""
                SELECT p.*, COUNT(er.id) as usage_count
                FROM prompts p
                LEFT JOIN enrichment_responses er ON p.prompt_id = er.prompt_id
                WHERE p.enrichment_name = ?
                GROUP BY p.prompt_id
                ORDER BY p.created_at DESC
            """, (enrichment_name,))
        else:
            cursor.execute("""
                SELECT p.*, COUNT(er.id) as usage_count
                FROM prompts p
                LEFT JOIN enrichment_responses er ON p.prompt_id = er.prompt_id
                GROUP BY p.prompt_id
                ORDER BY p.created_at DESC
            """)
        
        return [dict(row) for row in cursor.fetchall()]