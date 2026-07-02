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

from .common import *
from .migrations import run_pending_migrations


def _get_enrichment_value_type(cursor: sqlite3.Cursor, enrichment_name: str, field_name: str) -> Optional[str]:
    cursor.execute(f"""
        SELECT DISTINCT value_type
        FROM {ENRICHMENTS_TABLE}
        WHERE enrichment_name = ? AND field_name = ?
          AND value_type IS NOT NULL
          AND value_type != 'null'
    """, (enrichment_name, field_name))
    value_types = {row[0] for row in cursor.fetchall() if row[0]}
    if value_types == {"integer"}:
        return "integer"
    if value_types == {"number"}:
        return "number"
    return None


def _cast_numeric_value(expression: str, value_type: Optional[str]) -> str:
    if value_type == "integer":
        return f"CAST({expression} AS INTEGER)"
    if value_type == "number":
        return f"CAST({expression} AS REAL)"
    return expression


def create_run_view(
    db_path: str,
    run_id: Optional[str] = None,
    enrichment_name: Optional[str] = None,
    prompt_id: Optional[str] = None,
    run_timestamp: Optional[str] = None,
    documents_table: str = 'documents',
    key_column: str = 'sha1',
    priority_columns: Optional[List[str]] = None,
    source_db_path: Optional[str] = None
) -> Optional[str]:
    """Create a view for a specific run, falling back to legacy prompt-scoped runs."""
    ensure_enrichments_table(db_path)
    ensure_run_tracking_tables(db_path)

    if priority_columns is None:
        priority_columns = [
            'title', 'headline_main', 'bibtex_key',
            'authors', 'year', 'pub_date', 'date',
            'doi', 'publication_title', 'source',
        ]

    # Resolve legacy prompt-only calls to the latest persisted run if possible.
    if not run_id and enrichment_name and prompt_id:
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT run_id, command_started_at
                FROM {ENRICHMENT_RUNS_TABLE}
                WHERE enrichment_name = ? AND prompt_id = ?
                ORDER BY command_started_at DESC
                LIMIT 1
            """, (enrichment_name, prompt_id))
            row = cursor.fetchone()
            if row:
                run_id = row[0]
                run_timestamp = row[1]

    if run_id:
        run = get_enrichment_run(db_path, run_id)
        if not run:
            raise ValueError(f"Run '{run_id}' not found")

        enrichment_name = run["enrichment_name"]
        key_column = run.get("key_column") or key_column
        run_timestamp = run.get("command_started_at") or run.get("started_at") or run_timestamp

        try:
            ts_label = datetime.fromisoformat(run_timestamp).strftime('%Y%m%d_%H%M')
        except (ValueError, TypeError):
            ts_label = str(run_timestamp).replace('-', '').replace(':', '').replace('T', '_')[:13]

        safe_name = ''.join(c if c.isalnum() or c == '_' else '_' for c in enrichment_name)
        view_name = _doctrail_view_name(f"run_{safe_name}_{ts_label}_{run_id[:8]}")
        view_ref = _quote_identifier(view_name, "view name")
        quoted_run_id = _sql_quote(run_id)
        quoted_enrichment = _sql_quote(enrichment_name)
        quoted_prompt_id = _sql_quote(run.get("prompt_id") or "")
        quoted_query_hash = _sql_quote(run.get("query_hash") or "")

        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()

            cursor.execute(f"""
                SELECT DISTINCT field_name
                FROM {ENRICHMENTS_TABLE}
                WHERE run_id = ?
                ORDER BY field_name
            """, (run_id,))
            field_names = [row[0] for row in cursor.fetchall()]
            if not field_names:
                logging.info(f"No enrichment fields found for run '{run_id[:8]}'; run view not created")
                return None

            cursor.execute(f"""
                SELECT row_json
                FROM {ENRICHMENT_RUN_ITEMS_TABLE}
                WHERE run_id = ? AND row_json IS NOT NULL
                ORDER BY row_order
                LIMIT 1
            """, (run_id,))
            first_row = cursor.fetchone()
            ordered_snapshot_columns: List[str] = []
            if first_row and first_row[0]:
                try:
                    ordered_snapshot_columns = list(json.loads(first_row[0]).keys())
                except json.JSONDecodeError:
                    ordered_snapshot_columns = []

            cursor.execute(f"""
                SELECT DISTINCT j.key
                FROM {ENRICHMENT_RUN_ITEMS_TABLE} i, json_each(i.row_json) j
                WHERE i.run_id = ? AND i.row_json IS NOT NULL
                ORDER BY j.key
            """, (run_id,))
            discovered_columns = [row[0] for row in cursor.fetchall()]
            for col in discovered_columns:
                if col not in ordered_snapshot_columns:
                    ordered_snapshot_columns.append(col)
            if key_column not in ordered_snapshot_columns:
                ordered_snapshot_columns.insert(0, key_column)

            preferred = [key_column, 'rowid'] + priority_columns
            ordered_columns = [col for col in preferred if col in ordered_snapshot_columns]
            ordered_columns.extend(
                col for col in ordered_snapshot_columns
                if col not in ordered_columns
            )

            import json as _json
            scalar_fields: List[str] = []
            array_field: Optional[str] = None
            array_obj_keys: Optional[List[str]] = None

            for field in field_names:
                cursor.execute(f"""
                    SELECT value FROM {ENRICHMENTS_TABLE}
                    WHERE run_id = ? AND field_name = ?
                      AND value IS NOT NULL AND value != ''
                    LIMIT 1
                """, (run_id, field))
                sample = cursor.fetchone()
                if sample and sample[0] and sample[0].strip().startswith('['):
                    if array_field is None:
                        try:
                            parsed = _json.loads(sample[0])
                            if parsed and isinstance(parsed[0], dict):
                                array_field = field
                                array_obj_keys = list(parsed[0].keys())
                            elif parsed:
                                array_field = field
                                array_obj_keys = None
                            else:
                                scalar_fields.append(field)
                        except (ValueError, IndexError, TypeError):
                            scalar_fields.append(field)
                    else:
                        scalar_fields.append(field)
                else:
                    scalar_fields.append(field)

            all_enrichment_fields = set(scalar_fields)
            if array_field:
                all_enrichment_fields.add(array_field)
            safe_enrichment_names = {_sanitize_sql_name(f) for f in all_enrichment_fields}

            base_selects: List[str] = []
            for col in ordered_columns:
                safe_col = _sanitize_sql_name(col)
                if col == key_column:
                    base_selects.append(f"i.key_value as {_quote_identifier(safe_col, 'column alias')}")
                elif safe_col in safe_enrichment_names:
                    base_selects.append(
                        f"json_extract(i.row_json, '{_json_path_for_key(col)}') "
                        f"as {_quote_identifier(f'{safe_col}_input', 'column alias')}"
                    )
                else:
                    base_selects.append(
                        f"json_extract(i.row_json, '{_json_path_for_key(col)}') "
                        f"as {_quote_identifier(safe_col, 'column alias')}"
                    )
            base_selects.append("i.row_order as _row_order")
            base_selects.append("i.status as _status")

            metadata_subqueries = [
                f"'{quoted_run_id}' as _run_id",
                f"'{quoted_prompt_id}' as _prompt_id",
                f"'{quoted_query_hash}' as _query_hash",
                f"'{_sql_quote(run.get('model') or '')}' as _model",
            ]
            scalar_subqueries: List[str] = []
            for field in scalar_fields:
                safe_field = _sanitize_sql_name(field)
                value_type = _get_enrichment_value_type(cursor, enrichment_name, field)
                value_expr = _cast_numeric_value(f"""
                    (SELECT value FROM {ENRICHMENTS_TABLE}
                     WHERE run_id = '{quoted_run_id}'
                       AND key_value = i.key_value
                       AND enrichment_name = '{quoted_enrichment}'
                       AND field_name = '{_sql_quote(field)}'
                     ORDER BY timestamp DESC, id DESC LIMIT 1)
                """.strip(), value_type)
                scalar_subqueries.append(
                    f"{value_expr} as {_quote_identifier(safe_field, 'column alias')}"
                )

            cursor.execute(f"DROP VIEW IF EXISTS {view_ref}")

            if array_field:
                if array_obj_keys:
                    array_selects = [
                        f"json_extract(j.value, '{_json_path_for_key(str(key))}') "
                        f"as {_quote_identifier(_sanitize_sql_name(str(key)), 'column alias')}"
                        for key in array_obj_keys
                    ]
                else:
                    safe_arr = _sanitize_sql_name(array_field)
                    array_selects = [f"j.value as {_quote_identifier(safe_arr, 'column alias')}"]

                all_selects = base_selects + scalar_subqueries + metadata_subqueries + array_selects
                view_sql = f"""
                    CREATE VIEW {view_ref} AS
                    SELECT
                        {', '.join(all_selects)}
                    FROM {ENRICHMENT_RUN_ITEMS_TABLE} i
                    JOIN {ENRICHMENTS_TABLE} e
                      ON e.run_id = '{quoted_run_id}'
                     AND e.key_value = i.key_value
                     AND e.enrichment_name = '{quoted_enrichment}'
                     AND e.field_name = '{_sql_quote(array_field)}'
                     AND e.value IS NOT NULL
                     AND e.value != '[]',
                     json_each(e.value) j
                    WHERE i.run_id = '{quoted_run_id}'
                """
                field_desc = f"{len(scalar_fields)} scalar + '{array_field}' exploded"
            else:
                all_selects = base_selects + scalar_subqueries + metadata_subqueries
                view_sql = f"""
                    CREATE VIEW {view_ref} AS
                    SELECT
                        {', '.join(all_selects)}
                    FROM {ENRICHMENT_RUN_ITEMS_TABLE} i
                    WHERE i.run_id = '{quoted_run_id}'
                """
                field_desc = f"{len(scalar_fields)} scalar"

            cursor.execute(view_sql)
            conn.commit()
            logging.info(f"Created run view '{view_name}' with {field_desc} field(s)")

        return view_name

    if not enrichment_name or not prompt_id or not run_timestamp:
        raise ValueError("create_run_view requires run_id or legacy enrichment_name + prompt_id + run_timestamp")

    # Legacy fallback for historical databases without persisted runs.
    try:
        ts = datetime.fromisoformat(run_timestamp)
        ts_label = ts.strftime('%Y%m%d_%H%M')
    except (ValueError, TypeError):
        ts_label = str(run_timestamp).replace('-', '').replace(':', '').replace('T', '_')[:13]

    safe_name = ''.join(c if c.isalnum() or c == '_' else '_' for c in enrichment_name)
    view_name = _doctrail_view_name(f"run_{safe_name}_{ts_label}")
    view_ref = _quote_identifier(view_name, "view name")
    quoted_prompt = _sql_quote(prompt_id)
    quoted_enrichment = _sql_quote(enrichment_name)

    cross_db = source_db_path and os.path.abspath(source_db_path) != os.path.abspath(db_path)
    if cross_db:
        meta_table = copy_source_metadata(db_path, source_db_path, documents_table)
        table_ref = meta_table
    else:
        table_ref = documents_table
    table_ref_quoted = _quote_identifier(table_ref, "documents table")

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT DISTINCT field_name FROM {ENRICHMENTS_TABLE}
            WHERE enrichment_name = ? AND prompt_hash = ?
            ORDER BY field_name
        """, (enrichment_name, prompt_id))
        field_names = [row[0] for row in cursor.fetchall()]
        if not field_names:
            logging.warning(f"No enrichments found for '{enrichment_name}' with prompt {prompt_id[:8]}")
            return view_name

        cursor.execute(f"PRAGMA table_info({table_ref_quoted})")
        doc_col_names = [col[1] for col in cursor.fetchall()]
        doc_key_col = key_column if key_column in doc_col_names else ('sha1' if 'sha1' in doc_col_names else ('attachment_sha1' if 'attachment_sha1' in doc_col_names else key_column))
        doc_key_ref = _quote_identifier(doc_key_col, "key column")
        safe_field_names = {_sanitize_sql_name(f) for f in field_names}
        id_select = []
        for col in [doc_key_col] + priority_columns:
            if col not in doc_col_names:
                continue
            col_ref = _quote_identifier(col, "source column")
            if col == doc_key_col:
                id_select.append(f'd.{col_ref}')
            elif _sanitize_sql_name(col) in safe_field_names:
                id_select.append(
                    f'd.{col_ref} as {_quote_identifier(f"{_sanitize_sql_name(col)}_input", "column alias")}'
                )
            else:
                id_select.append(f'd.{col_ref}')
        scalar_subqueries = []
        for field in field_names:
            safe_field = _sanitize_sql_name(field)
            value_type = _get_enrichment_value_type(cursor, enrichment_name, field)
            value_expr = _cast_numeric_value(f"""
                (SELECT value FROM {ENRICHMENTS_TABLE}
                 WHERE key_value = d.{doc_key_ref}
                   AND enrichment_name = '{quoted_enrichment}'
                   AND field_name = '{_sql_quote(field)}'
                   AND prompt_hash = '{quoted_prompt}'
                 ORDER BY timestamp DESC, id DESC LIMIT 1)
            """.strip(), value_type)
            scalar_subqueries.append(
                f"{value_expr} as {_quote_identifier(safe_field, 'column alias')}"
            )
        cursor.execute(f"DROP VIEW IF EXISTS {view_ref}")
        cursor.execute(f"""
            CREATE VIEW {view_ref} AS
            SELECT
                {', '.join(id_select + scalar_subqueries)}
            FROM {table_ref_quoted} d
            WHERE d.{doc_key_ref} IN (
                SELECT DISTINCT key_value
                FROM {ENRICHMENTS_TABLE}
                WHERE enrichment_name = '{quoted_enrichment}'
                  AND prompt_hash = '{quoted_prompt}'
            )
        """)
        conn.commit()
    return view_name

def create_final_run_view(
    db_path: str,
    run_id: str,
    view_name: Optional[str] = None,
    priority_columns: Optional[List[str]] = None,
) -> str:
    """Create a final merged view that overlays manual overrides onto one run."""
    ensure_enrichments_table(db_path)
    ensure_run_tracking_tables(db_path)
    run = get_enrichment_run(db_path, run_id)
    if not run:
        raise ValueError(f"Run '{run_id}' not found")

    if priority_columns is None:
        priority_columns = [
            'title', 'headline_main', 'bibtex_key',
            'authors', 'year', 'pub_date', 'date',
            'doi', 'publication_title', 'source',
        ]

    started_at = run.get("command_started_at") or run.get("started_at") or datetime.now().isoformat()
    try:
        ts_label = datetime.fromisoformat(started_at).strftime('%Y%m%d_%H%M')
    except (ValueError, TypeError):
        ts_label = str(started_at).replace('-', '').replace(':', '').replace('T', '_')[:13]

    safe_name = ''.join(c if c.isalnum() or c == '_' else '_' for c in run["enrichment_name"])
    view_name = _doctrail_view_name(view_name or f"final_{safe_name}_{ts_label}_{run_id[:8]}")
    view_ref = _quote_identifier(view_name, "view name")
    quoted_run_id = _sql_quote(run_id)
    quoted_enrichment = _sql_quote(run["enrichment_name"])
    key_column = run.get("key_column") or DEFAULT_KEY_COLUMN

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT row_json
            FROM {ENRICHMENT_RUN_ITEMS_TABLE}
            WHERE run_id = ? AND row_json IS NOT NULL
            ORDER BY row_order
            LIMIT 1
        """, (run_id,))
        first_row = cursor.fetchone()
        ordered_snapshot_columns: List[str] = []
        if first_row and first_row[0]:
            try:
                ordered_snapshot_columns = list(json.loads(first_row[0]).keys())
            except json.JSONDecodeError:
                ordered_snapshot_columns = []

        cursor.execute(f"""
            SELECT DISTINCT j.key
            FROM {ENRICHMENT_RUN_ITEMS_TABLE} i, json_each(i.row_json) j
            WHERE i.run_id = ? AND i.row_json IS NOT NULL
            ORDER BY j.key
        """, (run_id,))
        discovered_columns = [row[0] for row in cursor.fetchall()]
        for col in discovered_columns:
            if col not in ordered_snapshot_columns:
                ordered_snapshot_columns.append(col)
        if key_column not in ordered_snapshot_columns:
            ordered_snapshot_columns.insert(0, key_column)

        preferred = [key_column, 'rowid'] + priority_columns
        ordered_columns = [col for col in preferred if col in ordered_snapshot_columns]
        ordered_columns.extend(col for col in ordered_snapshot_columns if col not in ordered_columns)

        cursor.execute(f"""
            SELECT DISTINCT field_name
            FROM {ENRICHMENTS_TABLE}
            WHERE run_id = ?
            ORDER BY field_name
        """, (run_id,))
        field_names = [row[0] for row in cursor.fetchall()]

        safe_enrichment_names = {_sanitize_sql_name(f) for f in field_names}

        base_selects: List[str] = []
        for col in ordered_columns:
            safe_col = _sanitize_sql_name(col)
            if col == key_column:
                base_selects.append(f"i.key_value as {_quote_identifier(safe_col, 'column alias')}")
            elif safe_col in safe_enrichment_names:
                base_selects.append(
                    f"json_extract(i.row_json, '{_json_path_for_key(col)}') "
                    f"as {_quote_identifier(f'{safe_col}_input', 'column alias')}"
                )
            else:
                base_selects.append(
                    f"json_extract(i.row_json, '{_json_path_for_key(col)}') "
                    f"as {_quote_identifier(safe_col, 'column alias')}"
                )
        base_selects.append("i.row_order as _row_order")
        base_selects.append("i.status as _status")

        merged_fields: List[str] = []
        for field in field_names:
            safe_field = _sanitize_sql_name(field)
            value_type = _get_enrichment_value_type(cursor, run["enrichment_name"], field)
            value_expr = _cast_numeric_value(f"""
                COALESCE(
                    (SELECT override_value FROM {ENRICHMENT_OVERRIDES_TABLE}
                     WHERE run_id = '{quoted_run_id}'
                       AND key_value = i.key_value
                       AND enrichment_name = '{quoted_enrichment}'
                       AND field_name = '{_sql_quote(field)}'
                     ORDER BY updated_at DESC LIMIT 1),
                    (SELECT value FROM {ENRICHMENTS_TABLE}
                     WHERE run_id = '{quoted_run_id}'
                       AND key_value = i.key_value
                       AND enrichment_name = '{quoted_enrichment}'
                       AND field_name = '{_sql_quote(field)}'
                     ORDER BY timestamp DESC, id DESC LIMIT 1)
                )
            """.strip(), value_type)
            merged_fields.append(
                f"{value_expr} as {_quote_identifier(safe_field, 'column alias')}"
            )
        merged_fields.append(f"""
            EXISTS(
                SELECT 1 FROM {ENRICHMENT_OVERRIDES_TABLE} o
                WHERE o.run_id = '{quoted_run_id}' AND o.key_value = i.key_value
            ) as _has_override
        """)
        merged_fields.append(f"'{_sql_quote(run.get('model') or '')}' as _model")
        merged_fields.append(f"'{quoted_run_id}' as _run_id")

        cursor.execute(f"DROP VIEW IF EXISTS {view_ref}")
        cursor.execute(f"""
            CREATE VIEW {view_ref} AS
            SELECT
                {', '.join(base_selects + merged_fields)}
            FROM {ENRICHMENT_RUN_ITEMS_TABLE} i
            WHERE i.run_id = '{quoted_run_id}'
        """)
        conn.commit()
    return view_name

def ensure_final_tables_registry(db_path: str) -> None:
    """Track editable final tables materialized from runs or review views."""
    with get_db_connection(db_path) as conn:
        run_pending_migrations(conn)
        conn.commit()

def materialize_view_as_table(
    db_path: str,
    view_name: str,
    table_name: str,
    replace: bool = False,
    source_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Copy a view into a writable table for direct human editing."""
    safe_view_name = _validate_sql_identifier(view_name, "view name")
    safe_table_name = _validate_sql_identifier(table_name, "table name")
    view_ref = _quote_identifier(safe_view_name, "view name")
    table_ref = _quote_identifier(safe_table_name, "table name")
    ensure_final_tables_registry(db_path)

    with get_db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view' AND name = ?",
            (safe_view_name,),
        )
        if not cursor.fetchone():
            raise ValueError(f"View not found: {safe_view_name}")

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (safe_table_name,),
        )
        exists = cursor.fetchone() is not None
        if exists and not replace:
            raise ValueError(
                f"Table '{safe_table_name}' already exists. Use --replace to rebuild it explicitly."
            )
        if exists and replace:
            cursor.execute(f"DROP TABLE {table_ref}")

        cursor.execute(f"CREATE TABLE {table_ref} AS SELECT * FROM {view_ref}")
        row_count = cursor.execute(f"SELECT COUNT(*) FROM {table_ref}").fetchone()[0]
        columns = [row["name"] for row in cursor.execute(f"PRAGMA table_info({table_ref})").fetchall()]

        if "_key_value" in columns:
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS {_quote_identifier(f'idx_{safe_table_name}_key')} "
                f"ON {table_ref}({_quote_identifier('_key_value')})"
            )
        elif "key_value" in columns:
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS {_quote_identifier(f'idx_{safe_table_name}_key')} "
                f"ON {table_ref}({_quote_identifier('key_value')})"
            )

        if "_key_value" in columns and "_item_index" in columns:
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS {_quote_identifier(f'idx_{safe_table_name}_item')} "
                f"ON {table_ref}({_quote_identifier('_key_value')}, {_quote_identifier('_item_index')})"
            )

        cursor.execute(f"""
            INSERT OR REPLACE INTO {DOCTRAIL_FINAL_TABLES_TABLE} (
                table_name, source_view, source_run_id, created_at
            ) VALUES (?, ?, ?, ?)
        """, (
            safe_table_name,
            safe_view_name,
            source_run_id,
            datetime.now().isoformat(),
        ))
        conn.commit()

    return {
        "table_name": safe_table_name,
        "source_view": safe_view_name,
        "source_run_id": source_run_id,
        "row_count": row_count,
        "columns": columns,
        "replaced": exists and replace,
    }

def create_editable_final_table(
    db_path: str,
    *,
    run_id: Optional[str] = None,
    view_name: Optional[str] = None,
    table_name: Optional[str] = None,
    replace: bool = False,
    priority_columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Materialize a chosen final surface into a writable table."""
    if bool(run_id) == bool(view_name):
        raise ValueError("Provide exactly one of run_id or view_name")

    source_view = view_name
    resolved_run_id = run_id
    enrichment_name: Optional[str] = None

    if run_id:
        run = get_enrichment_run(db_path, run_id)
        if not run:
            raise ValueError(f"Run '{run_id}' not found")
        enrichment_name = run["enrichment_name"]
        source_view = create_final_run_view(
            db_path=db_path,
            run_id=run_id,
            priority_columns=priority_columns,
        )
        if not table_name:
            safe_name = ''.join(c if c.isalnum() or c == '_' else '_' for c in enrichment_name)
            table_name = f"final_table_{safe_name}_{run_id[:8]}"
    else:
        source_view = _validate_sql_identifier(view_name or "", "view name")
        if not table_name:
            table_name = f"{source_view}_table"

    result = materialize_view_as_table(
        db_path=db_path,
        view_name=source_view,
        table_name=table_name,
        replace=replace,
        source_run_id=resolved_run_id,
    )
    if enrichment_name:
        result["enrichment_name"] = enrichment_name
    result["source_kind"] = "run" if run_id else "view"
    return result

def diff_enrichment_runs(
    db_path: str,
    run_a: str,
    run_b: str,
    limit: int = 20,
) -> Dict[str, Any]:
    """Compare two runs and return disagreement counts plus example rows."""
    ensure_enrichments_table(db_path)
    ensure_enrichment_runs_table(db_path)
    a_meta = get_enrichment_run(db_path, run_a)
    b_meta = get_enrichment_run(db_path, run_b)
    if not a_meta or not b_meta:
        raise ValueError("Both run IDs must exist")
    if a_meta["enrichment_name"] != b_meta["enrichment_name"]:
        raise ValueError("Runs must belong to the same enrichment_name")

    with get_db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(f"""
            WITH a AS (
                SELECT key_value, field_name, value
                FROM {ENRICHMENTS_TABLE}
                WHERE run_id = ?
            ),
            b AS (
                SELECT key_value, field_name, value
                FROM {ENRICHMENTS_TABLE}
                WHERE run_id = ?
            ),
            keys AS (
                SELECT key_value, field_name FROM a
                UNION
                SELECT key_value, field_name FROM b
            ),
            compared AS (
                SELECT
                    k.key_value,
                    k.field_name,
                    (SELECT value FROM a WHERE a.key_value = k.key_value AND a.field_name = k.field_name LIMIT 1) AS a_value,
                    (SELECT value FROM b WHERE b.key_value = k.key_value AND b.field_name = k.field_name LIMIT 1) AS b_value
                FROM keys k
            )
            SELECT
                COUNT(*) AS compared_cells,
                SUM(CASE WHEN COALESCE(a_value, '__NULL__') != COALESCE(b_value, '__NULL__') THEN 1 ELSE 0 END) AS disagreement_cells,
                COUNT(DISTINCT CASE WHEN COALESCE(a_value, '__NULL__') != COALESCE(b_value, '__NULL__') THEN key_value END) AS disagreement_rows
            FROM compared
        """, (run_a, run_b))
        summary = dict(cursor.fetchone())

        cursor.execute(f"""
            WITH a AS (
                SELECT key_value, field_name, value
                FROM {ENRICHMENTS_TABLE}
                WHERE run_id = ?
            ),
            b AS (
                SELECT key_value, field_name, value
                FROM {ENRICHMENTS_TABLE}
                WHERE run_id = ?
            ),
            keys AS (
                SELECT key_value, field_name FROM a
                UNION
                SELECT key_value, field_name FROM b
            ),
            compared AS (
                SELECT
                    k.key_value,
                    k.field_name,
                    (SELECT value FROM a WHERE a.key_value = k.key_value AND a.field_name = k.field_name LIMIT 1) AS a_value,
                    (SELECT value FROM b WHERE b.key_value = k.key_value AND b.field_name = k.field_name LIMIT 1) AS b_value
                FROM keys k
            )
            SELECT *
            FROM compared
            WHERE COALESCE(a_value, '__NULL__') != COALESCE(b_value, '__NULL__')
            ORDER BY key_value, field_name
            LIMIT ?
        """, (run_a, run_b, limit))
        examples = [dict(row) for row in cursor.fetchall()]

    return {
        "run_a": a_meta,
        "run_b": b_meta,
        "compared_cells": summary["compared_cells"] or 0,
        "disagreement_cells": summary["disagreement_cells"] or 0,
        "disagreement_rows": summary["disagreement_rows"] or 0,
        "examples": examples,
    }

def ensure_icr_samples_table(db_path: str) -> None:
    """Create the icr_samples table if it doesn't exist.

    Stores which key values were selected for each ICR run so that the same
    sample can be analysed repeatedly or compared across runs.

    Uses 'key_value' column (generic) and 'key_column' metadata column to
    record which column the key came from.
    """
    with get_db_connection(db_path) as conn:
        run_pending_migrations(conn)
        conn.commit()
        logging.debug("Ensured icr_samples table exists")

def get_icr_codings(
    db_path: str,
    field_name: str,
    enrichment_name: Optional[str] = None,
    models: Optional[List[str]] = None,
    sha1s: Optional[List[str]] = None,
    key_column: str = DEFAULT_KEY_COLUMN,
) -> List[Dict[str, Any]]:
    """Query the enrichments table for ICR coding data.

    Returns a list of dicts with keys: key_value, model, value.
    """
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row

        query = f"SELECT key_value, model, value FROM {ENRICHMENTS_TABLE} WHERE field_name = ?"
        params: list = [field_name]

        if enrichment_name:
            query += " AND enrichment_name = ?"
            params.append(enrichment_name)

        if models:
            placeholders = ",".join("?" for _ in models)
            query += f" AND model IN ({placeholders})"
            params.extend(models)

        if sha1s:
            placeholders = ",".join("?" for _ in sha1s)
            query += f" AND key_value IN ({placeholders})"
            params.extend(sha1s)

        query += " ORDER BY key_value, model"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

def _sanitize_sql_name(name: str) -> str:
    """Reduce a string to [a-zA-Z0-9_], safe for SQL identifiers."""
    safe = ''.join(c if c.isalnum() or c == '_' else '_' for c in str(name))
    if not safe:
        return "_"
    if not (safe[0].isalpha() or safe[0] == "_"):
        safe = f"_{safe}"
    return safe

def _shorten_model_names(models: List[str]) -> Dict[str, str]:
    """Map full model names to numbered short prefixes: m1, m2, ..."""
    return {m: f"m{i}" for i, m in enumerate(sorted(models), 1)}

def _resolve_latest_run_id(db_path: str, enrichment_name: str) -> Optional[str]:
    """Return the newest persisted run ID for an enrichment, if any."""
    ensure_enrichment_runs_table(db_path)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT run_id
            FROM {ENRICHMENT_RUNS_TABLE}
            WHERE enrichment_name = ?
            ORDER BY command_started_at DESC
            LIMIT 1
        """, (enrichment_name,))
        row = cursor.fetchone()
        return row[0] if row else None

def _normalize_view_spec_columns(
    spec_columns: Optional[List[Any]],
    *,
    primary_enrichment: str,
    available_fields: List[str],
    explode_field: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Normalize a view-spec column list into explicit field descriptors."""
    normalized: List[Dict[str, Any]] = []

    if spec_columns is None:
        for field_name in available_fields:
            if explode_field and field_name == explode_field:
                continue
            normalized.append({
                "enrichment": primary_enrichment,
                "field": field_name,
                "alias": _sanitize_sql_name(field_name),
            })
        return normalized

    for item in spec_columns:
        if isinstance(item, str):
            normalized.append({
                "enrichment": primary_enrichment,
                "field": item,
                "alias": _sanitize_sql_name(item),
            })
            continue

        if not isinstance(item, dict) or not item.get("field"):
            raise ValueError("Each view-spec column must be a string or a mapping with a 'field' key")

        field_name = str(item["field"])
        alias = item.get("alias") or field_name
        normalized.append({
            "enrichment": str(item.get("enrichment") or primary_enrichment),
            "field": field_name,
            "alias": _sanitize_sql_name(str(alias)),
            "run_id": item.get("run_id"),
        })

    return normalized

def _resolve_view_spec_run_id(
    db_path: str,
    enrichment_name: str,
    requested_run_id: Optional[str],
) -> Optional[str]:
    """Resolve a spec run_id, allowing the sentinel value 'latest'."""
    if not requested_run_id:
        return None
    if requested_run_id == "latest":
        latest = _resolve_latest_run_id(db_path, enrichment_name)
        if not latest:
            raise ValueError(f"No persisted runs found for enrichment '{enrichment_name}'")
        return latest
    return requested_run_id

def create_pivot_view(
    db_path: str,
    view_name: str,
    enrichment_name: str,
    documents_table: str = 'documents',
    key_column: str = 'sha1',
    fields: Optional[List[str]] = None,
    include_columns: Optional[List[str]] = None,
    by_model: bool = False,
) -> Dict[str, Any]:
    """Create a wide-format SQLite view pivoting enrichment EAV data.

    Basic mode: one column per field (latest value).
    ICR mode (by_model=True): one column per (model, field) pair with m1_/m2_ prefixes.

    Args:
        db_path: Path to database
        view_name: Name for the created view (will be sanitized)
        enrichment_name: Name of the enrichment to pivot
        documents_table: Source table to join
        key_column: Key column for joining (default: sha1)
        fields: Specific field names to include (None = auto-discover all)
        include_columns: Source table columns to include, supports :N truncation
        by_model: If True, create per-model columns (ICR mode)

    Returns:
        Dict with keys: view_name, columns, row_count, model_legend (if ICR)
    """
    safe_view = _doctrail_view_name(_sanitize_sql_name(view_name))
    safe_view_ref = _quote_identifier(safe_view, "view name")
    documents_table_ref = _quote_identifier(documents_table, "documents table")

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # ── 1. Discover available fields ──
        cursor.execute(f"""
            SELECT DISTINCT field_name FROM {ENRICHMENTS_TABLE}
            WHERE enrichment_name = ?
            ORDER BY field_name
        """, (enrichment_name,))
        available_fields = [row[0] for row in cursor.fetchall()]

        if not available_fields:
            raise ValueError(f"No enrichments found for '{enrichment_name}'")

        # Filter to requested fields
        if fields:
            missing = [f for f in fields if f not in available_fields]
            if missing:
                logging.warning(f"Fields not found in enrichment: {', '.join(missing)}")
            use_fields = [f for f in fields if f in available_fields]
            if not use_fields:
                raise ValueError(f"None of the requested fields exist. Available: {', '.join(available_fields)}")
        else:
            use_fields = available_fields

        # ── 2. Detect key column in source table ──
        cursor.execute(f"PRAGMA table_info({documents_table_ref})")
        doc_col_names = [col[1] for col in cursor.fetchall()]

        if key_column in doc_col_names:
            doc_key_col = key_column
        elif 'sha1' in doc_col_names:
            doc_key_col = 'sha1'
        elif 'attachment_sha1' in doc_col_names:
            doc_key_col = 'attachment_sha1'
        else:
            doc_key_col = key_column
        doc_key_ref = _quote_identifier(doc_key_col, "key column")

        # ── 3. Build include columns from source table ──
        safe_enrichment_fields = {_sanitize_sql_name(f) for f in use_fields}
        if include_columns is not None:
            include_selects = []
            for spec in include_columns:
                if ':' in spec:
                    col, length = spec.rsplit(':', 1)
                    col = col.strip()
                    if col in doc_col_names:
                        alias = _sanitize_sql_name(col)
                        if alias in safe_enrichment_fields:
                            alias = f"{alias}_input"
                        include_selects.append(
                            f"SUBSTR(d.{_quote_identifier(col, 'source column')}, 1, {int(length)}) "
                            f"as {_quote_identifier(alias, 'column alias')}"
                        )
                else:
                    col = spec.strip()
                    if col in doc_col_names:
                        safe_col = _sanitize_sql_name(col)
                        if safe_col in safe_enrichment_fields:
                            include_selects.append(
                                f"d.{_quote_identifier(col, 'source column')} "
                                f"as {_quote_identifier(f'{safe_col}_input', 'column alias')}"
                            )
                        else:
                            include_selects.append(f"d.{_quote_identifier(col, 'source column')}")
        else:
            # Default: common identifier columns that exist in this table
            default_cols = ['title', 'headline_main', 'filename', 'bibtex_key',
                            'authors', 'year', 'pub_date']
            if by_model:
                default_cols.extend(['country', 'raw_content:500'])
            include_selects = []
            for spec in default_cols:
                if ':' in spec:
                    col, length = spec.rsplit(':', 1)
                    col = col.strip()
                    if col not in doc_col_names:
                        continue
                    alias = _sanitize_sql_name(col)
                    if alias in safe_enrichment_fields:
                        alias = f"{alias}_input"
                    include_selects.append(
                        f"SUBSTR(d.{_quote_identifier(col, 'source column')}, 1, {int(length)}) "
                        f"as {_quote_identifier(alias, 'column alias')}"
                    )
                    continue
                col = spec.strip()
                if col not in doc_col_names:
                    continue
                if col in safe_enrichment_fields:
                    include_selects.append(
                        f"d.{_quote_identifier(col, 'source column')} "
                        f"as {_quote_identifier(f'{col}_input', 'column alias')}"
                    )
                else:
                    include_selects.append(f"d.{_quote_identifier(col, 'source column')}")

        # Always include the key column first
        key_select = f"d.{doc_key_ref}"

        # ── 4. Build enrichment subqueries ──
        enrichment_selects = []
        model_legend = None

        if by_model:
            # Discover models used for this enrichment
            cursor.execute(f"""
                SELECT DISTINCT model FROM {ENRICHMENTS_TABLE}
                WHERE enrichment_name = ? AND model IS NOT NULL
                ORDER BY model
            """, (enrichment_name,))
            models = [row[0] for row in cursor.fetchall()]

            if len(models) <= 1:
                logging.warning(f"Only {len(models)} model(s) found — falling back to basic mode")
                by_model = False

        if by_model:
            legend = _shorten_model_names(models)
            model_legend = legend
            for field in use_fields:
                safe_field = _sanitize_sql_name(field)
                value_type = _get_enrichment_value_type(cursor, enrichment_name, field)
                for model_name, prefix in sorted(legend.items(), key=lambda x: x[1]):
                    col_name = f"{prefix}_{safe_field}"
                    value_expr = _cast_numeric_value(f"""
                (SELECT value FROM {ENRICHMENTS_TABLE}
                 WHERE key_value = d.{doc_key_ref}
                   AND enrichment_name = {_sql_literal(enrichment_name)}
                   AND field_name = {_sql_literal(field)}
                   AND model = {_sql_literal(model_name)}
                 ORDER BY timestamp DESC, id DESC LIMIT 1)""".strip(), value_type)
                    enrichment_selects.append(
                        f"{value_expr} as {_quote_identifier(col_name, 'column alias')}"
                    )
        else:
            for field in use_fields:
                safe_field = _sanitize_sql_name(field)
                value_type = _get_enrichment_value_type(cursor, enrichment_name, field)
                value_expr = _cast_numeric_value(f"""
                (SELECT value FROM {ENRICHMENTS_TABLE}
                 WHERE key_value = d.{doc_key_ref}
                   AND enrichment_name = {_sql_literal(enrichment_name)}
                   AND field_name = {_sql_literal(field)}
                 ORDER BY timestamp DESC, id DESC LIMIT 1)""".strip(), value_type)
                enrichment_selects.append(
                    f"{value_expr} as {_quote_identifier(safe_field, 'column alias')}"
                )

        # ── 5. Build and execute CREATE VIEW ──
        # Scope the view to rows that actually have enrichments
        all_selects = [key_select] + include_selects + enrichment_selects
        select_clause = ',\n                '.join(all_selects)

        view_sql = f"""CREATE VIEW {safe_view_ref} AS
            SELECT
                {select_clause}
            FROM {documents_table_ref} d
            WHERE d.{doc_key_ref} IN (
                SELECT DISTINCT key_value FROM {ENRICHMENTS_TABLE}
                WHERE enrichment_name = {_sql_literal(enrichment_name)}
            )"""

        cursor.execute(f"DROP VIEW IF EXISTS {safe_view_ref}")
        cursor.execute(view_sql)
        conn.commit()

        # ── 6. Collect metadata for caller ──
        cursor.execute(f"PRAGMA table_info({safe_view_ref})")
        columns = [row[1] for row in cursor.fetchall()]
        cursor.execute(f"SELECT COUNT(*) FROM {safe_view_ref}")
        row_count = cursor.fetchone()[0]

    result = {
        'view_name': safe_view,
        'columns': columns,
        'row_count': row_count,
    }
    if model_legend:
        result['model_legend'] = model_legend
    return result

def create_view_from_spec(
    db_path: str,
    spec: Dict[str, Any],
    *,
    default_source_table: str = 'documents',
    default_key_column: str = DEFAULT_KEY_COLUMN,
) -> Dict[str, Any]:
    """Create a review/analysis view from a compact YAML-friendly spec."""
    ensure_enrichments_table(db_path)
    ensure_run_tracking_tables(db_path)

    primary_enrichment = spec.get('enrichment')
    if not primary_enrichment:
        raise ValueError("View spec must define 'enrichment'")

    raw_view_name = spec.get('name') or spec.get('view_name')
    if not raw_view_name:
        raise ValueError("View spec must define 'name'")
    view_name = _doctrail_view_name(_sanitize_sql_name(str(raw_view_name)))
    view_name_ref = _quote_identifier(view_name, "view name")

    source_table = str(spec.get('source_table') or default_source_table)
    source_table_ref = _quote_identifier(source_table, "source table")
    key_column = str(spec.get('key_column') or default_key_column)
    requested_run_id = spec.get('run_id')
    run_id = _resolve_view_spec_run_id(db_path, str(primary_enrichment), requested_run_id)
    include_columns = spec.get('include') or []
    explode = spec.get('explode') or None

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # Discover the primary field set for defaults and validation.
        if run_id:
            cursor.execute(f"""
                SELECT DISTINCT field_name
                FROM {ENRICHMENTS_TABLE}
                WHERE enrichment_name = ? AND run_id = ?
                ORDER BY field_name
            """, (primary_enrichment, run_id))
        else:
            cursor.execute(f"""
                SELECT DISTINCT field_name
                FROM {ENRICHMENTS_TABLE}
                WHERE enrichment_name = ?
                ORDER BY field_name
            """, (primary_enrichment,))
        available_fields = [row[0] for row in cursor.fetchall()]
        if not available_fields:
            scope = f" run '{run_id[:8]}'" if run_id else ""
            raise ValueError(f"No enrichments found for '{primary_enrichment}'{scope}")

        explode_field = None
        if explode:
            if not isinstance(explode, dict) or not explode.get('field'):
                raise ValueError("explode must be a mapping with at least a 'field' key")
            explode_field = str(explode['field'])

        columns = _normalize_view_spec_columns(
            spec.get('columns'),
            primary_enrichment=str(primary_enrichment),
            available_fields=available_fields,
            explode_field=explode_field,
        )

        # Build the base rowset. If a run_id is provided, anchor the view to the
        # exact persisted run snapshot; otherwise anchor to the source table.
        base_selects: List[str] = []
        base_filters: List[str] = []
        key_expr: str
        source_alias: str

        # Compute enrichment aliases to detect collisions with source columns
        enrichment_aliases = {_sanitize_sql_name(str(c['alias'])) for c in columns}

        if run_id:
            run = get_enrichment_run(db_path, run_id)
            if not run:
                raise ValueError(f"Run '{run_id}' not found")
            key_expr = "i.key_value"
            source_alias = "i"
            base_from = f"{ENRICHMENT_RUN_ITEMS_TABLE} i"
            base_filters.append(f"i.run_id = '{_sql_quote(run_id)}'")

            for spec_item in include_columns:
                spec_text = str(spec_item)
                if ':' in spec_text:
                    column_name, length = spec_text.rsplit(':', 1)
                    column_name = column_name.strip()
                    safe_alias = _sanitize_sql_name(column_name)
                    if safe_alias in enrichment_aliases:
                        safe_alias = f"{safe_alias}_input"
                    base_selects.append(
                        f"SUBSTR(json_extract(i.row_json, '{_json_path_for_key(column_name)}'), 1, {int(length)}) as {safe_alias}"
                    )
                else:
                    column_name = spec_text.strip()
                    safe_alias = _sanitize_sql_name(column_name)
                    if column_name == key_column:
                        base_selects.append(f"i.key_value as {safe_alias}")
                    elif safe_alias in enrichment_aliases:
                        base_selects.append(
                            f"json_extract(i.row_json, '{_json_path_for_key(column_name)}') as {safe_alias}_input"
                        )
                    else:
                        base_selects.append(
                            f"json_extract(i.row_json, '{_json_path_for_key(column_name)}') as {safe_alias}"
                        )

            base_selects.insert(0, f"i.key_value as {_sanitize_sql_name(key_column)}")
            base_selects.append("i.row_order as _row_order")
            base_selects.append("i.status as _status")
            base_selects.append(f"'{_sql_quote(run_id)}' as _run_id")
        else:
            cursor.execute(f"PRAGMA table_info({source_table_ref})")
            source_columns = [row[1] for row in cursor.fetchall()]
            if key_column in source_columns:
                doc_key_col = key_column
            elif 'sha1' in source_columns:
                doc_key_col = 'sha1'
            elif 'attachment_sha1' in source_columns:
                doc_key_col = 'attachment_sha1'
            else:
                doc_key_col = key_column

            doc_key_ref = _quote_identifier(doc_key_col, "key column")
            key_expr = f"d.{doc_key_ref}"
            source_alias = "d"
            base_from = f"{source_table_ref} d"
            base_filters.append(f"""
                EXISTS (
                    SELECT 1
                    FROM {ENRICHMENTS_TABLE} e_scope
                    WHERE e_scope.key_value = {key_expr}
                      AND e_scope.enrichment_name = '{_sql_quote(str(primary_enrichment))}'
                )
            """.strip())

            base_selects.append(
                f"d.{doc_key_ref} as {_quote_identifier(_sanitize_sql_name(key_column), 'column alias')}"
            )
            for spec_item in include_columns:
                spec_text = str(spec_item)
                if ':' in spec_text:
                    column_name, length = spec_text.rsplit(':', 1)
                    column_name = column_name.strip()
                    safe_alias = _sanitize_sql_name(column_name)
                    if safe_alias in enrichment_aliases:
                        safe_alias = f"{safe_alias}_input"
                    base_selects.append(
                        f"SUBSTR(d.{_quote_identifier(column_name, 'source column')}, 1, {int(length)}) "
                        f"as {_quote_identifier(safe_alias, 'column alias')}"
                    )
                else:
                    column_name = spec_text.strip()
                    if column_name == doc_key_col:
                        continue
                    safe_alias = _sanitize_sql_name(column_name)
                    if safe_alias in enrichment_aliases:
                        base_selects.append(
                            f"d.{_quote_identifier(column_name, 'source column')} "
                            f"as {_quote_identifier(f'{safe_alias}_input', 'column alias')}"
                        )
                    else:
                        base_selects.append(f"d.{_quote_identifier(column_name, 'source column')}")

        # Scalar enrichment columns. By default, a column follows the primary
        # enrichment and the spec's run_id if that run anchors the view.
        scalar_selects: List[str] = []
        for column in columns:
            column_enrichment = str(column['enrichment'])
            column_field = str(column['field'])
            column_alias = _sanitize_sql_name(str(column['alias']))
            column_run_id = _resolve_view_spec_run_id(
                db_path,
                column_enrichment,
                column.get('run_id') or (run_id if column_enrichment == primary_enrichment else None),
            )

            where_bits = [
                f"key_value = {key_expr}",
                f"enrichment_name = '{_sql_quote(column_enrichment)}'",
                f"field_name = '{_sql_quote(column_field)}'",
            ]
            if column_run_id:
                where_bits.append(f"run_id = '{_sql_quote(column_run_id)}'")

            value_type = _get_enrichment_value_type(cursor, column_enrichment, column_field)
            value_expr = _cast_numeric_value(f"""
                (SELECT value FROM {ENRICHMENTS_TABLE}
                 WHERE {' AND '.join(where_bits)}
                 ORDER BY timestamp DESC, id DESC LIMIT 1)
            """.strip(), value_type)
            scalar_selects.append(
                f"{value_expr} as {column_alias}"
            )

        join_clause = ""
        explode_selects: List[str] = []

        if explode:
            explode_enrichment = str(explode.get('enrichment') or primary_enrichment)
            explode_run_id = _resolve_view_spec_run_id(
                db_path,
                explode_enrichment,
                explode.get('run_id') or (run_id if explode_enrichment == primary_enrichment else None),
            )
            explode_field = str(explode['field'])
            alias_prefix = str(explode.get('alias_prefix') or '')
            object_fields = explode.get('object_fields') or explode.get('columns') or None

            explode_where = [
                f"key_value = {key_expr}",
                f"enrichment_name = '{_sql_quote(explode_enrichment)}'",
                f"field_name = '{_sql_quote(explode_field)}'",
            ]
            if explode_run_id:
                explode_where.append(f"run_id = '{_sql_quote(explode_run_id)}'")

            join_clause = f"""
                JOIN {ENRICHMENTS_TABLE} e_explode
                  ON e_explode.id = (
                        SELECT id
                        FROM {ENRICHMENTS_TABLE}
                        WHERE {' AND '.join(explode_where)}
                        ORDER BY timestamp DESC, id DESC
                        LIMIT 1
                    )
                JOIN json_each(e_explode.value) j
            """.strip()

            explode_selects.append("CAST(j.key AS INTEGER) as _item_index")
            if object_fields:
                for key_name in object_fields:
                    alias = _sanitize_sql_name(f"{alias_prefix}{key_name}" if alias_prefix else str(key_name))
                    explode_selects.append(
                        f"json_extract(j.value, '{_json_path_for_key(str(key_name))}') as {alias}"
                    )
            else:
                scalar_alias = _sanitize_sql_name(f"{alias_prefix}{explode_field}" if alias_prefix else explode_field)
                explode_selects.append(f"j.value as {scalar_alias}")

        select_clause = ",\n                ".join(base_selects + scalar_selects + explode_selects)
        view_sql = f"""
            CREATE VIEW {view_name_ref} AS
            SELECT
                {select_clause}
            FROM {base_from}
            {join_clause}
        """.strip()

        if base_filters:
            view_sql += "\nWHERE " + "\n  AND ".join(base_filters)

        cursor.execute(f"DROP VIEW IF EXISTS {view_name_ref}")
        cursor.execute(view_sql)
        conn.commit()

        cursor.execute(f"PRAGMA table_info({view_name_ref})")
        created_columns = [row[1] for row in cursor.fetchall()]
        cursor.execute(f"SELECT COUNT(*) FROM {view_name_ref}")
        row_count = cursor.fetchone()[0]

    return {
        "view_name": view_name,
        "columns": created_columns,
        "row_count": row_count,
        "run_id": run_id,
        "anchored_to": "run" if run_id else "table",
    }

def render_view_output(
    db_path: str,
    view_name: str,
    output_path: str,
    *,
    format_name: str = "html",
    limit: Optional[int] = None,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Render a materialized view to a lightweight export format."""
    query = f"SELECT * FROM {_quote_identifier(_sanitize_sql_name(view_name), 'view name')}"
    if limit:
        query += f" LIMIT {int(limit)}"

    with get_db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query)
        rows = [dict(row) for row in cursor.fetchall()]
        columns = [desc[0] for desc in cursor.description] if cursor.description else []

    output_title = title or view_name

    if format_name == "json":
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
    elif format_name == "csv":
        with open(output_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    elif format_name == "html":
        def render_cell(value: Any) -> str:
            text = "" if value is None else str(value)
            escaped = html.escape(text)
            if len(text) > 300:
                summary = html.escape(text[:300] + "…")
                return f"<details><summary>{summary}</summary><pre>{escaped}</pre></details>"
            return f"<div>{escaped}</div>"

        header_html = "".join(f"<th>{html.escape(column)}</th>" for column in columns)
        body_rows = []
        for row in rows:
            cells = "".join(f"<td>{render_cell(row.get(column))}</td>" for column in columns)
            body_rows.append(f"<tr>{cells}</tr>")

        page = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>{html.escape(output_title)}</title>
  <style>
    body {{ font-family: Georgia, serif; margin: 24px; background: #f5f1e8; color: #1f2937; }}
    h1 {{ margin-bottom: 8px; }}
    .meta {{ margin-bottom: 20px; color: #6b7280; font-size: 14px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fffdf8; }}
    th, td {{ border: 1px solid #d6d3d1; padding: 10px; vertical-align: top; text-align: left; }}
    th {{ position: sticky; top: 0; background: #e7decf; z-index: 1; }}
    td {{ max-width: 520px; white-space: pre-wrap; word-break: break-word; }}
    tr:nth-child(even) {{ background: #faf7f2; }}
    pre {{ white-space: pre-wrap; font-family: ui-monospace, monospace; }}
    summary {{ cursor: pointer; color: #8b4513; }}
  </style>
</head>
<body>
  <h1>{html.escape(output_title)}</h1>
  <div class="meta">{len(rows)} row(s){f" rendered from {html.escape(view_name)}" if view_name else ""}</div>
  <table>
    <thead><tr>{header_html}</tr></thead>
    <tbody>{''.join(body_rows)}</tbody>
  </table>
</body>
</html>
"""
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(page)
    else:
        raise ValueError(f"Unsupported render format: {format_name}")

    return {
        "output_path": output_path,
        "format": format_name,
        "row_count": len(rows),
        "columns": columns,
    }

__all__ = [

    'create_run_view',

    'create_final_run_view',

    'ensure_final_tables_registry',

    'materialize_view_as_table',

    'create_editable_final_table',

    'diff_enrichment_runs',

    'ensure_icr_samples_table',

    'get_icr_codings',

    '_sanitize_sql_name',

    '_shorten_model_names',

    '_resolve_latest_run_id',

    '_normalize_view_spec_columns',

    '_resolve_view_spec_run_id',

    'create_pivot_view',

    'create_view_from_spec',

    'render_view_output',

]
