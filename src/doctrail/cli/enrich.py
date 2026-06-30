"""
Enrich commands - LLM enrichment processing.
"""
import os
import sys
from pathlib import Path
from typing import Optional
from datetime import datetime

import click
import yaml
import asyncio

from .main import cli, create_enrichment_interactively
from .utils import (
    PRESET_ENRICHMENTS,
    _resolve_preset_alias,
    _get_doctrail_dir,
    _get_config_path,
    _load_project_config,
    _write_merged_config,
    _exit_error,
    setup_logging,
    get_db_connection,
    load_config,
)

BATCH_ENDPOINT_LABELS = {
    "openai": "/v1/batches -> /v1/chat/completions",
    "anthropic": "/v1/messages/batches -> /v1/messages",
    "gemini": "File API -> /v1beta/models/{model}:batchGenerateContent",
}


def _echo_run_artifacts(result: dict) -> None:
    """Print run artifacts from a completed enrichment result."""
    if result.get('run_artifacts'):
        for artifact in result['run_artifacts']:
            run_label = artifact['enrichment_name']
            if artifact.get('model'):
                run_label = f"{run_label} [{artifact['model']}]"
            click.echo(f"\nRun: {artifact['run_id']}")
            click.echo(f"   Task: {run_label}")
            if int(artifact.get('error_count', 0) or 0) > 0:
                click.echo(
                    f"   Status: completed with errors "
                    f"({int(artifact.get('success_count', 0) or 0)} succeeded, "
                    f"{int(artifact.get('error_count', 0) or 0)} errored)"
                )
            if artifact.get('query_hash'):
                click.echo(f"   Query hash: {artifact['query_hash'][:8]}")
            if artifact.get('view_name'):
                click.echo(f"   Run view: {artifact['view_name']}")
                click.echo(f"     SELECT * FROM {artifact['view_name']} LIMIT 10")
            if artifact.get('final_view'):
                click.echo(f"   Final view: {artifact['final_view']}")
                click.echo(f"     SELECT * FROM {artifact['final_view']} LIMIT 10")
            if artifact.get('summary_view'):
                click.echo(f"   Summary view: {artifact['summary_view']}")
                click.echo(f"     SELECT * FROM {artifact['summary_view']}")
    elif result.get('run_views'):
        for ename, view_info in result['run_views'].items():
            vname = view_info['view_name']
            click.echo(f"\nView: {vname}")
            click.echo(f"   SELECT * FROM {vname} LIMIT 10")


def _available_enrichment_names(doctrail_dir: Path, config_data: dict) -> list[str]:
    """Return enrichment names visible to the current project."""
    names = []
    enrichments_dir = doctrail_dir / "enrichments"
    if enrichments_dir.exists():
        names.extend(path.stem for path in sorted(enrichments_dir.glob("*.yml")))
    for enrichment in config_data.get("enrichments", []) or []:
        if isinstance(enrichment, dict) and enrichment.get("name"):
            names.append(str(enrichment["name"]))
    names.extend(PRESET_ENRICHMENTS.keys())

    seen = set()
    return [name for name in names if not (name in seen or seen.add(name))]


def _pick_enrichment_name(available: list[str]) -> Optional[str]:
    """Pick an enrichment interactively, or return None outside a terminal."""
    if not sys.stdin.isatty():
        return None
    try:
        import questionary
    except ImportError:
        return None

    choices = [questionary.Choice(name, value=name) for name in available]
    choices.append(questionary.Choice("+ new enrichment...", value="__new__"))
    choices.append(questionary.Choice("Cancel", value="__cancel__"))

    selected = questionary.select(
        "Which enrichment do you want to run?",
        choices=choices,
    ).ask()
    if selected in (None, "__cancel__"):
        return None
    return selected


@cli.command()
@click.argument('enrichment_names', nargs=-1)  # Positional: doctrail enrich language summarize
@click.option('--config', help='Path to config YAML (auto-detects .doctrail/config.yml)')
@click.option('--enrichments', multiple=True, help='(Legacy) Enrichment task names')
@click.option('--limit', type=int, help='Limit number of rows to process')
@click.option('--overwrite', is_flag=True, help='Overwrite existing data in output columns')
@click.option('--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--log-updates', is_flag=True, help='Log updates to a file')
@click.option('--model', help='Override the default model for all enrichments')
@click.option('--db-path', help='Override the database path from config')
@click.option('--output-db', help='Write enrichments to this database instead of the source database')
@click.option('--batch-size', type=int, help='Override batch size for processing')
@click.option('--rowid', type=int, help='Process only a specific row by rowid')
@click.option('--sha1', help='Process only a specific row by sha1 hash')
@click.option('--truncate', is_flag=True, help='Truncate long inputs to fit model context window instead of failing')
@click.option('--skip-cost-check', is_flag=True, help='Skip cost estimation and confirmation')
@click.option('--cost-threshold', type=float, default=5.0, help='Cost threshold for confirmation prompt (default: $5.00)')
@click.option('--where', 'where_clause', help='Filter the enrichment query with an outer SQL WHERE predicate')
@click.option('--query', 'override_query', help='Replace the SQL query from config entirely')
@click.option('--project', help='Tag enrichments with a project name for filtering (e.g., mock_compliance)')
@click.option('--dry-run', is_flag=True, help='Preview without calling LLM: show row counts, schema, and sample input')
@click.option(
    '--dedupe-scope',
    type=click.Choice(['query', 'prompt', 'enrichment', 'name']),
    default=None,
    help='Append-mode dedupe scope. Overrides per-enrichment dedupe_scope.',
)
@click.option(
    '--materialize-inputs/--no-materialize-inputs',
    default=True,
    help='Persist the exact input rowset for each run',
)
@click.option(
    '--execution-mode',
    type=click.Choice(['sync', 'batch', 'openai-batch']),
    default='sync',
    show_default=True,
    help=(
        "How to execute the enrichment work. "
        "batch maps direct OpenAI models to /v1/batches -> /v1/chat/completions, "
        "direct Claude models to /v1/messages/batches -> /v1/messages, "
        "and direct Gemini models to File API upload -> /v1beta/models/{model}:batchGenerateContent. "
        "openai-batch is accepted as a legacy alias."
    ),
)
@click.option('--allow-column-collision', is_flag=True,
              help='Allow enrichment field names that match source table columns')
@click.pass_context
def enrich(ctx, enrichment_names: tuple, config: Optional[str], enrichments: tuple,
        limit: Optional[int], overwrite: bool,
        verbose: bool, log_updates: bool, model: Optional[str],
        db_path: Optional[str], output_db: Optional[str], batch_size: Optional[int],
        rowid: Optional[int], sha1: Optional[str], truncate: bool, skip_cost_check: bool,
        cost_threshold: float, where_clause: Optional[str], override_query: Optional[str], project: Optional[str],
        dry_run: bool, dedupe_scope: str, materialize_inputs: bool,
        execution_mode: str, allow_column_collision: bool):
    """
    Enrich database content using LLM processing.

    Run enrichments by name:
        doctrail enrich language
        doctrail enrich language summarize

    In a doctrail project (.doctrail/ folder), enrichments are loaded from
    .doctrail/enrichments/<name>.yml and merged with .doctrail/config.yml.
    Model outputs are written to normalized tables; use `doctrail view create`
    or `doctrail view pivot` to inspect them in a wide, human-readable form.
    """
    # Combine positional args with legacy --enrichments flag
    all_enrichment_names = list(enrichment_names) + list(enrichments)

    # Process comma-separated values
    requested_enrichments = []
    for enrichment_arg in all_enrichment_names:
        if ',' in enrichment_arg:
            requested_enrichments.extend([e.strip() for e in enrichment_arg.split(',')])
        else:
            requested_enrichments.extend(enrichment_arg.split())

    # Remove duplicates while preserving order
    seen = set()
    requested_enrichments = [x for x in requested_enrichments if not (x in seen or seen.add(x))]

    # Auto-detect config from .doctrail/config.yml
    doctrail_dir = _get_doctrail_dir()
    doctrail_config = _get_config_path()

    if config:
        config_path = config
    elif doctrail_config.exists():
        config_path = str(doctrail_config)
        click.echo(f"Using config: {doctrail_config}")
    else:
        raise click.UsageError(
            "No config found. Either:\n"
            "  - Run 'doctrail init' first, or\n"
            "  - Use --config to specify a config file"
        )

    # Load config with the import-aware loader used by the core API.
    try:
        config_data = load_config(config_path)
    except FileNotFoundError as exc:
        raise click.UsageError(str(exc)) from exc
    except Exception as exc:
        raise click.UsageError(f"Failed to load config: {exc}") from exc

    # Use config verbose setting if CLI flag wasn't explicitly set
    current_ctx = click.get_current_context()
    verbose_was_passed = 'verbose' in current_ctx.params and current_ctx.params['verbose']

    if not verbose_was_passed and config_data.get('verbose', False):
        verbose = True

    setup_logging(verbose)

    if overwrite:
        click.echo("Overwrite mode enabled.")

    # If no enrichments specified, pick one interactively when possible.
    if not requested_enrichments:
        available = _available_enrichment_names(doctrail_dir, config_data)
        selected_name = _pick_enrichment_name(available)
        if selected_name == "__new__":
            selected_name, file_path = create_enrichment_interactively()
            click.echo(f"\nCreated: {file_path}")
        if selected_name:
            requested_enrichments = [selected_name]
        elif available:
            click.echo("\nAvailable enrichments:")
            for name in available:
                click.echo(f"  {name}")
            click.echo(f"\nRun: doctrail enrich <name>")
            return
        raise click.UsageError("No enrichment specified. Usage: doctrail enrich <name>")

    # Load enrichment configs from .doctrail/enrichments/<name>.yml
    # and merge with main config
    enrichments_dir = doctrail_dir / "enrichments"
    loaded_enrichments = []

    # Resolve aliases first (e.g., 'summarise' -> 'summarize')
    resolved_enrichments = []
    for name in requested_enrichments:
        resolved = _resolve_preset_alias(name)
        if resolved != name:
            click.echo(f"  ({name} → {resolved})")
        resolved_enrichments.append(resolved)
    requested_enrichments = resolved_enrichments

    for name in requested_enrichments:
        enrichment_file = enrichments_dir / f"{name}.yml"
        if enrichment_file.exists():
            # Found in project folder
            with open(enrichment_file, 'r') as f:
                enrichment_config = yaml.safe_load(f)
                loaded_enrichments.append(enrichment_config)
                click.echo(f"✓ Loaded enrichment: {name}")
        else:
            # Check if it's defined in the main config
            config_enrichments = config_data.get('enrichments', [])
            found = False
            for e in config_enrichments:
                if isinstance(e, dict) and e.get('name') == name:
                    loaded_enrichments.append(e)
                    found = True
                    break

            # Fallback: check package presets (with aliases)
            preset_name = _resolve_preset_alias(name)
            if not found and preset_name in PRESET_ENRICHMENTS:
                preset = PRESET_ENRICHMENTS[preset_name]
                enrichment_config = yaml.safe_load(preset['content'])
                loaded_enrichments.append(enrichment_config)

                # Copy preset to project folder so user can edit it
                enrichments_dir.mkdir(exist_ok=True)
                dest_file = enrichments_dir / f"{preset_name}.yml"
                dest_file.write_text(preset['content'])
                click.echo(f"✓ Copied preset '{preset_name}' to {dest_file}")
                found = True

            if not found:
                available_presets = list(PRESET_ENRICHMENTS.keys())
                raise click.UsageError(
                    f"Enrichment '{name}' not found.\n"
                    f"  Looked in: {enrichment_file}\n"
                    f"  And in config: {config_path}\n"
                    f"\n  Available presets: {', '.join(available_presets)}"
                )

    # Merge loaded enrichments into config for the core API
    config_data['enrichments'] = loaded_enrichments

    # Write merged config to a temp file for the core API.
    merged_config_path = _write_merged_config(config_data, base_config_path=config_path)

    try:
        from ..core import run_enrichment, EnrichmentError, ConfigurationError, DatabaseError

        # Use project name from config if not specified via --project flag
        effective_project = project or config_data.get('project_name')

        result = asyncio.run(run_enrichment(
            config_path=merged_config_path,
            enrichments=requested_enrichments,
            db_path=db_path,
            output_db_path=output_db,
            model=model,
            limit=limit,
            rowid=rowid,
            sha1=sha1,
            overwrite=overwrite,
            batch_size=batch_size,
            truncate=truncate,
            skip_cost_check=skip_cost_check,
            cost_threshold=cost_threshold,
            verbose=verbose,
            where_clause=where_clause,
            override_query=override_query,
            project=effective_project,
            dry_run=dry_run,
            dedupe_scope=dedupe_scope,
            materialize_inputs=materialize_inputs,
            execution_mode=execution_mode,
            allow_column_collision=allow_column_collision,
        ))

        # Handle dry-run output
        if dry_run:
            for info in result.get('results', []):
                if not info.get('dry_run'):
                    continue
                click.echo(f"\nDRY RUN — {info['enrichment']}")
                click.echo(f"  Database:    {info['db_path']}")
                click.echo(f"  Key column:  {info['key_column']}")
                click.echo(f"  Model(s):    {', '.join(info['models'])}")
                click.echo(f"\n  Query results:")
                click.echo(f"    Total rows:       {info['total_rows']}")
                click.echo(f"    Already done:     {info['already_processed']}")
                click.echo(f"    Would process:    {info['would_process']}")
                click.echo(f"    Min input chars:  {info.get('min_input_chars', 1)}")
                if info['schema_fields']:
                    click.echo(f"\n  Schema fields:")
                    for field in info['schema_fields']:
                        click.echo(f"    • {field}")
                if info['prompt_preview']:
                    click.echo(f"\n  Prompt (first 200 chars):")
                    click.echo(f"    {info['prompt_preview']}")
                if info['sample_input']:
                    click.echo(f"\n  Sample input (first row):")
                    for col, val in info['sample_input'].items():
                        click.echo(f"    {col}: {val}")
            return

        # Display results
        success_count = int(result.get('success_count', 0) or 0)
        error_count = int(result.get('error_count', 0) or 0)
        enrichment_count = len(result['enrichments_run'])
        if result['status'] == 'success':
            click.echo(f"\nSuccessfully completed {enrichment_count} enrichment(s)")
            click.echo(f"Total results: {result['total_processed']}")

            _echo_run_artifacts(result)

            if result.get('multi_model_view_notice'):
                click.echo(result['multi_model_view_notice'])

            # Show project view if created
            if result.get('project_view'):
                view_name = result['project_view']
                click.echo(f"Project view: {view_name}")
                click.echo(f"   SELECT * FROM {view_name} LIMIT 10")

            # Execute custom SQL views from .doctrail/views/*.sql
            views_dir = doctrail_dir / "views"
            if views_dir.exists() and db_path:
                custom_views = list(views_dir.glob("*.sql"))
                if custom_views:
                    click.echo(f"Refreshing {len(custom_views)} custom view(s)...")
                    for sql_file in custom_views:
                        try:
                            sql = sql_file.read_text()
                            with get_db_connection(db_path) as conn:
                                # Execute all statements in the file
                                conn.executescript(sql)
                            click.echo(f"   ✓ {sql_file.stem}")
                        except Exception as e:
                            click.echo(f"   ✗ {sql_file.stem}: {e}", err=True)

            if log_updates:
                import json
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                log_file = f"updates_{timestamp}.json"
                with open(log_file, 'w') as f:
                    json.dump(result['results'], f, indent=2)
                click.echo(f"Updates logged to {log_file}")
        elif result['status'] == 'submitted':
            click.echo(f"\nSubmitted {len(result['enrichments_run'])} batch enrichment(s)")
            for artifact in result.get('run_artifacts', []):
                click.echo(f"\nRun: {artifact['run_id']}")
                click.echo(f"   Task: {artifact['enrichment_name']} [{artifact['model']}]")
                click.echo(f"   Query hash: {artifact['query_hash'][:8]}")
                for batch_job in artifact.get('batch_jobs', []):
                    provider = batch_job.get('provider', 'openai')
                    endpoint_label = BATCH_ENDPOINT_LABELS.get(provider, batch_job.get('endpoint'))
                    click.echo(
                        f"   Batch ({provider}): {batch_job.get('provider_batch_id')} "
                        f"({batch_job.get('request_count')} requests, status={batch_job.get('status')}, "
                        f"endpoint={endpoint_label})"
                    )
            click.echo("\nPoll with: doctrail batch poll --run-id <run_id>")
            click.echo("Watch with: doctrail batch watch --run-id <run_id>")
        else:
            if error_count > 0:
                if success_count == 0:
                    click.echo(
                        f"\nFailed: {success_count} succeeded, {error_count} errored "
                        f"across {enrichment_count} enrichment(s)",
                        err=True,
                    )
                else:
                    click.echo(
                        f"\nCompleted with errors: {success_count} succeeded, {error_count} errored "
                        f"across {enrichment_count} enrichment(s)",
                        err=True,
                    )
                _echo_run_artifacts(result)
            else:
                click.echo("\nEnrichment completed with errors:", err=True)
            for error in result.get('errors', []):
                click.echo(f"  - {error}", err=True)
            raise click.exceptions.Exit(1)

    except (EnrichmentError, ConfigurationError, DatabaseError) as e:
        _exit_error(f"\nError: {e}")
    except click.exceptions.Exit:
        raise
    except KeyboardInterrupt:
        click.echo("\nEnrichment interrupted by user.", err=True)
        click.echo("Run the same command again to continue where you left off.", err=True)
        raise click.exceptions.Exit(1)
    except Exception as e:
        click.echo(f"\nUnexpected error: {e}", err=True)
        if verbose:
            import traceback
            traceback.print_exc()
        raise click.exceptions.Exit(1)
    finally:
        # Clean up temp config file
        if 'merged_config_path' in locals():
            try:
                os.unlink(merged_config_path)
            except Exception:
                pass


cli.add_command(enrich, "run")


@cli.command('list-enrichments')
@click.option('--config', required=True, help='Path to the configuration YAML file')
@click.pass_context
def list_enrichments(ctx, config: str):
    """List all available enrichments from a configuration file."""

    try:
        # Call core API
        from ..core import list_enrichments as core_list_enrichments, ConfigurationError

        result = asyncio.run(core_list_enrichments(config_path=config))

        # Display results
        if result['status'] == 'success':
            click.echo(f"\nFound {result['count']} enrichment(s) in {config}\n")

            for enrichment in result['enrichments']:
                click.echo(f"  • {enrichment['name']}")
                click.echo(f"    Description: {enrichment['description']}")
                click.echo(f"    Model: {enrichment['model']}")
                click.echo("    Storage: normalized enrichments + derived views")

                if enrichment['output_column']:
                    click.echo(f"    Field alias: '{enrichment['output_column']}'")
                elif enrichment['output_table']:
                    click.echo(f"    Legacy output_table hint: '{enrichment['output_table']}'")

                click.echo()

    except ConfigurationError as e:
        _exit_error(f"\nError: {e}")
    except Exception as e:
        _exit_error(f"\nUnexpected error: {e}")


def _resolve_batch_db_path(db_path: Optional[str]) -> str:
    """Resolve a database path from CLI input or project config."""
    if db_path:
        return str(Path(db_path).expanduser())
    config_data = _load_project_config()
    return str(Path(config_data.get('database', './out/documents.db')).expanduser())


@cli.group('batch')
def batch():
    """Manage submitted provider batch enrichment runs."""


@batch.command('poll')
@click.option('--db-path', help='Path to SQLite database')
@click.option('--run-id', help='Poll only one run ID')
@click.pass_context
def batch_poll(ctx, db_path: Optional[str], run_id: Optional[str]):
    """Poll batch jobs once and reconcile any completed outputs."""
    from ..core import poll_batch_runs, EnrichmentError, DatabaseError

    resolved_db_path = _resolve_batch_db_path(db_path)
    try:
        result = asyncio.run(poll_batch_runs(
            db_path=resolved_db_path,
            run_id=run_id,
            watch=False,
        ))
    except (EnrichmentError, DatabaseError) as e:
        _exit_error(f"Error: {e}")

    for summary in result.get('run_summaries', []):
        click.echo(
            f"{summary['run_id']}: status={summary['status']} "
            f"processed={summary['processed_rows']} errors={summary['error_count']}"
        )
    if result.get('pending_jobs'):
        click.echo(f"Pending batch jobs: {result['pending_jobs']}")
    if result.get('errors'):
        for error in result['errors']:
            click.echo(f"Error: {error}", err=True)
        raise click.exceptions.Exit(1)
    batch_error_count = int(result.get('error_count', 0) or 0)
    if batch_error_count:
        batch_success_count = int(result.get('success_count', 0) or 0)
        click.echo(
            f"Batch completed with errors: {batch_success_count} succeeded, "
            f"{batch_error_count} errored across {len(result.get('run_summaries', []))} run(s)",
            err=True,
        )
        raise click.exceptions.Exit(1)


@batch.command('watch')
@click.option('--db-path', help='Path to SQLite database')
@click.option('--run-id', required=True, help='Run ID to watch')
@click.option('--interval', 'interval_seconds', type=float, default=5.0, show_default=True, help='Polling interval in seconds')
@click.pass_context
def batch_watch(ctx, db_path: Optional[str], run_id: str, interval_seconds: float):
    """Poll until a batch-backed run is fully reconciled."""
    from ..core import poll_batch_runs, EnrichmentError, DatabaseError

    resolved_db_path = _resolve_batch_db_path(db_path)
    try:
        result = asyncio.run(poll_batch_runs(
            db_path=resolved_db_path,
            run_id=run_id,
            watch=True,
            interval_seconds=interval_seconds,
        ))
    except (EnrichmentError, DatabaseError) as e:
        _exit_error(f"Error: {e}")

    for summary in result.get('run_summaries', []):
        click.echo(
            f"{summary['run_id']}: status={summary['status']} "
            f"processed={summary['processed_rows']} errors={summary['error_count']}"
        )
    if result.get('errors'):
        for error in result['errors']:
            click.echo(f"Error: {error}", err=True)
        raise click.exceptions.Exit(1)
    batch_error_count = int(result.get('error_count', 0) or 0)
    if batch_error_count:
        batch_success_count = int(result.get('success_count', 0) or 0)
        click.echo(
            f"Batch completed with errors: {batch_success_count} succeeded, "
            f"{batch_error_count} errored across {len(result.get('run_summaries', []))} run(s)",
            err=True,
        )
        raise click.exceptions.Exit(1)


@batch.command('cancel')
@click.option('--db-path', help='Path to SQLite database')
@click.option('--run-id', required=True, help='Run ID to cancel')
@click.pass_context
def batch_cancel(ctx, db_path: Optional[str], run_id: str):
    """Cancel all active batch shards for a run."""
    from ..core import cancel_batch_runs, EnrichmentError, DatabaseError

    resolved_db_path = _resolve_batch_db_path(db_path)
    try:
        result = asyncio.run(cancel_batch_runs(
            db_path=resolved_db_path,
            run_id=run_id,
        ))
    except (EnrichmentError, DatabaseError) as e:
        _exit_error(f"Error: {e}")

    click.echo(f"Run {result['run_id']}: requested cancellation for {result['cancelled_jobs']} batch job(s)")
