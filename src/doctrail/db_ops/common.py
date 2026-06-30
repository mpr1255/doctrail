import os
import sqlite3
import logging
import click
import time
import threading
import json
import hashlib
import csv
import html
from datetime import datetime
from contextlib import contextmanager
from typing import List, Dict, Optional, Any, Tuple, Iterator, Union, Set

from ..constants import DEFAULT_BUSY_TIMEOUT, MAX_RETRY_ATTEMPTS, DEFAULT_KEY_COLUMN
from ..types import RowDict, RowList, DatabaseUpdate

ENRICHMENT_PROJECTION_VERSION = "v1"

ENRICHMENTS_TABLE = "_enrichments"
ENRICHMENT_AUDIT_TABLE = "_enrichment_audit"
ENRICHMENT_RUNS_TABLE = "_enrichment_runs"
ENRICHMENT_RUN_ITEMS_TABLE = "_enrichment_run_items"
ENRICHMENT_OVERRIDES_TABLE = "_enrichment_overrides"
ENRICHMENT_BATCH_JOBS_TABLE = "_enrichment_batch_jobs"
ENRICHMENTS_SUPERSEDED_TABLE = "_enrichments_superseded"
PROMPTS_TABLE = "_prompts"
ICR_SAMPLES_TABLE = "_icr_samples"
DOCTRAIL_META_TABLE = "_doctrail_meta"
DOCTRAIL_FINAL_TABLES_TABLE = "_doctrail_final_tables"
DOCTRAIL_VIEW_PREFIX = "v_"

BOOKKEEPING_TABLE_RENAMES = {
    "enrichments": ENRICHMENTS_TABLE,
    "enrichment_audit": ENRICHMENT_AUDIT_TABLE,
    "enrichment_runs": ENRICHMENT_RUNS_TABLE,
    "enrichment_run_items": ENRICHMENT_RUN_ITEMS_TABLE,
    "enrichment_overrides": ENRICHMENT_OVERRIDES_TABLE,
    "enrichment_batch_jobs": ENRICHMENT_BATCH_JOBS_TABLE,
    "enrichments_superseded": ENRICHMENTS_SUPERSEDED_TABLE,
    "prompts": PROMPTS_TABLE,
    "icr_samples": ICR_SAMPLES_TABLE,
}


def _doctrail_view_name(name: str) -> str:
    """Return the canonical persisted name for a Doctrail-managed view."""
    return name if name.startswith(DOCTRAIL_VIEW_PREFIX) else f"{DOCTRAIL_VIEW_PREFIX}{name}"


def _key_prefix(key_value: Any) -> str:
    """Return a stable short key preview for logging."""
    text = "" if key_value is None else str(key_value)
    return text[:8] if len(text) >= 8 else text

def _json_path_for_key(key: str) -> str:
    """Return a JSON path that safely addresses an object key."""
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'$."{escaped}"'

def _hash_text(*parts: Optional[str]) -> str:
    """Hash a sequence of strings into a stable SHA-256 hex digest."""
    payload = "||".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _sql_quote(value: str) -> str:
    """Return a safely quoted SQL string literal."""
    return str(value).replace("'", "''")

def _sql_literal(value: Any) -> str:
    """Return a safely quoted SQL string literal including delimiters."""
    return f"'{_sql_quote(value)}'"

def _quote_identifier(name: Any, kind: str = "identifier") -> str:
    """Return a safely quoted SQLite identifier."""
    if name is None:
        raise ValueError(f"{kind} cannot be empty")
    text = str(name)
    if not text:
        raise ValueError(f"{kind} cannot be empty")
    if "\x00" in text:
        raise ValueError(f"{kind} cannot contain null bytes: {text!r}")
    escaped = text.replace('"', '""')
    return f'"{escaped}"'

def _quote_identifier_list(names: List[Any], kind: str = "identifier") -> str:
    """Return a comma-separated list of quoted SQLite identifiers."""
    return ", ".join(_quote_identifier(name, kind) for name in names)

def _validate_sql_identifier(name: str, kind: str = "identifier") -> str:
    """Validate a SQLite identifier that will be interpolated into SQL."""
    if not name:
        raise ValueError(f"{kind} cannot be empty")
    if not (name[0].isalpha() or name[0] == "_"):
        raise ValueError(f"{kind} must start with a letter or underscore: {name}")
    if any(not (char.isalnum() or char == "_") for char in name):
        raise ValueError(f"{kind} may only contain letters, numbers, and underscores: {name}")
    return name

def _prepare_enrichment_storage_value(value: Any, value_type: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Normalize a value to the exact serialized representation used by enrichments."""
    if isinstance(value, (list, dict)):
        serialized_value = json.dumps(value, ensure_ascii=False)
        inferred_type = 'json'
    elif hasattr(value, 'value'):
        serialized_value = value.value
        inferred_type = 'enum'
    elif isinstance(value, bool):
        # JSON-canonical so views and SQL filters see 'true'/'false'
        serialized_value = 'true' if value else 'false'
        inferred_type = 'boolean'
    elif isinstance(value, int):
        serialized_value = str(value)
        inferred_type = 'integer'
    elif isinstance(value, float):
        serialized_value = str(value)
        inferred_type = 'number'
    elif value is None:
        serialized_value = None
        inferred_type = 'null'
    else:
        serialized_value = str(value)
        inferred_type = 'string'

    return serialized_value, (value_type or inferred_type)

def build_enrichment_projection(
    updated: Any,
    output_fields: Optional[List[str]] = None,
    metadata_by_field: Optional[Dict[str, Any]] = None,
    value_type_overrides: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Build the exact normalized enrichments projection for an LLM result.

    The returned rows are already serialized the same way they will be stored in
    `enrichments`, which makes rebuilds from audit exact instead of heuristic.
    """
    projection_rows: List[Dict[str, Any]] = []
    metadata_by_field = metadata_by_field or {}
    value_type_overrides = value_type_overrides or {}

    if updated is None:
        field_items = [(field_name, None) for field_name in (output_fields or [])]
    elif isinstance(updated, dict):
        field_items = updated.items()
    else:
        field_name = output_fields[0] if output_fields else "result"
        field_items = [(field_name, updated)]

    for field_name, field_value in field_items:
        serialized_value, inferred_type = _prepare_enrichment_storage_value(
            field_value,
            value_type=value_type_overrides.get(field_name),
        )
        projection_rows.append({
            "field_name": str(field_name),
            "value": serialized_value,
            "value_type": inferred_type,
            "metadata": metadata_by_field.get(field_name),
        })

    return projection_rows

def serialize_enrichment_projection(projection_rows: List[Dict[str, Any]]) -> str:
    """Serialize normalized projection rows for persistence in enrichment_audit."""
    return json.dumps(projection_rows, ensure_ascii=False)

def parse_enrichment_projection(projection_json: str) -> List[Dict[str, Any]]:
    """Parse and validate projection rows stored in enrichment_audit."""
    try:
        data = json.loads(projection_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid projection_json: {exc}") from exc

    if not isinstance(data, list):
        raise ValueError("projection_json must decode to a list")

    parsed_rows: List[Dict[str, Any]] = []
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValueError(f"projection_json row {index} is not an object")
        field_name = row.get("field_name")
        if not field_name:
            raise ValueError(f"projection_json row {index} is missing field_name")
        parsed_rows.append({
            "field_name": str(field_name),
            "value": row.get("value"),
            "value_type": row.get("value_type") or "string",
            "metadata": row.get("metadata"),
        })

    return parsed_rows

def _serialize_raw_enrichment_payload(
    *,
    updated: Any,
    raw_json: Optional[str] = None,
    error: Optional[str] = None,
) -> str:
    """Build the exact raw audit payload when a caller has not provided one."""
    if raw_json:
        return raw_json
    if updated is None:
        return json.dumps({"error": error or "Unknown error"}, ensure_ascii=False)
    if isinstance(updated, dict):
        return json.dumps(updated, ensure_ascii=False)
    return json.dumps({"result": updated}, ensure_ascii=False)

@contextmanager
def get_db_connection(db_path: str, timeout: float = DEFAULT_BUSY_TIMEOUT, retries: int = MAX_RETRY_ATTEMPTS) -> Iterator[sqlite3.Connection]:
    """Get a database connection with proper timeout and retry logic."""
    conn = None
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
            break
        except sqlite3.OperationalError as e:
            if conn is not None:
                conn.close()
                conn = None
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
            if conn is not None:
                conn.close()
            raise
    else:
        raise sqlite3.OperationalError(f"Failed to connect after {retries} attempts: {db_path}")

    try:
        yield conn
    finally:
        conn.close()

def execute_query(db_path: str, query: str, params: Optional[Union[Dict[str, Any], Tuple[Any, ...]]] = None) -> RowList:
    try:
        with get_db_connection(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            countable_query = query.strip().rstrip(";")
            count_query = f"SELECT COUNT(*) FROM ({countable_query}) AS _doctrail_count"
            try:
                if params:
                    count = cursor.execute(count_query, params).fetchone()[0]
                else:
                    count = cursor.execute(count_query).fetchone()[0]
                logging.debug(f"Query will return {count} rows")
            except sqlite3.Error as count_error:
                logging.debug(f"Skipping query row-count preflight: {count_error}")
            
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

This usually means:
   1. You're referencing a column that hasn't been created yet by an enrichment
   2. Your SQL query has a typo in the column name
   3. You need to run a prerequisite enrichment first

To fix this:
   - Check your SQL query for typos
   - Make sure prerequisite enrichments have been run
   - Use a query that doesn't depend on enrichment columns for initial runs

Query that failed: {query}"""
            raise click.UsageError(friendly_msg) from e
        elif "no such table" in error_msg:
            table_name = error_msg.split("no such table: ")[-1]
            friendly_msg = f"""Database error: Table '{table_name}' doesn't exist.

This usually means:
   1. The database hasn't been created yet
   2. You need to run the 'ingest' command first
   3. The table name in your config is incorrect

To fix this:
   - Run: doctrail ingest --input-dir /path/to/docs --db-path your_database.db
   - Check table names in your database
   - Verify the 'table' field in your enrichment config"""
            raise click.UsageError(friendly_msg) from e
        else:
            raise click.UsageError(f"Database error: {e}") from e

def execute_query_optimized(
    db_path: str,
    query: str,
    input_columns: List[str],
    params: Optional[Union[Dict[str, Any], Tuple[Any, ...]]] = None,
    key_column: str = "sha1",
    default_table: Optional[str] = None,
) -> RowList:
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
            
            # Check if we have key column in results (required for multi-table)
            has_sha1 = key_column in initial_results[0] if initial_results else False
            has_rowid = 'rowid' in initial_results[0] if initial_results else False
            
            # Parse input columns to handle character limits and table prefixes
            from ..core_utils import parse_input_columns_with_limits
            parsed_columns = parse_input_columns_with_limits(input_columns)

            if default_table is None and any("." not in col_spec for col_spec, _ in parsed_columns):
                raise ValueError(
                    "execute_query_optimized requires default_table when input_columns contain unqualified columns"
                )
            
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
                
                # Multi-table fetch using key column
                if has_sha1 and row.get(key_column) is not None:
                    sha1 = row[key_column]
                    
                    # Fetch columns from each table
                    for table, table_columns in columns_by_table.items():
                        # Build column list for this table
                        col_names = [col for col, _ in table_columns]
                        
                        # Always include key column and rowid if fetching from the table
                        fetch_cols = ['rowid', key_column] + [c for c in col_names if c not in ['rowid', key_column]]
                        columns_str = _quote_identifier_list(fetch_cols, "column name")
                        table_ref = _quote_identifier(table, "table name")
                        key_column_ref = _quote_identifier(key_column, "key column")
                        
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
                            
                            # Fetch from this table using key column
                            fetch_query = f"SELECT {columns_str} FROM {table_ref} WHERE {key_column_ref} = ?"
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
                        if key_column not in fetch_cols:
                            fetch_cols.append(key_column)
                        fetch_cols.extend([c for c in all_columns if c not in fetch_cols])
                        
                        columns_str = _quote_identifier_list(fetch_cols, "column name")
                        default_table_ref = _quote_identifier(default_table, "table name")
                        fetch_query = f"SELECT {columns_str} FROM {default_table_ref} WHERE rowid = ?"
                        
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
        logging.error(f"Optimized query failed: {e}")
        raise

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
    table_ref = _quote_identifier(table, "table name")
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_ref})")
        columns = cursor.fetchall()
        
        # Look for explicit primary key
        for col in columns:
            if col[5] == 1:  # col[5] is the pk flag
                return col[1]  # col[1] is the column name
        
        # No explicit primary key found, use rowid
        return 'rowid'

__all__ = [

    '_key_prefix',

    '_json_path_for_key',

    '_hash_text',

    '_sql_quote',

    '_sql_literal',

    '_quote_identifier',

    '_quote_identifier_list',

    '_validate_sql_identifier',

    '_prepare_enrichment_storage_value',

    'build_enrichment_projection',

    'serialize_enrichment_projection',

    'parse_enrichment_projection',

    '_serialize_raw_enrichment_payload',

    'get_db_connection',

    'execute_query',

    'execute_query_optimized',

    'checkpoint_wal',

    'get_table_primary_key',

    'ENRICHMENT_PROJECTION_VERSION',

    'ENRICHMENTS_TABLE',

    'ENRICHMENT_AUDIT_TABLE',

    'ENRICHMENT_RUNS_TABLE',

    'ENRICHMENT_RUN_ITEMS_TABLE',

    'ENRICHMENT_OVERRIDES_TABLE',

    'ENRICHMENT_BATCH_JOBS_TABLE',

    'ENRICHMENTS_SUPERSEDED_TABLE',

    'PROMPTS_TABLE',

    'ICR_SAMPLES_TABLE',

    'DOCTRAIL_META_TABLE',

    'DOCTRAIL_FINAL_TABLES_TABLE',

    'DOCTRAIL_VIEW_PREFIX',

    'BOOKKEEPING_TABLE_RENAMES',

    '_doctrail_view_name',

]
