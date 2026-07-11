"""
Core ingestion logic and workflow coordination.

This module contains the main ingestion orchestration function.
"""

import os
import sys
import time
import signal
import hashlib
import asyncio
import logging
import tempfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
import click
import sqlite_utils
from rich import print
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
from loguru import logger

# Import from sibling modules
from .database import (
    check_db_schema,
    clean_metadata,
    configure_ingest_database,
    ensure_ingest_timestamps,
    insert_document,
    setup_fts,
)
from .document_processor import process_document, SkippedFileException
from ..db_operations import _quote_identifier
from ..file_filters import should_skip_file, apply_file_patterns, check_for_manual_override
from .manifest import load_manifest, get_file_metadata, find_manifest_in_directory

# Initialize Rich console for pretty output
console = Console()


def _expand_zip_inputs(
    paths: List[Path],
) -> tuple[
    List[Path],
    Dict[str, str],
    Dict[str, Dict[str, Any]],
    Optional[tempfile.TemporaryDirectory],
    List[tuple[str, str]],
]:
    """Expand ZIP inputs through Rust and return leaf paths plus provenance.

    One temporary tree is owned by the caller for the duration of ingestion.
    Nested ZIPs are expanded breadth-first with global entry and byte budgets so
    individually valid inner archives cannot multiply into an aggregate bomb.
    """
    zip_paths = [path for path in paths if path.suffix.lower() == ".zip"]
    if not zip_paths:
        return paths, {}, {}, None, []

    from . import native_extractor

    if not native_extractor.available():
        raise RuntimeError(
            "ZIP ingestion requires the native Rust extension; run `make native`"
        )

    staging = tempfile.TemporaryDirectory(prefix="doctrail_zip_")
    staging_root = Path(staging.name)
    pending = deque()
    leaves: List[Path] = []
    logical_paths: Dict[str, str] = {}
    archive_metadata: Dict[str, Dict[str, Any]] = {}
    archive_failures: List[tuple[str, str]] = []
    archive_index = 0
    total_entries = 0
    total_expanded_bytes = 0

    for path in paths:
        if path.suffix.lower() == ".zip":
            pending.append((path, str(path), str(path), "", 1))
        else:
            leaves.append(path)

    while pending:
        archive_path, logical_archive, top_archive, member_prefix, depth = pending.popleft()
        if depth > native_extractor.ZIP_MAX_NESTING_DEPTH:
            raise RuntimeError(
                f"ZIP nesting exceeds depth {native_extractor.ZIP_MAX_NESTING_DEPTH}: "
                f"{logical_archive}"
            )
        destination = staging_root / f"archive-{archive_index:06}"
        archive_index += 1
        try:
            members = native_extractor.expand_zip(str(archive_path), str(destination))
        except Exception as exc:
            error = str(exc)
            logger.warning(f"Skipping unreadable ZIP archive {logical_archive}: {error}")
            archive_failures.append((logical_archive, error))
            continue
        total_entries += len(members)
        total_expanded_bytes += sum(member["uncompressed_bytes"] for member in members)
        if total_entries > native_extractor.ZIP_MAX_TOTAL_ENTRIES:
            raise RuntimeError(
                f"nested ZIPs expanded to {total_entries} entries, exceeding global limit "
                f"{native_extractor.ZIP_MAX_TOTAL_ENTRIES}"
            )
        if total_expanded_bytes > native_extractor.ZIP_MAX_TOTAL_BYTES:
            raise RuntimeError(
                f"nested ZIPs expanded to {total_expanded_bytes} bytes, exceeding global limit "
                f"{native_extractor.ZIP_MAX_TOTAL_BYTES}"
            )

        for member in members:
            member_path = member["member_path"]
            member_parts = Path(member_path).parts
            if any(part.startswith(".") or part == "__MACOSX" for part in member_parts):
                logger.debug(f"Skipping hidden ZIP member: {logical_archive}!/{member_path}")
                continue
            extracted_path = Path(member["path"])
            member_chain = f"{member_prefix}!/{member_path}" if member_prefix else member_path
            logical_path = f"{top_archive}!/{member_chain}"
            if extracted_path.suffix.lower() == ".zip":
                pending.append(
                    (extracted_path, logical_path, top_archive, member_chain, depth + 1)
                )
                continue
            leaves.append(extracted_path)
            logical_paths[str(extracted_path)] = logical_path
            archive_metadata[str(extracted_path)] = {
                "archive_path": top_archive,
                "archive_member_path": member_chain,
                "archive_depth": depth,
            }

    logger.info(
        f"Expanded {len(zip_paths)} ZIP input(s) into {len(logical_paths)} leaf file(s); "
        f"{total_expanded_bytes} bytes staged"
    )
    return leaves, logical_paths, archive_metadata, staging, archive_failures


def _default_worker_count() -> int:
    cpu_count = os.cpu_count() or 1
    return max(1, min(cpu_count, 8))


def _short_display_name(file_path: Path, input_path: Path) -> str:
    try:
        display_name = str(file_path.relative_to(input_path))
    except Exception:
        display_name = file_path.name
    if len(display_name) > 40:
        return "..." + display_name[-37:]
    return display_name


def _extract_primary_fields(json_metadata_obj: Optional[dict]) -> Dict[str, str]:
    extracted_primary_fields: Dict[str, str] = {}
    if not isinstance(json_metadata_obj, dict):
        return extracted_primary_fields

    def pick(*keys):
        for key in keys:
            value = json_metadata_obj.get(key)
            if value not in (None, ""):
                return value
        return None

    url_val = pick('url', 'original_url', 'source_url', 'page_url')
    if url_val:
        extracted_primary_fields['url'] = str(url_val)

    archive_url_val = pick('archive_url', 'archiveUrl', 'archive_link', 'archiveLink')
    if archive_url_val:
        extracted_primary_fields['archive_url'] = str(archive_url_val)

    captured_time_val = pick('captured_time', 'capture_time', 'captured_at', 'timestamp', 'time')
    if captured_time_val:
        extracted_primary_fields['captured_time'] = str(captured_time_val)

    return extracted_primary_fields


def _load_sidecar_metadata(file_path: Path) -> tuple[Optional[dict], Optional[str]]:
    import json

    candidates = [
        file_path.with_suffix('.json'),
        file_path.parent / f"{file_path.name}.json",
    ]
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            with open(candidate, 'r', encoding='utf-8') as handle:
                return json.load(handle), str(candidate)
        except Exception as exc:
            logger.warning(f"Failed to parse JSON sidecar {candidate}: {exc}")
    return None, None


def _merge_ingest_json_metadata(
    base_metadata: Optional[Any],
    spreadsheet_sheets: Optional[list[dict]],
) -> Optional[Any]:
    """Merge structured spreadsheet payloads into stored json_metadata."""
    if not spreadsheet_sheets:
        return base_metadata

    spreadsheet_payload = {
        "spreadsheet_sheet_count": len(spreadsheet_sheets),
        "spreadsheet_sheets": spreadsheet_sheets,
    }

    if base_metadata is None:
        return spreadsheet_payload

    if isinstance(base_metadata, dict):
        merged = dict(base_metadata)
        merged.update(spreadsheet_payload)
        return merged

    return {
        "sidecar_data": base_metadata,
        **spreadsheet_payload,
    }


def _normalize_worker_value(value: Any, seen: Optional[set[int]] = None) -> Any:
    """Convert worker results to plain Python values that are safe to persist."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    if isinstance(value, Path):
        return str(value)

    if seen is None:
        seen = set()

    if isinstance(value, dict):
        obj_id = id(value)
        if obj_id in seen:
            return "[recursive]"
        seen.add(obj_id)
        try:
            return {
                str(key): _normalize_worker_value(item, seen)
                for key, item in value.items()
            }
        finally:
            seen.discard(obj_id)

    if isinstance(value, (list, tuple)):
        obj_id = id(value)
        if obj_id in seen:
            return ["[recursive]"]
        seen.add(obj_id)
        try:
            return [_normalize_worker_value(item, seen) for item in value]
        finally:
            seen.discard(obj_id)

    if isinstance(value, set):
        obj_id = id(value)
        if obj_id in seen:
            return ["[recursive]"]
        seen.add(obj_id)
        try:
            return [
                _normalize_worker_value(item, seen)
                for item in sorted(value, key=lambda item: str(item))
            ]
        finally:
            seen.discard(obj_id)

    return str(value)


def _build_success_result(
    file_path: str,
    sha1: str,
    content: Any,
    metadata: Any,
    elapsed: float,
) -> Dict[str, Any]:
    normalized_metadata = _normalize_worker_value(metadata)
    if not isinstance(normalized_metadata, dict):
        normalized_metadata = {"raw_metadata": str(normalized_metadata)}

    normalized_content = content if isinstance(content, str) else str(content or "")

    return {
        'success': True,
        'file_path': file_path,
        'sha1': sha1,
        'content': normalized_content,
        'metadata': normalized_metadata,
        'elapsed': elapsed,
    }


def _extract_document_worker(payload: Dict[str, Any]) -> Dict[str, Any]:
    import time

    start_time = time.time()
    file_path = payload['file_path']
    file_sha1 = payload['file_sha1']

    try:
        sha1, content, metadata = asyncio.run(process_document(
            file_path,
            file_sha1,
            use_readability=payload['readability'],
            html_extractor=payload['html_extractor'],
            skip_garbage_check=payload['skip_garbage_check'],
            pdf_engine=payload['pdf_engine'],
            ocr_engine=payload['ocr_engine'],
        ))
        return _build_success_result(
            file_path=file_path,
            sha1=sha1,
            content=content,
            metadata=metadata,
            elapsed=time.time() - start_time,
        )
    except SkippedFileException as exc:
        return {
            'success': None,
            'file_path': file_path,
            'error': f"Skipped: {exc}",
            'elapsed': time.time() - start_time,
        }
    except Exception as exc:
        return {
            'success': False,
            'file_path': file_path,
            'error': f"Error: {exc}",
            'elapsed': time.time() - start_time,
        }


async def process_ingest(
    db_path: str,
    input_dir: str,
    table: str,
    verbose: bool = False,
    force: bool = False,
    overwrite: bool = False,
    limit: Optional[int] = None,
    include_pattern: Optional[str] = None,
    exclude_pattern: Optional[str] = None,
    readability: bool = False,
    html_extractor: str = 'default',
    skip_garbage_check: bool = False,
    yes: bool = False,
    fulltext: bool = False,
    manifest_path: Optional[str] = None,
    labels: Optional[List[str]] = None,
    pdf_engine: str = 'auto',
    ocr_engine: str = 'auto',
    workers: Optional[int] = None,
    override_filepaths: Optional[Dict[str, str]] = None,
    extractor: str = 'auto',
):
    """
    Process files from directory and insert into database.
    
    Args:
        db_path: Path to SQLite database
        input_dir: Directory containing files to ingest
        table: Table name to insert documents into
        verbose: Enable verbose logging
        readability: Use readability library for HTML content extraction
        force: Force import even if database schema doesn't match
        fulltext: Create full-text search index
    """
    # Set up signal handling for graceful shutdown
    shutdown_requested = False
    
    def signal_handler(sig, frame):
        console.print("\n[red]Shutdown requested. Terminating immediately...[/red]")
        logger.info("Shutdown signal received - terminating")
        # Force immediate exit
        os._exit(1)
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Set up logging
    log_level = "DEBUG" if verbose else "WARNING"  # Less verbose by default
    logger.remove()
    logger.add(sys.stderr, level=log_level)
    
    # Add a file log for more detailed logging in /tmp
    log_dir = Path("/tmp/doctrail_logs")
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = log_dir / f"doctrail_ingest_{timestamp}.log"
    logger.add(str(log_file), level="DEBUG", rotation="100 MB")
    
    logger.info(f"Starting doctrail ingestion - detailed logs at: {log_file}")
    
    # Expand user path for database
    db_path = os.path.expanduser(db_path)
    db_parent = Path(db_path).parent
    db_parent.mkdir(parents=True, exist_ok=True)

    if workers is not None and workers < 1:
        raise RuntimeError("workers must be >= 1")

    # Check if database schema is compatible
    try:
        schema_ok = check_db_schema(db_path, table)
    except Exception as e:
        raise RuntimeError(f"Could not open database file '{db_path}': {e}") from e

    if not force and not schema_ok:
        raise RuntimeError(
            f"Database schema mismatch for table '{table}'. "
            "Use --force to override this check (may cause data issues)."
        )

    # Initialize database connection
    try:
        db = configure_ingest_database(sqlite_utils.Database(db_path))
        ensure_ingest_timestamps(db, table)
    except Exception as e:
        raise RuntimeError(f"Could not open database file '{db_path}': {e}") from e
    
    # Get existing documents if not overwriting
    existing_sha1s = set()
    if not overwrite:
        try:
            if table in db.table_names():
                table_ref = _quote_identifier(table, "table name")
                existing_sha1s = {row[0] for row in db.execute(f"SELECT sha1 FROM {table_ref}")}
                logger.info(f"Found {len(existing_sha1s)} existing documents in table '{table}'")
        except Exception as e:
            logger.warning(f"Could not read existing documents: {e}")
    
    # Find all files in the input directory
    input_path = Path(input_dir)
    if not input_path.exists():
        raise RuntimeError(f"Input directory does not exist: {input_dir}")
    
    # Collect files based on whether input is a file or directory
    if input_path.is_file():
        # Single file mode
        all_files = [input_path]
        logger.info(f"Processing single file: {input_path}")
    else:
        # Directory mode - find all files recursively
        all_files = list(input_path.rglob("*"))
        all_files = [f for f in all_files if f.is_file()]
        # Never descend into hidden directories (.git, .unison, .stversions,
        # .svn, ...). rglob walks into them, and their contents (e.g. Unison
        # conflict copies) have non-hidden names that should_skip_file would let
        # through. Prune any file that passes through a dotted directory below
        # the input root; the root's own ancestors are not considered.
        pre_hidden = len(all_files)
        all_files = [
            f for f in all_files
            if not any(part.startswith('.') for part in f.relative_to(input_path).parts[:-1])
        ]
        if pre_hidden != len(all_files):
            logger.info(f"Skipped {pre_hidden - len(all_files)} file(s) inside hidden directories")
        logger.info(f"Found {len(all_files)} total files in {input_dir}")

    (
        all_files,
        archive_filepaths,
        archive_metadata,
        archive_staging,
        archive_failures,
    ) = _expand_zip_inputs(all_files)
    effective_override_filepaths = dict(override_filepaths or {})
    effective_override_filepaths.update(archive_filepaths)
    
    # Apply include/exclude patterns
    if include_pattern or exclude_pattern:
        all_files = apply_file_patterns(all_files, include_pattern, exclude_pattern)
    
    # Apply limit if specified
    if limit and len(all_files) > limit:
        all_files = all_files[:limit]
    
    # Load manifest if provided or auto-detect
    manifest_data = {}
    manifest_keys = set()
    if manifest_path:
        # Use explicit manifest path
        try:
            manifest_data = load_manifest(manifest_path)
            console.print(f"[green]✓[/green] Loaded manifest from: {manifest_path}")
        except Exception as e:
            raise RuntimeError(f"Error loading manifest: {e}") from e
    elif input_path.is_dir():
        # Auto-detect manifest.json in directory
        auto_manifest = find_manifest_in_directory(str(input_path))
        if auto_manifest:
            try:
                manifest_data = load_manifest(auto_manifest)
                console.print(f"[green]✓[/green] Auto-detected and loaded manifest.json")
            except Exception as e:
                logger.warning(f"Found manifest.json but couldn't load it: {e}")
        if limit:
            logger.info(f"Limited to first {limit} files")
    
    if manifest_data:
        manifest_keys = {
            key
            for metadata in manifest_data.values()
            for key in metadata.keys()
        }

    # Filter out already-processed files
    files_to_process = []
    existing_office_paths: set[str] = set()
    skipped_count = 0
    from .embedded_media import OFFICE_SUFFIXES
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        
        filter_task = progress.add_task("Filtering files...", total=len(all_files))
        
        for file_path in all_files:
            progress.update(filter_task, advance=1)
            
            # Skip if should be ignored
            if should_skip_file(str(file_path)):
                skipped_count += 1
                continue

            # Skip standalone JSON files from direct processing; pair them as metadata later
            if file_path.suffix.lower() == '.json':
                skipped_count += 1
                logger.debug(f"Skipping JSON sidecar candidate: {file_path}")
                continue
            
            # Calculate SHA1
            try:
                with open(file_path, 'rb') as f:
                    file_sha1 = hashlib.sha1(f.read()).hexdigest()
                
                # Skip if already processed (unless overwriting)
                if not overwrite and file_sha1 in existing_sha1s:
                    logger.debug(f"Skipping already processed file: {file_path}")
                    if file_path.suffix.lower() in OFFICE_SUFFIXES:
                        existing_office_paths.add(str(file_path))
                    continue
                
                files_to_process.append((file_path, file_sha1))
            except Exception as e:
                logger.warning(f"Could not read file {file_path}: {e}")
                continue

    # Deduplicate by sha1 (the ingest row key): identical bytes are the same
    # document. Keep the first occurrence in input order so the winning row is
    # deterministic regardless of extractor (native vs python) or route (clean
    # vs OCR fallback), and duplicate files are never extracted/OCR'd twice.
    if files_to_process:
        seen_keys = set()
        deduped = []
        for dup_fp, dup_sha in files_to_process:
            if dup_sha in seen_keys:
                continue
            seen_keys.add(dup_sha)
            deduped.append((dup_fp, dup_sha))
        dup_removed = len(files_to_process) - len(deduped)
        if dup_removed:
            logger.info(f"Skipping {dup_removed} duplicate-content file(s) with an already-seen sha1")
        files_to_process = deduped

    if not files_to_process and not existing_office_paths and not archive_failures:
        console.print("[yellow]No new files to process.[/yellow]")
        return {
            'status': 'success',
            'successful': 0,
            'skipped': skipped_count,
            'failed': 0,
        }
    
    # Show summary and confirm
    worker_count = min(_default_worker_count() if workers is None else workers, max(1, len(files_to_process)))
    console.print(f"\n[bold]Ingestion Summary:[/bold]")
    console.print(f"  Database: {db_path}")
    console.print(f"  Table: {table}")
    console.print(f"  Files to process: {len(files_to_process)}")
    console.print(f"  Files skipped: {skipped_count}")
    console.print(f"  Already in database: {len(all_files) - len(files_to_process) - skipped_count}")
    console.print(f"  Worker threads: {worker_count}")
    
    if not yes:
        if not click.confirm("\nProceed with ingestion?", default=True):
            console.print("[yellow]Ingestion cancelled.[/yellow]")
            return
    
    # Process files
    successful = 0
    failed = len(archive_failures)
    warnings = 0
    failed_files = [
        (archive_path, f"ZIP expansion failed: {error}")
        for archive_path, error in archive_failures
    ]
    successful_source_paths: set[str] = set(existing_office_paths)
    embedded_media_stats: Dict[str, Any] = {}

    # Seconds to pause after each per-file status line; used by screen
    # recordings (scripts/build_demo_gif.sh) so the output stays readable.
    try:
        line_delay = float(os.environ.get("DOCTRAIL_INGEST_THROTTLE", "0"))
    except ValueError:
        line_delay = 0.0

    # Create a task for overall progress
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:

        task = progress.add_task(f"Importing {len(files_to_process)} documents", total=len(files_to_process))
        
        # Track timing for log
        file_timings = []

        def handle_result(result: Dict[str, Any]) -> None:
            nonlocal successful, failed, warnings

            file_path = Path(result['file_path'])
            progress.update(task, advance=1)

            try:
                display_name = str(file_path.relative_to(input_path))
            except Exception:
                display_name = file_path.name

            elapsed = result.get('elapsed', 0) or 0
            elapsed_str = f" ({elapsed:.1f}s)" if elapsed >= 0.1 else ""
            url_hint = ""

            if result['success'] is True:
                if result.get('content') == "":
                    try:
                        if file_path.stat().st_size > 0:
                            logger.warning(
                                f"Extractor returned empty content for non-empty source file: {file_path}"
                            )
                    except OSError:
                        logger.warning(
                            f"Extractor returned empty content and source size could not be checked: {file_path}"
                        )

                raw_metadata = dict(result['metadata'])
                structured_sheets = raw_metadata.pop('_spreadsheet_sheets', None)
                metadata = clean_metadata(raw_metadata)
                logical_file_path = effective_override_filepaths.get(str(file_path), str(file_path))
                manifest_metadata = get_file_metadata(logical_file_path, manifest_data)
                if manifest_metadata:
                    metadata.update(manifest_metadata)
                    logger.debug(f"Added {len(manifest_metadata)} fields from manifest for {file_path.name}")

                for key in manifest_keys:
                    metadata.setdefault(key, None)

                json_metadata_obj, sidecar_path_str = _load_sidecar_metadata(file_path)
                json_metadata_obj = _merge_ingest_json_metadata(json_metadata_obj, structured_sheets)
                extracted_primary_fields = _extract_primary_fields(json_metadata_obj)

                if str(file_path) in archive_metadata:
                    metadata.update(archive_metadata[str(file_path)])

                final_file_path = str(file_path)
                if str(file_path) in effective_override_filepaths:
                    final_file_path = effective_override_filepaths[str(file_path)]
                    metadata['original_file_path'] = final_file_path

                insert_document(
                    db,
                    table,
                    result['sha1'],
                    final_file_path,
                    result['content'],
                    metadata,
                    labels=labels,
                    json_metadata=json_metadata_obj,
                    extra_fields=extracted_primary_fields,
                    overwrite=overwrite,
                    file_stat_path=str(file_path),
                )

                if extracted_primary_fields.get('url'):
                    url_hint = f" [dim](url: {extracted_primary_fields['url']})[/dim]"
                elif extracted_primary_fields.get('archive_url'):
                    url_hint = f" [dim](archive: {extracted_primary_fields['archive_url']})[/dim]"
                elif sidecar_path_str:
                    url_hint = " [dim](sidecar)[/dim]"

                successful += 1
                successful_source_paths.add(str(file_path))
                time_color = "yellow" if elapsed > 5 else "dim"
                progress.console.print(f"[green]✓[/green] {display_name}[{time_color}]{elapsed_str}[/{time_color}]{url_hint}")
                file_timings.append({'file': display_name, 'status': 'success', 'elapsed': elapsed})
            elif result['success'] is False:
                failed += 1
                failed_files.append((file_path.name, result['error']))
                progress.console.print(f"[red]✗[/red] {display_name}[dim]{elapsed_str}[/dim]: {result['error']}")
                file_timings.append({'file': display_name, 'status': 'failed', 'elapsed': elapsed, 'error': result['error']})
            else:
                warnings += 1
                logger.debug(f"Skipped {display_name}: {result['error']}")
                file_timings.append({'file': display_name, 'status': 'skipped', 'elapsed': elapsed})

            if line_delay > 0 and result['success'] is not None:
                time.sleep(line_delay)

        # Native (Rust) is authoritative for auto/rust mode. Python extraction is
        # available only through the explicit --extractor python backup switch.
        from . import native_extractor
        if extractor == 'auto' and os.environ.get('DOCTRAIL_DISABLE_NATIVE'):
            extractor = 'python'
        native_active = extractor in ('auto', 'rust')
        if native_active and not native_extractor.available():
            raise RuntimeError(
                "Native extraction is required for --extractor auto/rust, but "
                "doctrail._ingest_native is not installed. Run `make native`, or "
                "use `--extractor python` for the explicit backup path."
            )
        if native_active:
            console.print("[cyan]Using native Rust extractor[/cyan] (multicore in-process)")
            path_to_sha = {str(fp): sha for fp, sha in files_to_process}
            native_paths: List[str] = []
            for fp, sha in files_to_process:
                p = str(fp)
                override_path = check_for_manual_override(p)
                if not override_path:
                    native_paths.append(p)
                    continue
                try:
                    override_content = Path(override_path).read_text(encoding="utf-8").strip()
                    if not override_content:
                        raise ValueError("manual override is empty")
                    handle_result({
                        "success": True,
                        "file_path": p,
                        "sha1": sha,
                        "content": override_content,
                        "metadata": {
                            "extraction_method": "manual_override",
                            "processing_method": f"manual_override_{Path(override_path).stem.split('--')[-1]}",
                            "original_file_path": p,
                            "original_file_type": fp.suffix.lower().lstrip("."),
                        },
                        "elapsed": 0,
                    })
                except Exception as exc:
                    handle_result({
                        "success": False,
                        "file_path": p,
                        "error": f"Manual override failed: {exc}",
                        "elapsed": 0,
                    })
            native_chunk = 512
            for start in range(0, len(native_paths), native_chunk):
                if shutdown_requested:
                    break
                batch_paths = native_paths[start:start + native_chunk]
                # Per-file Rust panics come back as status=failed. A recoverable
                # batch-level failure is recorded against every input in the chunk;
                # native process aborts and segfaults cannot be caught in-process.
                try:
                    docs = native_extractor.extract_batch(batch_paths, workers)
                except Exception as exc:
                    logger.error(f"Native extract_batch failed for {len(batch_paths)} file(s): {exc}")
                    for p in batch_paths:
                        handle_result({
                            "success": False,
                            "file_path": p,
                            "error": f"Native batch failed: {exc}",
                            "elapsed": 0,
                        })
                    continue
                for p, doc in zip(batch_paths, docs):
                    if native_extractor.is_complete_extraction(doc, p):
                        handle_result(native_extractor.to_result(p, path_to_sha[p], doc))
                        continue
                    reason = (
                        doc.get("error")
                        or doc.get("ocr_reason")
                        or f"native status={doc.get('status')!r}"
                    )
                    handle_result({
                        "success": False,
                        "file_path": p,
                        "error": f"Native extraction incomplete: {reason}",
                        "elapsed": (doc.get("extraction_ms") or 0) / 1000.0,
                    })
            files_to_process = []

        submission_batch_size = max(worker_count * 4, 1)
        loop = asyncio.get_running_loop()

        if worker_count == 1:
            for file_path, file_sha1 in files_to_process:
                if shutdown_requested:
                    break
                progress.update(task, description=f"[cyan]{file_path.name}[/cyan]")
                start_time = datetime.now()
                try:
                    sha1, content, metadata = await process_document(
                        str(file_path),
                        file_sha1,
                        use_readability=readability,
                        html_extractor=html_extractor,
                        skip_garbage_check=skip_garbage_check,
                        pdf_engine=pdf_engine,
                        ocr_engine=ocr_engine,
                    )
                    result = _build_success_result(
                        file_path=str(file_path),
                        sha1=sha1,
                        content=content,
                        metadata=metadata,
                        elapsed=(datetime.now() - start_time).total_seconds(),
                    )
                except SkippedFileException as exc:
                    result = {
                        'success': None,
                        'file_path': str(file_path),
                        'error': f"Skipped: {exc}",
                        'elapsed': (datetime.now() - start_time).total_seconds(),
                    }
                except Exception as exc:
                    result = {
                        'success': False,
                        'file_path': str(file_path),
                        'error': f"Error: {exc}",
                        'elapsed': (datetime.now() - start_time).total_seconds(),
                    }
                handle_result(result)
        else:
            # Extraction metadata can contain parser objects that are safe to stringify
            # but unsafe to pickle, so keep concurrent extraction in-process.
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                file_iter = iter(files_to_process)
                pending_futures: set[asyncio.Future] = set()

                def submit_next() -> bool:
                    try:
                        file_path, file_sha1 = next(file_iter)
                    except StopIteration:
                        return False

                    display_name = _short_display_name(file_path, input_path)
                    progress.update(task, description=f"[cyan]{display_name}[/cyan]")
                    future = loop.run_in_executor(
                        executor,
                        _extract_document_worker,
                        {
                            'file_path': str(file_path),
                            'file_sha1': file_sha1,
                            'readability': readability,
                            'html_extractor': html_extractor,
                            'skip_garbage_check': skip_garbage_check,
                            'pdf_engine': pdf_engine,
                            'ocr_engine': ocr_engine,
                        }
                    )
                    pending_futures.add(future)
                    return True

                for _ in range(min(submission_batch_size, len(files_to_process))):
                    if not submit_next():
                        break

                while pending_futures and not shutdown_requested:
                    done, pending_futures = await asyncio.wait(
                        pending_futures,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for future in done:
                        handle_result(await future)
                        if not shutdown_requested:
                            submit_next()
    
    # Extract each embedded Office image as a linked child row and OCR it. This
    # runs after parent rows are durable so every child can point to an existing
    # parent sha1. ZIP-expanded Office files still exist until archive cleanup.
    from .embedded_media import run_media_ingest
    office_paths = [
        str(path)
        for path in all_files
        if str(path) in successful_source_paths and path.suffix.lower() in OFFICE_SUFFIXES
    ]
    if office_paths:
        console.print(f"\n[cyan]Extracting embedded images from {len(office_paths)} Office file(s)...[/cyan]")
        embedded_media_stats = await asyncio.to_thread(
            run_media_ingest,
            office_paths,
            db_path,
            table,
            ocr_engine=ocr_engine,
            workers=worker_count,
            overwrite=overwrite,
            fulltext=fulltext,
            path_overrides=effective_override_filepaths,
        )
        media_failures = embedded_media_stats.get("failed_files", [])
        for media_error in media_failures:
            failed += 1
            failed_files.append(("embedded media", media_error))
        console.print(
            "  Embedded images: "
            f"[green]{embedded_media_stats.get('inserted', 0)} stored[/green], "
            f"{embedded_media_stats.get('ocr_chars', 0)} OCR characters"
        )
        if embedded_media_stats.get("ocr_empty"):
            console.print(
                f"  [yellow]{embedded_media_stats['ocr_empty']} image(s) were OCR-attempted "
                "but contained no readable text[/yellow]"
            )

    # A passive checkpoint never waits for readers and leaves any reader-pinned
    # WAL frames for SQLite's normal autocheckpoint machinery.
    try:
        checkpoint = db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        logger.debug(f"Final passive WAL checkpoint: {checkpoint}")
    except Exception as e:
        logger.warning(f"Final passive WAL checkpoint failed: {e}")
    
    # Create FTS index if requested
    if fulltext and successful > 0:
        console.print("\n[bold]Creating full-text search index...[/bold]")
        setup_fts(db_path, table)
    
    # Show results
    console.print(f"\n[bold]Ingestion Complete![/bold]")
    console.print(f"  Successfully processed: [green]{successful}[/green]")
    console.print(f"  Skipped (duplicates/hidden): [yellow]{warnings}[/yellow]")
    if failed > 0:
        console.print(f"  Failed with errors: [red]{failed}[/red]")
        # Show which files failed
        if failed_files:
            console.print("\n[bold red]Failed files (with actual errors):[/bold red]")
            for filename, error in failed_files:
                console.print(f"  [red]✗[/red] {filename}: {error}")
    
    # Provide helpful next steps
    if successful > 0:
        console.print(f"\n[bold]Next steps:[/bold]")
        console.print(f"  View your data: [cyan]doctrail query[/cyan]")
        console.print(f"  Enrich with LLMs: [cyan]doctrail enrich --help[/cyan]")

    # Write ingest log to .doctrail/ directory
    try:
        import json
        doctrail_dir = Path.cwd() / ".doctrail"
        if doctrail_dir.exists():
            log_path = doctrail_dir / "ingest.log"
            total_time = sum(f.get('elapsed', 0) for f in file_timings)
            log_data = {
                'timestamp': datetime.now().isoformat(),
                'db_path': db_path,
                'table': table,
                'total_files': len(file_timings),
                'successful': successful,
                'failed': failed,
                'skipped': warnings,
                'total_time_seconds': round(total_time, 2),
                'files': sorted(file_timings, key=lambda x: -x.get('elapsed', 0)),  # Slowest first
            }
            with open(log_path, 'w') as f:
                json.dump(log_data, f, indent=2)
            console.print(f"\n[dim]Log saved to: {log_path}[/dim]")
    except Exception as e:
        logger.warning(f"Could not write ingest log: {e}")

    if archive_staging is not None:
        archive_staging.cleanup()

    return {
        'status': 'success',
        'successful': successful,
        'skipped': warnings,
        'failed': failed,
        'embedded_media': embedded_media_stats,
    }
