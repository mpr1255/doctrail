"""
Ingest command - document ingestion from directories, Zotero, or plugins.
"""
import os
from pathlib import Path
from typing import Optional

import click
import yaml
import asyncio

from .main import cli
from .utils import (
    _get_config_path,
    setup_logging, verify_dependencies
)
from ..ingest.document_processor import format_supported_extensions_for_help

DEFAULT_INGEST_DB_NAME = "doctrail.db"
SUPPORTED_FORMATS_HELP = format_supported_extensions_for_help()


def _looks_like_database_file(path: Path) -> bool:
    """Return True when a path should be treated as an explicit database file."""
    if path.exists():
        return path.is_file()
    return bool(path.suffix)


def _resolve_ingest_db_path(db_path: Optional[str], config_data: Optional[dict]) -> str:
    """
    Resolve the ingest database target using three tiers:
    1. Explicit file path -> use it as-is
    2. Directory path -> place doctrail.db inside it
    3. No --db-path -> use config database or ./doctrail.db
    """
    if db_path:
        requested = Path(db_path).expanduser()
        if _looks_like_database_file(requested):
            return str(requested)
        return str(requested / DEFAULT_INGEST_DB_NAME)

    if config_data and config_data.get('database'):
        return str(Path(config_data['database']).expanduser())

    return str(Path.cwd() / DEFAULT_INGEST_DB_NAME)


def get_zotero_config(config_data: Optional[dict]) -> dict:
    """Get Zotero configuration from config, environment, or raise error."""
    api_key = None
    library_id = None
    library_type = 'user'

    # Check config first
    if config_data and 'zotero' in config_data:
        zotero_cfg = config_data['zotero']
        api_key = zotero_cfg.get('api_key')
        library_id = zotero_cfg.get('library_id') or zotero_cfg.get('user_id')
        library_type = zotero_cfg.get('library_type', 'user')

    # Environment variables override config
    api_key = os.environ.get('ZOTERO_API_KEY', api_key)
    library_id = os.environ.get('ZOTERO_USER_ID', library_id) or os.environ.get('ZOTERO_LIBRARY_ID', library_id)

    if not api_key:
        raise click.UsageError(
            "Zotero API key not found. Set ZOTERO_API_KEY environment variable, "
            "or add to config under 'zotero.api_key'"
        )
    if not library_id:
        raise click.UsageError(
            "Zotero user/library ID not found. Set ZOTERO_USER_ID environment variable, "
            "or add to config under 'zotero.library_id'"
        )

    return {
        'api_key': api_key,
        'library_id': library_id,
        'library_type': library_type
    }


@cli.command(
    help=(
        "Ingest documents from local directories, Zotero, or plugins.\n\n"
        f"Supported local file types: {SUPPORTED_FORMATS_HELP}.\n\n"
        "Examples:\n"
        "    doctrail ingest --input-dir ./docs --db-path ./data.db\n"
        "    doctrail ingest --zotero --collection \"Papers\" --db-path ./lit.db\n"
        "    doctrail ingest --plugin zotero --collection \"My Research\""
    )
)
@click.option('--config', help='Path to the configuration YAML file')
@click.option('--db-path', help='SQLite database file, or a directory to use/create doctrail.db in')
@click.option('--table', default="documents", help='Table name for documents')
@click.option('--verbose', is_flag=True, help='Enable detailed logging')
@click.option('--input-dir', multiple=True, help='Input directory (can repeat)')
@click.option('--force', is_flag=True, help='Force ingest even if schema mismatch')
@click.option('--overwrite', is_flag=True, help='Overwrite existing documents')
@click.option(
    '--skip-existing',
    is_flag=True,
    help='Fast resume: skip exact database filepaths before hashing (ZIPs are still checked)',
)
@click.option('--limit', type=int, help='Limit files to process')
@click.option('--include-pattern', help='Only process matching files')
@click.option('--exclude-pattern', help='Skip matching files')
@click.option('--workers', type=click.IntRange(1, None), help='Number of extraction worker threads')
@click.option('--pdf-engine', type=click.Choice(['auto', 'pymupdf', 'pdftotext', 'mutool', 'mac-ocr']), help='PDF extraction strategy')
@click.option('--ocr-engine', type=click.Choice(['auto', 'textra', 'ocrmypdf', 'mac-ocr']), help='OCR backend when OCR is needed')
@click.option('--extractor', type=click.Choice(['auto', 'rust', 'python']), default='auto', help='Extraction engine: auto/rust requires native; python is the explicit backup')
@click.option('--readability', is_flag=True, help='Use readability for HTML')
@click.option('--html-extractor', type=click.Choice(['default', 'smart']), default='default')
@click.option('--skip-garbage-check', is_flag=True, help='Skip garbage detection')
@click.option('--yes', '-y', is_flag=True, help='Skip prompts')
@click.option('--fulltext', is_flag=True, help='Create FTS index')
@click.option('--manifest', help='Path to manifest.json')
@click.option('--zotero', is_flag=True, help='Zotero mode')
@click.option('--collection', help='Zotero collection name')
@click.option('--plugin', help='Plugin name')
@click.option('--plugin-dir', help='Custom plugins directory')
@click.option('--cache-db', help='[doi_connector] Cache database')
@click.option('--project', help='[doi_connector] Project name')
@click.option('--base-path', help='[doi_connector] Base path')
@click.option('--api-key', help='[zotero] API key')
@click.option('--user-id', help='[zotero] User ID')
@click.option('--zotero-dir', help='[zotero] Data directory')
@click.pass_context
def ingest(
    ctx,
    config: Optional[str],
    db_path: Optional[str],
    table: str,
    verbose: bool,
    input_dir: tuple,
    force: bool,
    overwrite: bool,
    skip_existing: bool,
    limit: Optional[int],
    include_pattern: Optional[str],
    exclude_pattern: Optional[str],
    workers: Optional[int],
    pdf_engine: Optional[str],
    ocr_engine: Optional[str],
    extractor: str,
    readability: bool,
    html_extractor: str,
    skip_garbage_check: bool,
    yes: bool,
    fulltext: bool,
    manifest: Optional[str],
    zotero: bool,
    collection: Optional[str],
    plugin: Optional[str],
    plugin_dir: Optional[str],
    cache_db: Optional[str],
    project: Optional[str],
    base_path: Optional[str],
    api_key: Optional[str],
    user_id: Optional[str],
    zotero_dir: Optional[str]
):
    """
    Ingest documents from local directories, Zotero, or plugins.

    Examples:
        doctrail ingest --input-dir ./docs --db-path ./data.db
        doctrail ingest --zotero --collection "Papers" --db-path ./lit.db
        doctrail ingest --plugin zotero --collection "My Research"
    """
    skip_requirements = ctx.obj.get('skip_requirements', False) if ctx.obj else False
    if not verify_dependencies(skip_requirements):
        ctx.exit(1)

    setup_logging(verbose)

    # Auto-detect config
    config_data = None
    doctrail_config = _get_config_path()

    if config:
        config_to_load = config
    elif doctrail_config.exists():
        config_to_load = str(doctrail_config)
        click.echo(f"Using config from {doctrail_config}")
    else:
        config_to_load = None

    if config_to_load:
        try:
            with open(config_to_load, 'r') as f:
                config_data = yaml.safe_load(f)
            if not readability and config_data.get('readability', False):
                readability = True
            if html_extractor == 'default' and config_data.get('html_extractor'):
                html_extractor = config_data.get('html_extractor')
            pdf_engine = pdf_engine or config_data.get('pdf_engine')
            ocr_engine = ocr_engine or config_data.get('ocr_engine')
            if not skip_garbage_check and config_data.get('skip_garbage_check', False):
                skip_garbage_check = True
        except Exception as e:
            raise click.UsageError(f"Error loading config: {e}")

    pdf_engine = pdf_engine or os.environ.get('DOCTRAIL_PDF_ENGINE') or 'auto'
    ocr_engine = ocr_engine or os.environ.get('DOCTRAIL_OCR_ENGINE') or 'auto'
    if pdf_engine == 'mac-ocr' and ocr_engine == 'auto':
        ocr_engine = 'mac-ocr'
    if extractor == 'auto':
        extractor = os.environ.get('DOCTRAIL_EXTRACTOR') or 'auto'

    # Default to documents_path from config
    if not input_dir and not zotero and not plugin:
        if config_data and config_data.get('documents_path'):
            docs_dir = Path(config_data['documents_path']).expanduser()
            if not docs_dir.is_absolute():
                docs_dir = Path.cwd() / docs_dir
            input_dir = (str(docs_dir),)
            click.echo(f"📂 Ingesting from: {docs_dir}")
        else:
            input_dir = (str(Path.cwd()),)
            click.echo(f"📂 Ingesting from current directory")

    # Validate mode
    modes = sum([bool(input_dir), bool(zotero), bool(plugin)])
    if modes != 1:
        raise click.UsageError(
            "Must provide exactly ONE of: --input-dir, --zotero, or --plugin"
        )

    final_db_path = _resolve_ingest_db_path(db_path, config_data)

    if zotero and not collection:
        raise click.UsageError("--collection required with --zotero")

    if input_dir:
        for dir_path in input_dir:
            if not os.path.exists(dir_path):
                raise click.UsageError(f"Directory not found: {dir_path}")

    try:
        from ..core import run_ingest, ConfigurationError, DatabaseError

        plugin_args = None
        if plugin:
            plugin_args = {}
            if cache_db:
                plugin_args['cache_db'] = cache_db
            if project:
                plugin_args['project'] = project
            if base_path:
                plugin_args['base_path'] = base_path
            if collection:
                plugin_args['collection'] = collection
            if api_key:
                plugin_args['api_key'] = api_key
            if user_id:
                plugin_args['user_id'] = user_id
            if zotero_dir:
                plugin_args['zotero_dir'] = zotero_dir

        zotero_api_key = None
        zotero_library_id = None
        zotero_library_type = 'user'

        if zotero:
            zotero_cfg = get_zotero_config(config_data)
            zotero_api_key = zotero_cfg['api_key']
            zotero_library_id = zotero_cfg['library_id']
            zotero_library_type = zotero_cfg['library_type']

        result = asyncio.run(run_ingest(
            db_path=final_db_path,
            input_dirs=list(input_dir) if input_dir else None,
            table=table,
            force=force,
            overwrite=overwrite,
            skip_existing=skip_existing,
            limit=limit,
            include_pattern=include_pattern,
            exclude_pattern=exclude_pattern,
            workers=workers,
            extractor=extractor,
            pdf_engine=pdf_engine,
            ocr_engine=ocr_engine,
            readability=readability,
            html_extractor=html_extractor,
            skip_garbage_check=skip_garbage_check,
            fulltext=fulltext,
            manifest_path=manifest,
            verbose=verbose,
            yes=yes,
            plugin_name=plugin,
            plugin_args=plugin_args,
            zotero_api_key=zotero_api_key,
            zotero_library_id=zotero_library_id,
            zotero_library_type=zotero_library_type,
            zotero_collection=collection,
        ))

        if result['status'] == 'success':
            mode = result.get('mode', result.get('plugin', 'unknown'))
            click.echo(f"\nIngestion completed ({mode} mode)")
            if 'directories_processed' in result:
                click.echo(f"Directories: {result['directories_processed']}")

    except (ConfigurationError, DatabaseError) as e:
        click.echo(f"\nError: {e}", err=True)
        ctx.exit(1)
    except KeyboardInterrupt:
        click.echo("\nInterrupted", err=True)
        ctx.exit(1)
    except Exception as e:
        click.echo(f"\nError: {e}", err=True)
        if verbose:
            import traceback
            traceback.print_exc()
        ctx.exit(1)
