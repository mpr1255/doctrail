"""
ICR (Intercoder Reliability) commands.

  doctrail icr <enrichment> -m model1 -m model2 ...
  doctrail icr-report --db-path db.db --field hostility_level
"""
import os
import asyncio
from pathlib import Path
from typing import Optional

import click
import yaml

from .main import cli
from .utils import (
    _get_doctrail_dir,
    _get_config_path,
    _write_merged_config,
    _exit_error,
    setup_logging,
    load_config,
)
from ..db_operations import ICR_SAMPLES_TABLE


@cli.command()
@click.argument('enrichment_name')
@click.option('-m', '--models', multiple=True, required=True,
              help='Models to use as coders (repeat for multiple)')
@click.option('--sample', 'sample_size', type=int, default=None,
              help='Sample N rows (default: all)')
@click.option('--stratify-by', default=None,
              help='Stratify sample by this enrichment field')
@click.option('--seed', type=int, default=None,
              help='Random seed for reproducibility')
@click.option('--config', help='Path to config YAML (auto-detects .doctrail/config.yml)')
@click.option('--db-path', help='Database path override')
@click.option('--overwrite', is_flag=True, help='Re-run models that already have codings')
@click.option('--skip-cost-check', is_flag=True, help='Skip cost confirmation')
@click.option('--project', help='Tag enrichments with a project name')
@click.option('--verbose', is_flag=True, help='Verbose logging')
def icr(enrichment_name: str, models: tuple, sample_size: Optional[int],
         stratify_by: Optional[str], seed: Optional[int], config: Optional[str],
         db_path: Optional[str], overwrite: bool, skip_cost_check: bool,
         project: Optional[str], verbose: bool):
    """Run intercoder reliability: enrich sampled rows with multiple models.

    Example:
        doctrail icr threat_coding -m openrouter/google/gemini-2.5-flash -m gpt-4o-mini --sample 50 --seed 42
    """
    setup_logging(verbose)

    # Resolve config
    doctrail_dir = _get_doctrail_dir()
    doctrail_config = _get_config_path()

    if config:
        config_path = config
    elif doctrail_config.exists():
        config_path = str(doctrail_config)
        click.echo(f"Using config: {doctrail_config}")
    else:
        raise click.UsageError(
            "No config found. Use --config or run 'doctrail init' first."
        )

    # Load and merge enrichment configs with import support.
    try:
        config_data = load_config(config_path)
    except Exception as e:
        raise click.UsageError(f"Failed to load config: {e}") from e

    # Check if enrichment exists in config; if not, check .doctrail/enrichments/
    enrichments_dir = doctrail_dir / "enrichments"
    found = any(
        isinstance(e, dict) and e.get('name') == enrichment_name
        for e in config_data.get('enrichments', [])
    )

    if not found and enrichments_dir.exists():
        enrichment_file = enrichments_dir / f"{enrichment_name}.yml"
        if enrichment_file.exists():
            with open(enrichment_file, 'r') as f:
                enrichment_config = yaml.safe_load(f)
            config_data.setdefault('enrichments', []).append(enrichment_config)
            found = True

    if not found:
        raise click.UsageError(f"Enrichment '{enrichment_name}' not found in config or .doctrail/enrichments/")

    # Write merged config to temp file.
    merged_config_path = _write_merged_config(config_data, base_config_path=config_path)

    try:
        from ..core import run_icr, EnrichmentError, ConfigurationError, DatabaseError

        effective_project = project or config_data.get('project_name')

        result = asyncio.run(run_icr(
            config_path=merged_config_path,
            enrichment_name=enrichment_name,
            models=list(models),
            sample_size=sample_size,
            stratify_by=stratify_by,
            seed=seed,
            db_path=db_path,
            overwrite=overwrite,
            skip_cost_check=skip_cost_check,
            verbose=verbose,
            project=effective_project,
        ))

        if result.get('status') == 'success':
            click.echo(f"\nICR completed")
            click.echo(f"  Sample ID: {result['sample_id']}")
            click.echo(f"  Sample size: {result['sample_size']}")
            click.echo(f"  Models: {', '.join(result['models'])}")
            click.echo(f"  Total processed: {result['total_processed']}")
            click.echo(f"\nRun report:")
            click.echo(f"  doctrail icr-report --db-path <db> --field <field_name> --sample-id {result['sample_id']}")
        else:
            click.echo(f"\nICR completed with errors:", err=True)
            for error in result.get('errors', []):
                click.echo(f"  - {error}", err=True)
            raise click.exceptions.Exit(1)

    except (EnrichmentError, ConfigurationError, DatabaseError) as e:
        _exit_error(f"\nError: {e}")
    except KeyboardInterrupt:
        click.echo("\nInterrupted.", err=True)
        raise click.exceptions.Exit(1)
    except Exception as e:
        click.echo(f"\nUnexpected error: {e}", err=True)
        if verbose:
            import traceback
            traceback.print_exc()
        raise click.exceptions.Exit(1)
    finally:
        try:
            os.unlink(merged_config_path)
        except Exception:
            pass


@cli.command('icr-report')
@click.option('--db-path', help='Database path (defaults to .doctrail/config.yml)')
@click.option('--field', 'field_name', required=True, help='Field name to analyse')
@click.option('--enrichment-name', default=None, help='Filter by enrichment name')
@click.option('-m', '--models', multiple=True, help='Specific models to compare (repeat)')
@click.option('--sample-id', default=None, help='Filter to specific ICR sample')
@click.option('--level', 'level_of_measurement',
              type=click.Choice(['nominal', 'ordinal', 'interval']),
              default=None, help='Measurement level (auto-detected if omitted)')
@click.option('-o', '--output', 'output_path', default=None,
              help='Write CSV coding matrix to this path')
@click.option('--verbose', is_flag=True, help='Verbose logging')
def icr_report(db_path: Optional[str], field_name: str, enrichment_name: Optional[str],
               models: tuple, sample_id: Optional[str],
               level_of_measurement: Optional[str], output_path: Optional[str],
               verbose: bool):
    """Compute intercoder reliability statistics from enrichment codings.

    Example:
        doctrail icr-report --db-path out/db.db --field hostility_level
    """
    setup_logging(verbose)

    try:
        from ..core import run_icr_report, EnrichmentError, DatabaseError

        # Resolve db_path and key_column from config if available
        key_column = 'sha1'
        doctrail_dir = _get_doctrail_dir()
        doctrail_config = _get_config_path()
        if doctrail_config.exists():
            try:
                with open(doctrail_config, 'r') as f:
                    cfg = yaml.safe_load(f)
                if cfg and 'key_column' in cfg:
                    key_column = cfg['key_column']
                if not db_path and cfg and cfg.get('database'):
                    db_path = cfg['database']
            except Exception:
                pass
        if not db_path:
            raise click.UsageError("No database path found. Use --db-path or run inside a doctrail project.")

        result = asyncio.run(run_icr_report(
            db_path=db_path,
            field_name=field_name,
            enrichment_name=enrichment_name,
            models=list(models) if models else None,
            sample_id=sample_id,
            level_of_measurement=level_of_measurement,
            key_column=key_column,
            verbose=verbose,
        ))

        # Display results
        click.echo(f"\nICR report: {result['field_name']}")
        click.echo(f"  Measurement level: {result['level_of_measurement']}")
        click.echo(f"  Items (complete): {result['n_items']} / {result['n_items_total']} total")
        click.echo(f"  Models: {', '.join(result['models'])}")

        # Krippendorff's alpha
        click.echo(f"\nKrippendorff's alpha: ", nl=False)
        if result['alpha'] is not None:
            alpha = result['alpha']
            # Colour-code the interpretation
            if alpha >= 0.80:
                label = "good"
            elif alpha >= 0.67:
                label = "tentative"
            else:
                label = "low"
            click.echo(f"{alpha:.4f}  ({label})")
        elif result.get('alpha_error'):
            click.echo(f"error — {result['alpha_error']}")

        # Pairwise
        if result['pairwise']:
            click.echo(f"\nPairwise comparisons:")
            for pair, stats in result['pairwise'].items():
                line = f"  {pair}: agreement={stats['agreement_rate']:.1%} ({stats['agree']}/{stats['total']})"
                if 'kappa' in stats:
                    line += f", kappa={stats['kappa']:.4f}"
                if 'kappa_error' in stats:
                    line += f", kappa error: {stats['kappa_error']}"
                click.echo(line)

        # Distributions
        if result['distributions']:
            click.echo(f"\nPer-model distributions:")
            for model, dist in result['distributions'].items():
                dist_str = ", ".join(f"{k}: {v}" for k, v in dist.items())
                click.echo(f"  {model}: {dist_str}")

        # CSV output
        if output_path:
            _write_icr_csv(result, output_path, db_path, field_name, enrichment_name, sample_id, key_column=key_column)
            click.echo(f"\nCoding matrix written to: {output_path}")

    except (EnrichmentError, DatabaseError) as e:
        _exit_error(f"\nError: {e}")
    except Exception as e:
        click.echo(f"\nUnexpected error: {e}", err=True)
        if verbose:
            import traceback
            traceback.print_exc()
        raise click.exceptions.Exit(1)


def _write_icr_csv(result, output_path, db_path, field_name, enrichment_name, sample_id, key_column='sha1'):
    """Write the coding matrix to CSV for external analysis."""
    import csv
    from ..db_operations import get_icr_codings

    key_filter = None
    if sample_id:
        from ..db_operations import get_db_connection
        import sqlite3
        with get_db_connection(db_path) as conn:
            cursor = conn.cursor()
            # Handle both old (sha1) and new (key_value) column names
            cursor.execute(f"PRAGMA table_info({ICR_SAMPLES_TABLE})")
            columns = [row[1] for row in cursor.fetchall()]
            key_col_name = 'key_value' if 'key_value' in columns else 'sha1'
            cursor.execute(
                f"SELECT DISTINCT {key_col_name} FROM {ICR_SAMPLES_TABLE} WHERE sample_id = ?",
                (sample_id,)
            )
            key_filter = [row[0] for row in cursor.fetchall()]

    codings = get_icr_codings(
        db_path=db_path,
        field_name=field_name,
        enrichment_name=enrichment_name,
        sha1s=key_filter,
        key_column=key_column,
    )

    # Build matrix
    from collections import defaultdict
    items = defaultdict(dict)
    for c in codings:
        items[c['key_value']][c['model']] = c['value']

    models = result['models']
    with open(output_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([key_column] + models)
        for key_val in sorted(items.keys()):
            row = [key_val] + [items[key_val].get(m, '') for m in models]
            writer.writerow(row)
