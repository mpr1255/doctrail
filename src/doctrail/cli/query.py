"""
Query commands - database querying, search, and inspection.

Includes: query, sql, fts, chroma, document, stats, and run inspection.
Views are the main user-facing analysis surface; normalized enrichment tables
remain the storage layer underneath them.
"""
import csv
import json
import sqlite3
from pathlib import Path
from typing import Optional

import click

from .main import cli
from .utils import _exit_error, _load_project_config
from ..db_operations import (
    ENRICHMENT_OVERRIDES_TABLE,
    ENRICHMENTS_TABLE,
    _quote_identifier,
)


def _resolve_db_path(db_path: Optional[str]) -> Path:
    """Resolve a database path from CLI input or the current doctrail project."""
    if db_path:
        resolved = Path(db_path).expanduser()
    else:
        try:
            config_data = _load_project_config()
        except click.UsageError as exc:
            raise click.UsageError("No doctrail project found. Run 'doctrail init' first or pass --db-path.") from exc
        resolved = Path(config_data.get('database', './out/documents.db')).expanduser()

    if not resolved.exists():
        raise click.UsageError(f"Database not found: {resolved}")
    return resolved


@cli.command()
@click.argument('query_or_id', required=False)
@click.option('--limit', '-n', default=10, help='Limit number of rows (default: 10)')
@click.option('--table', '-t', default=None, help='Table name (defaults to project default_table, then documents)')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON')
@click.option('--content', '-c', is_flag=True, help='Show content preview')
@click.pass_context
def query(ctx, query_or_id: str, limit: int, table: str, as_json: bool, content: bool):
    """
    Query the database.

    Examples:
        doctrail query                    # List documents
        doctrail query 1                  # Show details of document #1
        doctrail query -c                 # List with content preview
        doctrail query --json             # Full JSON output
        doctrail query "SELECT ..."       # Custom SQL
    """
    import sqlite_utils
    import json

    # Load config to get database path
    try:
        config_data = _load_project_config()
        db_path = config_data.get('database', './out/documents.db')
        table = table or config_data.get('default_table') or 'documents'
    except click.UsageError:
        _exit_error("No doctrail project found. Run 'doctrail init' first.")

    db_path = Path(db_path).expanduser()
    if not db_path.exists():
        click.echo(f"Database not found: {db_path}", err=True)
        click.echo("Run 'doctrail ingest' first to import documents.", err=True)
        raise click.exceptions.Exit(1)

    db = sqlite_utils.Database(str(db_path))

    # Check if query_or_id is a number (document ID)
    if query_or_id and query_or_id.isdigit():
        doc_id = int(query_or_id)
        rows = list(db[table].rows_where("rowid = ?", [doc_id]))
        if not rows:
            _exit_error(f"Document #{doc_id} not found.")

        row = rows[0]
        filename = row.get('filename', 'unknown')
        click.echo(f"\n[Document #{doc_id}]")
        click.echo(f"  Filename: {filename}")

        # Show key fields
        for key in ['title', 'extraction_method', 'sha1']:
            if row.get(key):
                click.echo(f"  {key}: {row[key]}")

        # Show content preview
        raw = row.get('raw_content', '')
        if raw:
            click.echo(f"  Content: {len(raw):,} chars")
            preview = raw[:500].replace('\n', ' ')
            click.echo(f"\n  {preview}...")

        # Show any normalized enrichment values for this document
        if ENRICHMENTS_TABLE in db.table_names():
            sha1 = row.get('sha1')
            if sha1:
                enrichments_table = db[ENRICHMENTS_TABLE]
                enrichment_columns = {column.name for column in enrichments_table.columns}
                key_col_name = 'key_value' if 'key_value' in enrichment_columns else 'sha1'
                enrichments = list(enrichments_table.rows_where(f"{key_col_name} = ?", [sha1]))
                if enrichments:
                    click.echo(f"\n  Enrichments:")
                    for e in enrichments:
                        click.echo(f"    • {e['field_name']}: {e['value']}")
        return

    # Check if it's a SQL query
    if query_or_id and query_or_id.upper().startswith('SELECT'):
        try:
            rows = list(db.execute(query_or_id).fetchall())
            columns = [d[0] for d in db.execute(query_or_id).description] if rows else []
            if as_json:
                result = [dict(zip(columns, row)) for row in rows]
                click.echo(json.dumps(result, indent=2, default=str))
            else:
                if rows:
                    click.echo("  ".join(columns))
                    click.echo("-" * 60)
                    for row in rows:
                        click.echo("  ".join(str(v)[:50] for v in row))
                click.echo(f"\n({len(rows)} rows)")
        except Exception as e:
            _exit_error(f"SQL error: {e}")
        return

    # Default: list documents
    if table not in db.table_names():
        click.echo(f"Table '{table}' not found.", err=True)
        click.echo(f"Available tables: {', '.join(db.table_names())}", err=True)
        raise click.exceptions.Exit(1)

    table_ref = _quote_identifier(table, "table name")
    rows = list(db.execute(f"SELECT rowid, * FROM {table_ref} LIMIT ?", [limit]).fetchall())
    columns = [d[0] for d in db.execute(f"SELECT rowid, * FROM {table_ref} LIMIT 1").description]

    if as_json:
        result = [dict(zip(columns, row)) for row in rows]
        click.echo(json.dumps(result, indent=2, default=str))
        return

    if not rows:
        click.echo("No documents found.")
        return

    # Convert to dicts for easier access
    rows = [dict(zip(columns, row)) for row in rows]

    # Show table
    click.echo(f"\n{'#':<4} {'Filename':<50} {'Chars':>10}")
    click.echo("-" * 68)

    for row in rows:
        rowid = row.get('rowid', '?')
        filename = row.get('filename', row.get('sha1', 'unknown'))
        if len(filename) > 48:
            filename = filename[:22] + "..." + filename[-23:]
        raw = row.get('raw_content', '')
        chars = len(raw) if raw else 0
        click.echo(f"{rowid:<4} {filename:<50} {chars:>10,}")

        # Show content preview if requested
        if content and raw:
            preview = raw[:100].replace('\n', ' ').strip()
            click.echo(f"     [dim]{preview}...[/dim]")

    total = db[table].count
    click.echo("-" * 68)
    click.echo(f"Showing {len(rows)} of {total} documents")
    click.echo(f"\nTip: doctrail query 1  (show details of document #1)")


@cli.command()
@click.option('--db-path', required=True, type=click.Path(exists=True), help='Path to SQLite database')
@click.option('--query', '-q', required=True, help='SQL SELECT query')
@click.option('--format', 'output_format', type=click.Choice(['text', 'json']), default='text', help='Output format')
@click.pass_context
def sql(ctx, db_path, query, output_format):
    """Execute a read-only SQL query (SELECT only)."""
    from ..search import get_connection, sql_query, format_sql_results_text

    conn = get_connection(db_path)
    try:
        result = sql_query(conn, query)

        if output_format == "json":
            import json
            click.echo(json.dumps(result, indent=2, default=str))
        else:
            click.echo(format_sql_results_text(result))
    finally:
        conn.close()


@cli.command()
@click.option('--db-path', required=True, type=click.Path(exists=True), help='Path to SQLite database')
@click.option('--id', 'doc_id', required=True, help='Document ID (primary key value)')
@click.option('--format', 'output_format', type=click.Choice(['text', 'json']), default='text', help='Output format')
@click.pass_context
def document(ctx, db_path, doc_id, output_format):
    """Get a single document by ID."""
    from ..search import get_connection, get_document, format_document_text
    from ..server_config import DatabaseConfig

    db_dir = Path(db_path).parent

    # Try to load schema
    try:
        db_config = DatabaseConfig.from_directory("temp", db_dir)
        pk_col = db_config.schema.pk_column
        doc_table = db_config.schema.documents_table
    except Exception:
        pk_col = "id"
        doc_table = "documents"

    conn = get_connection(db_path)
    try:
        result = get_document(
            conn=conn,
            doc_id=doc_id,
            documents_table=doc_table,
            pk_column=pk_col,
        )

        if result is None:
            _exit_error(f"Document not found: {doc_id}")

        if output_format == "json":
            import json
            click.echo(json.dumps(result.columns, indent=2, default=str))
        else:
            click.echo(format_document_text(result))
    finally:
        conn.close()


@cli.command()
@click.option('--db-path', required=True, type=click.Path(exists=True), help='Path to SQLite database')
@click.option('--format', 'output_format', type=click.Choice(['text', 'json']), default='text', help='Output format')
@click.pass_context
def stats(ctx, db_path, output_format):
    """Get database statistics."""
    from ..search import get_connection, get_stats
    from ..server_config import DatabaseConfig

    db_dir = Path(db_path).parent

    # Try to load schema
    try:
        db_config = DatabaseConfig.from_directory("temp", db_dir)
        doc_table = db_config.schema.documents_table
    except Exception:
        doc_table = "documents"

    conn = get_connection(db_path)
    try:
        result = get_stats(conn, documents_table=doc_table)

        if output_format == "json":
            import json
            click.echo(json.dumps(result, indent=2))
        else:
            click.echo("# Database Statistics")
            click.echo("")
            for key, value in result.items():
                if key == "tables":
                    click.echo(f"Tables: {', '.join(value)}")
                else:
                    click.echo(f"{key}: {value}")
    finally:
        conn.close()


@cli.command('rebuild-enrichments')
@click.option('--db-path', type=click.Path(exists=True), help='Path to SQLite database')
@click.option('--run-id', help='Restrict rebuild to one run ID')
@click.option('--enrichment', 'enrichment_name', help='Restrict rebuild to one enrichment name')
@click.option('--key-value', help='Restrict rebuild to one document key')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
@click.pass_context
def rebuild_enrichments(ctx, db_path: Optional[str], run_id: Optional[str], enrichment_name: Optional[str], key_value: Optional[str], yes: bool):
    """Rebuild _enrichments exactly from projection payloads stored in _enrichment_audit."""
    from ..db_operations import rebuild_enrichments_from_audit

    resolved_db_path = _resolve_db_path(db_path)

    scope_bits = []
    if run_id:
        scope_bits.append(f"run_id={run_id}")
    if enrichment_name:
        scope_bits.append(f"enrichment={enrichment_name}")
    if key_value:
        scope_bits.append(f"key_value={key_value}")
    scope_label = ", ".join(scope_bits) if scope_bits else "all audit rows"

    if not yes and not click.confirm(
        f"Rebuild enrichments from {scope_label}? Matching enrichment rows will be replaced."
    ):
        click.echo("Aborted.")
        return

    try:
        summary = rebuild_enrichments_from_audit(
            db_path=str(resolved_db_path),
            run_id=run_id,
            enrichment_name=enrichment_name,
            key_value=key_value,
            clear_existing=True,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"Rebuilt {summary['written_rows']} enrichment rows from "
        f"{summary['audit_rows']} audit records."
    )


@cli.command('finalize')
@click.option('--db-path', type=click.Path(exists=True), help='Path to SQLite database')
@click.option('--run-id', help='Materialize the final surface for one run ID')
@click.option('--view', 'source_view', help='Materialize an existing review view instead of a run')
@click.option('--table', 'table_name', help='Writable table name to create')
@click.option('--replace', is_flag=True, help='Replace the target table if it already exists')
@click.pass_context
def finalize(ctx, db_path: Optional[str], run_id: Optional[str], source_view: Optional[str], table_name: Optional[str], replace: bool):
    """Materialize an editable final table from a run or existing review view."""
    from ..db_operations import create_editable_final_table

    resolved_db_path = _resolve_db_path(db_path)

    if bool(run_id) == bool(source_view):
        raise click.UsageError("Provide exactly one of --run-id or --view.")

    try:
        result = create_editable_final_table(
            db_path=str(resolved_db_path),
            run_id=run_id,
            view_name=source_view,
            table_name=table_name,
            replace=replace,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Created editable table: {result['table_name']}")
    click.echo(f"  Source: {result['source_view']} ({result['source_kind']})")
    if result.get('source_run_id'):
        click.echo(f"  Run ID: {result['source_run_id']}")
    click.echo(f"  Rows: {result['row_count']}")
    click.echo(f"  Columns: {', '.join(result['columns'])}")
    click.echo("\nEdit it directly with SQLite or any database UI. It will not be refreshed unless you rerun finalize with --replace.")


@cli.command('runs')
@click.option('--db-path', type=click.Path(exists=True), help='Path to SQLite database')
@click.option('--enrichment', help='Filter to one enrichment name')
@click.option('--limit', default=20, show_default=True, help='Max runs to list')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON')
@click.pass_context
def runs(ctx, db_path: Optional[str], enrichment: Optional[str], limit: int, as_json: bool):
    """List recent enrichment runs with persisted run IDs and summary counts."""
    from ..db_operations import list_enrichment_runs

    resolved_db_path = _resolve_db_path(db_path)
    run_rows = list_enrichment_runs(str(resolved_db_path), enrichment_name=enrichment, limit=limit)

    if as_json:
        click.echo(json.dumps(run_rows, indent=2, default=str))
        return

    if not run_rows:
        click.echo("No enrichment runs found.")
        return

    click.echo("run_id    enrichment            model                    status     processed skipped errors cost")
    click.echo("-" * 108)
    for row in run_rows:
        click.echo(
            f"{row['run_id'][:8]:<9} "
            f"{row['enrichment_name'][:20]:<21} "
            f"{row['model'][:24]:<25} "
            f"{row['status'][:10]:<10} "
            f"{row.get('processed_rows', 0):>9} "
            f"{row.get('skipped_rows', 0):>7} "
            f"{row.get('error_count', 0):>6} "
            f"${(row.get('estimated_cost') or 0.0):.4f}"
        )


@cli.command('diff-runs')
@click.option('--db-path', type=click.Path(exists=True), help='Path to SQLite database')
@click.option('--run-a', required=True, help='First run ID')
@click.option('--run-b', required=True, help='Second run ID')
@click.option('--limit', default=20, show_default=True, help='Max differing cells to show')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON')
@click.pass_context
def diff_runs(ctx, db_path: Optional[str], run_a: str, run_b: str, limit: int, as_json: bool):
    """Show where two runs disagree."""
    from ..db_operations import diff_enrichment_runs

    resolved_db_path = _resolve_db_path(db_path)
    diff = diff_enrichment_runs(str(resolved_db_path), run_a, run_b, limit=limit)

    if as_json:
        click.echo(json.dumps(diff, indent=2, default=str))
        return

    click.echo(f"Run A: {diff['run_a']['run_id']}  ({diff['run_a']['model']})")
    click.echo(f"Run B: {diff['run_b']['run_id']}  ({diff['run_b']['model']})")
    click.echo("")
    click.echo(f"Compared cells:     {diff['compared_cells']}")
    click.echo(f"Disagreement cells: {diff['disagreement_cells']}")
    click.echo(f"Disagreement rows:  {diff['disagreement_rows']}")

    examples = diff.get('examples', [])
    if not examples:
        click.echo("\nNo disagreements found.")
        return

    click.echo("\nExamples:")
    for row in examples:
        click.echo(f"  {row['key_value']}  {row['field_name']}")
        click.echo(f"    A: {row['a_value']}")
        click.echo(f"    B: {row['b_value']}")


@cli.command('overrides-export')
@click.option('--db-path', type=click.Path(exists=True), help='Path to SQLite database')
@click.option('--run-id', required=True, help='Run ID to export for review')
@click.option('--output', type=click.Path(), help='CSV path to write (defaults to ./overrides_<run>.csv)')
@click.pass_context
def overrides_export(ctx, db_path: Optional[str], run_id: str, output: Optional[str]):
    """Export one run to a CSV template for human review and overrides."""
    from ..db_operations import create_run_view, get_enrichment_run

    resolved_db_path = _resolve_db_path(db_path)
    run = get_enrichment_run(str(resolved_db_path), run_id)
    if not run:
        raise click.UsageError(f"Run not found: {run_id}")

    output_path = Path(output).expanduser() if output else Path.cwd() / f"overrides_{run['enrichment_name']}_{run_id[:8]}.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    view_name = create_run_view(str(resolved_db_path), run_id=run_id)
    if not view_name:
        raise click.UsageError(f"No enrichment fields found for run '{run_id[:8]}'; no override CSV was created.")
    with sqlite3.connect(str(resolved_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {view_name} ORDER BY _row_order")
        rows = [dict(row) for row in cursor.fetchall()]
        field_names = [
            row[0]
            for row in cursor.execute(
                f"SELECT DISTINCT field_name FROM {ENRICHMENTS_TABLE} WHERE run_id = ? ORDER BY field_name",
                (run_id,),
            ).fetchall()
        ]
        overrides = {
            (row['key_value'], row['field_name']): row
            for row in cursor.execute(
                f"""
                SELECT key_value, field_name, override_value, note
                FROM {ENRICHMENT_OVERRIDES_TABLE}
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchall()
        }

    if not rows:
        click.echo(f"No rows found for run {run_id}.")
        return

    fieldnames = list(rows[0].keys())
    for field_name in field_names:
        fieldnames.append(f"override__{field_name}")
        fieldnames.append(f"note__{field_name}")

    with output_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            key_value = str(row.get(run.get('key_column') or 'key_value') or row.get('key_value'))
            export_row = dict(row)
            for field_name in field_names:
                override_row = overrides.get((key_value, field_name), {})
                export_row[f"override__{field_name}"] = override_row.get('override_value') or ''
                export_row[f"note__{field_name}"] = override_row.get('note') or ''
            writer.writerow(export_row)

    click.echo(f"Exported override template: {output_path}")
    click.echo("Edit the override__* columns, then import with:")
    click.echo(f"  doctrail overrides-import --run-id {run_id} --input {output_path}")


@cli.command('overrides-import')
@click.option('--db-path', type=click.Path(exists=True), help='Path to SQLite database')
@click.option('--run-id', required=True, help='Run ID to import overrides into')
@click.option('--input', 'input_path', required=True, type=click.Path(exists=True), help='CSV created by overrides-export')
@click.option('--reviewer', help='Reviewer name stored with imported overrides')
@click.pass_context
def overrides_import(ctx, db_path: Optional[str], run_id: str, input_path: str, reviewer: Optional[str]):
    """Import human overrides from a CSV and refresh the final merged view."""
    from ..db_operations import (
        create_final_run_view,
        delete_enrichment_override,
        get_enrichment_run,
        upsert_enrichment_override,
    )

    resolved_db_path = _resolve_db_path(db_path)
    run = get_enrichment_run(str(resolved_db_path), run_id)
    if not run:
        raise click.UsageError(f"Run not found: {run_id}")

    imported = 0
    cleared = 0
    key_column = run.get('key_column') or 'key_value'

    with Path(input_path).expanduser().open('r', newline='', encoding='utf-8') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise click.UsageError("Override CSV has no header row.")
        override_columns = [name for name in reader.fieldnames if name.startswith('override__')]
        if not override_columns:
            raise click.UsageError("Override CSV has no override__* columns.")

        for row in reader:
            key_value = row.get(key_column) or row.get('key_value') or row.get('sha1')
            if not key_value:
                continue
            for override_column in override_columns:
                field_name = override_column.split('override__', 1)[1]
                override_value = row.get(override_column, '')
                note = row.get(f'note__{field_name}') or None
                normalized_value = override_value.strip()

                if normalized_value == '__CLEAR__':
                    delete_enrichment_override(
                        str(resolved_db_path),
                        run_id,
                        str(key_value),
                        run['enrichment_name'],
                        field_name,
                    )
                    cleared += 1
                    continue

                if not normalized_value:
                    continue

                upsert_enrichment_override(
                    str(resolved_db_path),
                    run_id,
                    str(key_value),
                    run['enrichment_name'],
                    field_name,
                    override_value,
                    reviewer=reviewer,
                    note=note,
                )
                imported += 1

    final_view = create_final_run_view(str(resolved_db_path), run_id)
    click.echo(f"Imported {imported} override cell(s)")
    if cleared:
        click.echo(f"Cleared {cleared} override cell(s)")
    click.echo(f"Final view refreshed: {final_view}")
    click.echo(f"  SELECT * FROM {final_view} LIMIT 20")
