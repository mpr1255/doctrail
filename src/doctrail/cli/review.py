"""
Review command - validate enrichment accuracy with a web UI.
"""
from typing import Optional

import click

from .main import cli
from .utils import load_config


@cli.command()
@click.argument('db_path', required=True, type=click.Path(exists=True))
@click.option('--field', required=True, help='Field name to review (e.g., is_relevant)')
@click.option('--sample', default=50, type=int, help='Sample size per class (default: 50)')
@click.option('--port', default=8765, type=int, help='Port to run server on (default: 8765)')
@click.option('--table', default='articles', help='Table name (default: articles)')
@click.option('--config', type=click.Path(exists=True), help='Config file to get truncation from input_columns')
@click.option('--truncate', type=int, help='Content truncation limit (overrides config)')
def review(db_path: str, field: str, sample: int, port: int, table: str, config: Optional[str], truncate: Optional[int]):
    """
    Validate enrichment accuracy with a web UI.

    Opens a browser-based interface for rapid Y/N validation of LLM classifications.
    Results are saved to the human_audit table.

    Examples:
        doctrail review /path/to/db.db --field is_relevant --sample 50
        doctrail review db.db --field language --sample 100 --table documents
        doctrail review db.db --field is_relevant --config enrichment.yml
    """
    from ..review_server import run_review_server

    # Determine truncation limit
    trunc_limit = truncate  # CLI override takes precedence

    if trunc_limit is None and config:
        # Try to extract from config's input_columns
        try:
            config_data = load_config(config)
            enrichments = config_data.get('enrichments', [])
            # Find enrichment that produces this field
            for e in enrichments:
                # Check if field matches output column or enrichment name
                schema = e.get('schema', {})
                if isinstance(schema, dict):
                    if field in schema or e.get('name') == field:
                        input_cols = e.get('input', {}).get('input_columns', [])
                        for col in input_cols:
                            if ':' in str(col):
                                # Extract truncation e.g. "raw_content:3000" -> 3000
                                parts = col.split(':')
                                if parts[0] in ('raw_content', 'content', 'text', 'body'):
                                    trunc_limit = int(parts[1])
                                    click.echo(f"Using truncation {trunc_limit} from config ({col})")
                                    break
                        break
        except Exception as e:
            click.echo(f"Could not parse config for truncation: {e}", err=True)

    # Default truncation if still not set
    if trunc_limit is None:
        trunc_limit = 2000

    click.echo(f"Starting review server for field '{field}'...")
    click.echo(f"Database: {db_path}")
    click.echo(f"Sample: {sample} per class, truncation: {trunc_limit} chars")

    run_review_server(
        db_path=db_path,
        field_name=field,
        sample_per_class=sample,
        port=port,
        table_name=table,
        truncate_limit=trunc_limit
    )
