"""
Export command - export enriched data in various formats.
"""
import asyncio
from typing import Optional

import click

from .main import cli
from .utils import _exit_error, setup_logging


@cli.command()
@click.option('--config', required=True, help='Path to the configuration YAML file')
@click.option('--export-type', required=True, help='Type of export to run (e.g., parallel-translation, case-summaries)')
@click.option('--output-dir', required=False, help='Override the default output directory from config')
@click.option('--verbose', is_flag=True, help='Enable verbose logging')
@click.pass_context
def export(ctx, config: str, export_type: str, output_dir: str, verbose: bool):
    """Export enriched data in various formats."""

    setup_logging(verbose)

    try:
        # Call core API
        from ..core import run_export, ConfigurationError

        result = asyncio.run(run_export(
            config_path=config,
            export_type=export_type,
            output_dir=output_dir,
            verbose=verbose,
        ))

        # Display results
        if result['status'] == 'success':
            click.echo(f"\nExport completed successfully.")
            click.echo(f"Output directory: {result['output_dir']}")
            click.echo(f"Export type: {result['export_type']}")

    except ConfigurationError as e:
        _exit_error(f"\nError: {e}")
    except KeyboardInterrupt:
        click.echo("\nExport interrupted by user.", err=True)
        raise click.exceptions.Exit(1)
    except Exception as e:
        click.echo(f"\nUnexpected error: {e}", err=True)
        if verbose:
            import traceback
            traceback.print_exc()
        raise click.exceptions.Exit(1)
