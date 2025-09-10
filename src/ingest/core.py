"""
Core ingestion logic and workflow coordination.

This module contains the main ingestion orchestration function.
"""

import os
import sys
import signal
import hashlib
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, List
import click
import sqlite_utils
from rich import print
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
from loguru import logger

# Import from sibling modules
from .database import insert_document, check_db_schema, setup_fts, clean_metadata
from .document_processor import process_document, SkippedFileException
from ..file_filters import should_skip_file, apply_file_patterns
from .manifest import load_manifest, get_file_metadata, find_manifest_in_directory

# Initialize Rich console for pretty output
console = Console()


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
    manifest_path: Optional[str] = None
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
        console.print("\n[red]⚠️  Shutdown requested. Terminating immediately...[/red]")
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
    
    # Check if database schema is compatible
    if not force and not check_db_schema(db_path, table):
        console.print(f"[red]Error: Database schema mismatch for table '{table}'[/red]")
        console.print("Use --force to override this check (may cause data issues)")
        return
    
    # Initialize database connection
    db = sqlite_utils.Database(db_path)
    
    # Get existing documents if not overwriting
    existing_sha1s = set()
    if not overwrite:
        try:
            if table in db.table_names():
                existing_sha1s = {row['sha1'] for row in db.execute(f"SELECT sha1 FROM {table}")}
                logger.info(f"Found {len(existing_sha1s)} existing documents in table '{table}'")
        except Exception as e:
            logger.warning(f"Could not read existing documents: {e}")
    
    # Find all files in the input directory
    input_path = Path(input_dir)
    if not input_path.exists():
        console.print(f"[red]Error: Input directory does not exist: {input_dir}[/red]")
        return
    
    # Collect files based on whether input is a file or directory
    if input_path.is_file():
        # Single file mode
        all_files = [input_path]
        logger.info(f"Processing single file: {input_path}")
    else:
        # Directory mode - find all files recursively
        all_files = list(input_path.rglob("*"))
        all_files = [f for f in all_files if f.is_file()]
        logger.info(f"Found {len(all_files)} total files in {input_dir}")
    
    # Apply include/exclude patterns
    if include_pattern or exclude_pattern:
        all_files = apply_file_patterns(all_files, include_pattern, exclude_pattern)
    
    # Apply limit if specified
    if limit and len(all_files) > limit:
        all_files = all_files[:limit]
    
    # Load manifest if provided or auto-detect
    manifest_data = {}
    if manifest_path:
        # Use explicit manifest path
        try:
            manifest_data = load_manifest(manifest_path)
            console.print(f"[green]✓[/green] Loaded manifest from: {manifest_path}")
        except Exception as e:
            console.print(f"[red]Error loading manifest: {e}[/red]")
            return
    elif input_path.is_dir():
        # Auto-detect manifest.json in directory
        auto_manifest = find_manifest_in_directory(str(input_path))
        if auto_manifest:
            try:
                manifest_data = load_manifest(auto_manifest)
                console.print(f"[green]✓[/green] Auto-detected and loaded manifest.json")
            except Exception as e:
                logger.warning(f"Found manifest.json but couldn't load it: {e}")
        logger.info(f"Limited to first {limit} files")
    
    # Filter out already-processed files
    files_to_process = []
    skipped_count = 0
    
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
            
            # Calculate SHA1
            try:
                with open(file_path, 'rb') as f:
                    file_sha1 = hashlib.sha1(f.read()).hexdigest()
                
                # Skip if already processed (unless overwriting)
                if not overwrite and file_sha1 in existing_sha1s:
                    logger.debug(f"Skipping already processed file: {file_path}")
                    continue
                
                files_to_process.append((file_path, file_sha1))
            except Exception as e:
                logger.warning(f"Could not read file {file_path}: {e}")
                continue
    
    if not files_to_process:
        console.print("[yellow]No new files to process.[/yellow]")
        return
    
    # Show summary and confirm
    console.print(f"\n[bold]Ingestion Summary:[/bold]")
    console.print(f"  Database: {db_path}")
    console.print(f"  Table: {table}")
    console.print(f"  Files to process: {len(files_to_process)}")
    console.print(f"  Files skipped: {skipped_count}")
    console.print(f"  Already in database: {len(all_files) - len(files_to_process) - skipped_count}")
    
    if not yes:
        if not click.confirm("\nProceed with ingestion?", default=True):
            console.print("[yellow]Ingestion cancelled.[/yellow]")
            return
    
    # Process files
    console.print(f"\n[bold]Processing {len(files_to_process)} files...[/bold]")
    
    successful = 0
    failed = 0
    warnings = 0
    
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
        
        task = progress.add_task("Processing files...", total=len(files_to_process))
        
        # Process files with asyncio for better performance
        async def process_file_wrapper(file_info):
            file_path, file_sha1 = file_info
            try:
                sha1, content, metadata = await process_document(str(file_path), file_sha1, use_readability=readability, html_extractor=html_extractor, skip_garbage_check=skip_garbage_check)
                
                # Clean metadata
                metadata = clean_metadata(metadata)
                
                # Add manifest metadata if available
                manifest_metadata = get_file_metadata(str(file_path), manifest_data)
                if manifest_metadata:
                    # Merge manifest metadata with extracted metadata
                    # Manifest metadata takes precedence
                    metadata.update(manifest_metadata)
                    logger.debug(f"Added {len(manifest_metadata)} fields from manifest for {file_path.name}")
                
                # Insert into database
                insert_document(db, table, sha1, str(file_path), content, metadata)
                
                return True, None
            except SkippedFileException as e:
                return None, f"Skipped: {str(e)}"
            except Exception as e:
                return False, f"Error: {str(e)}"
        
        # Process files in batches to avoid overwhelming the system
        batch_size = 10
        for i in range(0, len(files_to_process), batch_size):
            if shutdown_requested:
                break
                
            batch = files_to_process[i:i+batch_size]
            
            # Process batch concurrently
            tasks = [process_file_wrapper(file_info) for file_info in batch]
            results = await asyncio.gather(*tasks)
            
            # Update progress and count results
            for (file_path, _), (success, error) in zip(batch, results):
                progress.update(task, advance=1)
                
                if success is True:
                    successful += 1
                    progress.console.print(f"[green]✓[/green] {file_path.name}")
                elif success is False:
                    failed += 1
                    progress.console.print(f"[red]✗[/red] {file_path.name}: {error}")
                else:  # None = skipped
                    warnings += 1
                    logger.debug(f"Skipped {file_path.name}: {error}")
            
            # WAL checkpoint after each batch
            if successful > 0 and (successful % 100) == 0:
                try:
                    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    logger.debug("Performed WAL checkpoint")
                except Exception as e:
                    logger.warning(f"WAL checkpoint failed: {e}")
    
    # Final WAL checkpoint
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.debug("Performed final WAL checkpoint")
    except Exception as e:
        logger.warning(f"Final WAL checkpoint failed: {e}")
    
    # Create FTS index if requested
    if fulltext and successful > 0:
        console.print("\n[bold]Creating full-text search index...[/bold]")
        setup_fts(db_path, table)
    
    # Show results
    console.print(f"\n[bold]Ingestion Complete![/bold]")
    console.print(f"  Successfully processed: [green]{successful}[/green]")
    if failed > 0:
        console.print(f"  Failed: [red]{failed}[/red]")
    if warnings > 0:
        console.print(f"  Warnings/Skipped: [yellow]{warnings}[/yellow]")
    
    # Provide helpful next steps
    if successful > 0:
        console.print(f"\n[bold]Next steps:[/bold]")
        console.print(f"  View your data: [cyan]sqlite-utils rows {db_path} {table} --limit 5[/cyan]")
        if fulltext:
            console.print(f"  Search content: [cyan]sqlite-utils search {db_path} {table} 'your search term'[/cyan]")
        console.print(f"  Enrich with LLMs: [cyan]doctrail enrich --help[/cyan]")