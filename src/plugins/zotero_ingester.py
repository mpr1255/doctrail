#!/usr/bin/env -S /opt/homebrew/bin/uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "typer",
#     "loguru",
#     "rich",
#     "pyzotero",
#     "sqlite-utils",
#     "sqlite-fts4",
# [Removed: tika dependency no longer needed]
#     "aiohttp",
#     "beautifulsoup4",
#     "chardet",
#     # --- Test Dependencies ---
#     "pytest",
#     "pytest-asyncio",
#     "pytest-mock",
#     "respx" # For mocking HTTP requests (like Zotero/Tika) if needed later
# ]
# ///

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Tuple
import hashlib
import json
import subprocess
import os

# Third-party imports
import typer
from loguru import logger
from pyzotero import zotero, zotero_errors
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn
import sqlite_utils
import aiohttp # Need these for the imported functions eventually
# Tika imports removed - zotero ingester no longer supports direct file processing

# --- Imports from .ingester ---
# Keep the metadata cleaner for now
# from .ingester import process_with_tika as ingest_file_with_tika # <-- Comment out or remove
from ..ingest import clean_metadata as ingest_clean_metadata
# logger.debug("Successfully imported processing functions from .ingester")
# --- End Imports ---

# Initialize Rich console
console = Console()

async def find_collection_id(zot: zotero.Zotero, collection_name: str) -> Optional[str]:
    """Find the collection ID for a given collection name."""
    logger.debug(f"Searching for collection ID for '{collection_name}'...")
    try:
        collections = await asyncio.to_thread(zot.collections)
        for coll in collections:
            if coll.get('data', {}).get('name') == collection_name:
                coll_id = coll.get('key')
                logger.info(f"Found collection '{collection_name}' with ID: {coll_id}")
                return coll_id
        logger.warning(f"Collection '{collection_name}' not found.")
        return None
    except Exception as e:
        logger.error(f"Error fetching collections: {e}")
        return None

async def download_and_extract_text_via_ingester(
    zot: zotero.Zotero,
    attachment_key: str,
    download_dir: Path,
    filename: str,
    verbose: bool = False
) -> Tuple[Optional[str], Optional[dict], Optional[Path], Optional[str]]:
    """
    File processing disabled - Tika dependency removed.
    Downloads attachments but does not extract text content.
    """
    # File processing disabled - Tika dependency removed
    logger.warning("Zotero text extraction disabled - Tika dependency removed from Doctrail")
    logger.warning("Files will be downloaded but text content will not be extracted")
    
    download_path = None
    error_message = "Text extraction disabled - use main doctrail ingest command instead"
    extracted_text = None
    metadata = None
    
    return extracted_text, metadata, download_path, error_message

async def process_item_for_fulltext(
    zot: zotero.Zotero,
    item: Dict,
    download_dir: Path,
    verbose: bool
) -> List[Dict]:
    """
    Processes a single Zotero item (potential parent).
    Finds PDF/HTML attachments, downloads them, and extracts their text using Tika.
    Returns a list of dictionaries, each containing info about a processed attachment.
    Example return: [{'attachment_key': 'ABC', 'filename': 'file.pdf', 'downloaded_path': Path(...), 'full_text': '...', 'error': None}]
    """
    item_data = item.get('data', {})
    item_key = item_data.get('key')
    item_type = item_data.get('itemType', 'N/A')

    # Skip attachments themselves or items without keys
    if not item_key or item_type == 'attachment':
        return []

    processed_attachments_data = []
    attachments_processed_count = 0 # Local counter for logging

    try:
        # Find children (attachments)
        children = await asyncio.to_thread(zot.children, item_key)
        logger.debug(f"Item {item_key} has {len(children)} children.")

        relevant_attachments = []
        for child in children:
            child_data = child.get('data', {})
            child_type = child_data.get('itemType')
            link_mode = child_data.get('linkMode')
            content_type = child_data.get('contentType', '').lower()
            filename = child_data.get('filename', '')

            if child_type == 'attachment' and link_mode in ['imported_file', 'imported_url']:
                 if 'pdf' in content_type or 'html' in content_type or \
                    filename.lower().endswith(('.pdf', '.html')):
                      relevant_attachments.append(child)

        if not relevant_attachments:
            logger.debug(f"No relevant downloadable PDF/HTML attachments found for item {item_key}")
            return []

        # Download and extract text for each relevant attachment
        for attachment in relevant_attachments:
            attachments_processed_count += 1
            attachment_key = attachment.get('key')
            attachment_data = {
                "attachment_key": attachment_key,
                "filename": attachment.get('data', {}).get('filename', 'N/A'),
                "full_text": None,
                "tika_metadata": None,
                "downloaded_path": None,
                "error": None
            }
            logger.info(f"Processing attachment {attachment_key} ('{attachment_data['filename']}') for parent {item_key}")

            extracted_text, tika_metadata, downloaded_path, error_msg = await download_and_extract_text_via_ingester(
                zot, attachment_key, download_dir, attachment_data['filename'], verbose
            )

            attachment_data["downloaded_path"] = str(downloaded_path) if downloaded_path else None
            attachment_data["error"] = error_msg
            attachment_data["full_text"] = extracted_text
            attachment_data["tika_metadata"] = tika_metadata

            if extracted_text:
                 logger.info(f"Successfully extracted text for attachment {attachment_key}")
            elif downloaded_path and not error_msg:
                 logger.warning(f"No text content extracted via ingester for attachment {attachment_key}, though download succeeded.")

            processed_attachments_data.append(attachment_data)

    except Exception as e:
        logger.error(f"Error processing children/attachments for item {item_key}: {e}", exc_info=verbose)

    logger.debug(f"Finished processing attachments for item {item_key}. Got text for {len([a for a in processed_attachments_data if a.get('full_text')])} / {attachments_processed_count} attachments.")
    return processed_attachments_data

async def process_zotero_ingest(
    db_path: str,
    api_key: str,
    library_id: str,
    library_type: str,
    collection_name: str,
    download_dir: str = "/tmp/doctrail/zotero",
    verbose: bool = False,
    overwrite: bool = False,
    fulltext: bool = False
):
    """
    DEPRECATED: Zotero ingester disabled due to Tika dependency removal.
    
    Use the main doctrail ingest command instead:
    1. Download your Zotero files manually or use Zotero sync
    2. Run: uv run doctrail.py ingest /path/to/your/files
    """
    # Early exit with deprecation notice
    console.print(Panel.fit(
        "[bold red]⚠️ ZOTERO INGESTER DISABLED[/bold red]\n\n"
        "The Zotero ingester has been disabled due to the removal of the Tika dependency.\n\n"
        "[bold]Recommended approach:[/bold]\n"
        "1. Export your Zotero files manually or use Zotero sync\n"
        "2. Run the main doctrail ingest command:\n"
        "   [cyan]uv run doctrail.py ingest /path/to/your/files[/cyan]\n\n"
        "[dim]This provides better file type support with specialized extractors.[/dim]",
        title="⛔ Feature Disabled", border_style="red"
    ))
    logger.error("Zotero ingester disabled - use main doctrail ingest command instead")
    return

    log_level = "DEBUG" if verbose else "INFO"
    logger.remove()
    logger.add(sys.stderr, level=log_level)
    log_dir = Path("/tmp/doctrail_logs")
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = log_dir / f"doctrail_zotero_ingest_{timestamp}.log"
    logger.add(str(log_file), level="DEBUG", rotation="100 MB")
    logger.info(f"Detailed Zotero ingest logs will be written to {log_file}")

    console.print(Panel.fit(
        f"[bold]SQLite Enricher - Zotero Ingest (File Download Mode)[/bold]\n\n"
        f"Ingesting items from collection:\n"
        f"- Library ID: [cyan]{library_id}[/cyan] (Type: {library_type})\n"
        f"- Collection: [cyan]{collection_name}[/cyan]\n"
        f"Into Database: [cyan]{db_path}[/cyan] (Table: '{table_name}')\n"
        f"Attachment Download Dir: [cyan]{download_path_obj}[/cyan]\n"
        f"Overwrite existing: {'[bold yellow]Yes[/bold yellow]' if overwrite else 'No'}",
        title="🚚 Zotero Ingest Run", border_style="blue"
    ))

    existing_keys_with_content = set() # Store keys that *definitely* have content
    tika_process = None # Initialize tika_process

    try:
        # --- Start Tika Server ---
        logger.info("Starting Apache Tika server for document parsing with OCR disabled...")
        console.print("[yellow]Starting Apache Tika server with OCR disabled...[/yellow]")

        # Construct path to tika-config.properties relative to this script's location
        # Assumes zotero_ingester.py is in src/ and tika-config.properties is in the root
        script_dir = Path(__file__).parent
        config_file_path = script_dir.parent / "tika-config.properties"

        if not config_file_path.exists():
             logger.warning(f"Tika config file not found at {config_file_path}. OCR might not be disabled.")
             config_param = []
        else:
             config_param = [f'--config={str(config_file_path)}']
             logger.info(f"Using Tika config: {config_file_path}")


        # Set environment variables to disable OCR explicitly (belt and suspenders)
        tika_env = os.environ.copy()
        tika_env['TIKA_DISABLE_OCR'] = 'true'
        tika_env['TIKA_OCR_STRATEGY'] = 'no_ocr'

        try:
            # Start Tika server as a subprocess
            tika_command = ['tika', '-s'] + config_param # '-s' flag for server mode
            tika_process = subprocess.Popen(
                tika_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=tika_env # Pass the modified environment
            )
            # Give Tika a moment to start up
            await asyncio.sleep(5) # Adjust sleep time if needed
            logger.info(f"Tika server process started (PID: {tika_process.pid}).")

            # --- Set TikaClientOnly HERE ---
            tika.TikaClientOnly = True
            logger.info("Set tika.TikaClientOnly = True")
            # --- End Set TikaClientOnly ---

            # Optional: Test connection explicitly if desired
            # try:
            #     tika_parser.from_buffer("test")
            #     logger.info("Tika server connection test successful.")
            # except Exception as tika_test_e:
            #     logger.error(f"Tika server connection test failed: {tika_test_e}. Aborting.", exc_info=True)
            #     raise ConnectionRefusedError("Tika server failed to start or respond.") from tika_test_e

        except FileNotFoundError:
             logger.error("Tika command not found. Please ensure Apache Tika is installed and in your PATH.")
             raise
        except Exception as e:
             logger.error(f"Failed to start Tika server: {e}", exc_info=True)
             raise # Re-raise to stop the process
        # --- End Tika Server Start ---


        db = sqlite_utils.Database(db_path)
        # Ensure table exists and potentially add new column
        if not db[table_name].exists():
             db[table_name].create({
                 "zotero_key": str,
                 "item_type": str,
                 "title": str,
                 "authors": str,
                 "date": str,
                 "publication_title": str,
                 "doi": str,
                 "zotero_metadata": str,
                 "ingested_at": str,
                 "collection_name": str,
                 "content": str,
                 "attachment_key": str,
                 "attachment_filename": str,
                 "downloaded_file_path": str
             }, pk="zotero_key")
        else:
             # Add column if it doesn't exist, handling potential errors
             try:
                 db[table_name].add_column("downloaded_file_path", str)
                 logger.info(f"Added 'downloaded_file_path' column to '{table_name}'.")
             except sqlite_utils.db.AlterError as ae:
                  if "duplicate column name" in str(ae).lower():
                      logger.debug("'downloaded_file_path' column already exists.")
                  else: raise
             except Exception as e:
                  logger.warning(f"Could not check/add 'downloaded_file_path' column: {e}")

        zotero_table = db[table_name]
        logger.info(f"Connected to database: {db_path}")
        logger.info(f"Ensured table '{table_name}' exists with 'downloaded_file_path' column.")

        # --- Modified Existing Key Check ---
        if not overwrite:
            if zotero_table.exists() and "content" in zotero_table.columns_dict:
                try:
                    # Fetch keys where content is NOT NULL and not empty
                    query = f"SELECT zotero_key FROM {db.quote(table_name)} WHERE content IS NOT NULL AND content != ''"
                    existing_keys_with_content = {row[0] for row in db.query(query)}
                    if existing_keys_with_content:
                        logger.info(f"Found {len(existing_keys_with_content)} existing Zotero keys with content in DB. Will skip these.")
                    else:
                        logger.info(f"Table '{table_name}' exists but no entries found with content.")
                except Exception as e:
                     logger.error(f"Error fetching existing keys with content from DB: {e}. Processing all items.", exc_info=True)
                     # If query fails, don't skip anything - effectively overwrite=True for safety
                     overwrite = True # Force processing if DB check fails
            else:
                 logger.info(f"Table '{table_name}' does not exist or missing 'content' column. Processing all items.")
        else:
            logger.info("Overwrite flag is set, will re-process all items and overwrite existing entries.")
        # --- End Modified Check ---

        # Now initialize Zotero Client
        logger.info("Initializing Zotero API client...")
        zot = await asyncio.to_thread(zotero.Zotero, library_id, library_type, api_key)
        await asyncio.to_thread(zot.key_info)
        logger.info("Zotero client initialized and connection verified.")

        # Find collection ID
        collection_id = await find_collection_id(zot, collection_name)
        if not collection_id:
            console.print(f"[red]Error:[/red] Could not find collection named '{collection_name}'.")
            return

        try:
             total_items_in_collection = await asyncio.to_thread(zot.num_collectionitems, collection_id)
             console.print(f"Collection '{collection_name}' contains approximately {total_items_in_collection} items.")
             logger.info(f"Collection {collection_id} has {total_items_in_collection} items.")
        except Exception as e:
             logger.warning(f"Could not get item count for collection {collection_id}: {e}")
             total_items_in_collection = 0
        if total_items_in_collection == 0:
             try:
                 await asyncio.to_thread(zot.collection, collection_id)
                 logger.info(f"Collection '{collection_name}' exists but reported 0 items.")
             except zotero_errors.ResourceNotFound:
                  console.print(f"[red]Error:[/red] Collection '{collection_name}' (ID: {collection_id}) not found.")
                  return
             except Exception as e_coll_check: logger.warning(f"Error during secondary check: {e_coll_check}")
             logger.warning("Item count is 0, attempting fetch.")

        processed_item_count = 0
        skipped_existing_count = 0
        items_with_content_count = 0
        items_upserted_count = 0
        attachments_processed_count = 0
        current_page = 0

        console.print(f"\nFetching items, downloading attachments, and processing content for '{collection_name}'...")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=False
        ) as progress:

            task_description = f"Ingesting items from '{collection_name}'"
            main_task = progress.add_task(task_description, total=total_items_in_collection or None)

            more_items = True
            items_page = []
            fetched_count_this_page = 0
            try:
                logger.debug("Fetching first page of collection items...")
                items_page = await asyncio.to_thread(zot.collection_items, collection_id, limit=fetch_limit)
                fetched_count_this_page = len(items_page)
                logger.info(f"Fetched page {current_page+1} with {fetched_count_this_page} items.")
                current_page += 1
                if not items_page:
                    more_items = False
            except Exception as e:
                logger.error(f"Error fetching initial page: {e}", exc_info=True)
                console.print(f"[red]Error fetching initial page: {e}[/red]")
                items_page = []
                more_items = False

            while more_items:
                if items_page:
                    records_to_upsert = []
                    item_processing_tasks = []

                    for item in items_page:
                         if not isinstance(item, dict):
                            logger.warning(f"Skipping non-dict item in page: {type(item)}")
                            continue

                         item_key = item.get('key', 'UNKNOWN_KEY')

                         # --- Use the Modified Skip Logic ---
                         if not overwrite and item_key in existing_keys_with_content:
                            logger.debug(f"Skipping existing item with content: {item_key}")
                            skipped_existing_count += 1
                            progress.update(main_task, advance=1, description=f"{task_description} ({processed_item_count+skipped_existing_count} attempted)")
                            continue
                         # --- End Skip Logic ---

                         # If not skipped, create processing task
                         item_processing_tasks.append(
                              process_single_item(zot, item, download_path_obj, verbose, collection_name)
                         )

                    if item_processing_tasks:
                        logger.debug(f"Processing {len(item_processing_tasks)} items concurrently for page {current_page}...")
                        results = await asyncio.gather(*item_processing_tasks, return_exceptions=True)

                        for result in results:
                            processed_item_count += 1
                            if isinstance(result, Exception):
                                logger.error(f"Error processing an item fully: {result}", exc_info=verbose)
                            elif result:
                                records_to_upsert.append(result)
                                if result.get('content'):
                                     items_with_content_count += 1
                                if result.get('downloaded_file_path'):
                                    attachments_processed_count += 1
                            progress.update(main_task, advance=1, description=f"{task_description} ({processed_item_count+skipped_existing_count} attempted)")
                            await asyncio.sleep(0.01)

                    if records_to_upsert:
                        try:
                           logger.debug(f"Upserting {len(records_to_upsert)} combined records into '{table_name}'...")
                           column_types = {'zotero_metadata': str, 'tika_metadata': str, 'content': str}
                           zotero_table.upsert_all(
                               records_to_upsert,
                               pk='zotero_key',
                               alter=True, # Ensures columns like tika_metadata are added
                               column_order=['zotero_key', 'title', 'authors', 'date', 'content', 'downloaded_file_path'],
                               columns=column_types
                           )
                           items_upserted_count += len(records_to_upsert)
                           logger.info(f"Upserted {len(records_to_upsert)} records from page {current_page}.")
                        except Exception as db_e:
                            logger.error(f"Database error upserting combined batch: {db_e}", exc_info=True)
                            console.print(f"[bold red]DB Error:[/bold red] {db_e}")
                    else:
                         logger.debug("No items in the current page buffer to process.")

                if fetched_count_this_page < fetch_limit:
                    logger.info(f"Last fetch returned {fetched_count_this_page} items (limit {fetch_limit}), assuming end of collection.")
                    more_items = False
                    items_page = []
                elif more_items:
                    try:
                        logger.debug("Attempting to fetch next page using follow()...")
                        next_page_items = await asyncio.to_thread(zot.follow)
                        fetched_count_this_page = len(next_page_items)
                        if not next_page_items:
                            logger.info("follow() returned empty list. End of collection.")
                            more_items = False; items_page = []
                        else:
                            logger.info(f"Fetched page {current_page+1} with {fetched_count_this_page} items.")
                            items_page = next_page_items; current_page += 1
                            await asyncio.sleep(0.2)
                    except StopIteration:
                         logger.info("follow() raised StopIteration. End of collection.")
                         more_items = False; items_page = []
                    except Exception as e:
                         logger.error(f"Error calling follow() for next page: {e}", exc_info=True)
                         console.print(f"[red]Error fetching next page: {e}[/red]")
                         more_items = False; items_page = []
            progress.update(main_task, completed=processed_item_count+skipped_existing_count, total=processed_item_count+skipped_existing_count or None, description=f"{task_description} (Complete)")
            try:
                if items_upserted_count > 0 and fulltext:
                    logger.info("Ensuring FTS is enabled on 'content' and 'tika_metadata' columns...")
                    # Drop existing FTS table and triggers first for clean recreation
                    # This handles cases where the schema might have changed slightly
                    fts_table_name = f"{table_name}_fts"
                    for trigger_suffix in ['ai', 'au', 'ad']:
                         trigger_name = f"{table_name}_{trigger_suffix}"
                         db.execute(f"DROP TRIGGER IF EXISTS {db.quote(trigger_name)};")
                    db.execute(f"DROP TABLE IF EXISTS {db.quote(fts_table_name)};")
                    logger.debug(f"Dropped existing FTS table/triggers for {table_name} if they existed.")

                    # Now enable FTS cleanly
                    zotero_table.enable_fts(['content', 'tika_metadata'], create_triggers=True)
                    logger.info("FTS enabled successfully.")
                elif items_upserted_count > 0:
                    logger.info("Skipping FTS index creation (use --fulltext to enable)")
            except Exception as fts_e:
                 logger.error(f"Failed to enable FTS index: {fts_e}", exc_info=verbose)

            console.print("\n--- Ingest Complete ---")
            console.print(Panel.fit(
                f"[bold]Zotero Full Ingest Summary[/bold]\n\n"
                f"Collection:            [cyan]{collection_name}[/cyan]\n"
                f"Items Attempted:       [cyan]{processed_item_count+skipped_existing_count}[/cyan]\n"
                f"Items Skipped (Exists):[yellow]{skipped_existing_count}[/yellow]\n"
                f"Items Processed:       [cyan]{processed_item_count}[/cyan]\n"
                f"Attachments Processed: [cyan]{attachments_processed_count}[/cyan]\n"
                f"Items w/ Extracted Txt:[green]{items_with_content_count}[/green]\n"
                f"Records Upserted:      [blue]{items_upserted_count}[/blue] -> '{table_name}'\n\n"
                f"Database: [cyan]{db_path}[/cyan]\n"
                f"Attachment Downloads: [cyan]{download_path_obj}[/cyan]\n"
                f"Detailed logs: [cyan]{log_file}[/cyan]",
                title="📊 Ingest Summary", border_style="green"
            ))

    except zotero_errors.ResourceNotFound as e:
         logger.error(f"Zotero API Error: Resource not found (check library/collection ID?): {e}", exc_info=True)
         console.print(f"[red]Zotero API Error:[/red] Resource not found.")
    except zotero_errors.AuthenticationError as e:
         logger.error(f"Zotero API Error: Authentication failed (check API key?): {e}", exc_info=True)
         console.print(f"[red]Zotero API Error:[/red] Authentication failed.")
    except Exception as e:
        logger.error(f"An unexpected error occurred during full ingest: {e}", exc_info=True)
        console.print(f"[red]Error:[/red] An unexpected error occurred: {e}")

    # --- Tika Server Shutdown ---
    finally:
        if tika_process and tika_process.poll() is None: # Check if process exists and is running
            logger.info(f"Shutting down Tika server process (PID: {tika_process.pid})...")
            tika_process.terminate()
            try:
                tika_process.wait(timeout=10) # Wait for graceful shutdown
                logger.info("Tika server terminated.")
            except subprocess.TimeoutExpired:
                logger.warning("Tika server did not terminate gracefully after 10s, forcing kill.")
                tika_process.kill()
                logger.info("Tika server killed.")
            except Exception as e:
                 logger.error(f"Error during Tika shutdown: {e}")
        elif tika_process:
            logger.info("Tika server process already terminated.")
        else:
            logger.debug("No Tika server process was started by this script.")
    # --- End Tika Server Shutdown ---

async def process_single_item(
    zot: zotero.Zotero,
    item: Dict,
    download_dir: Path,
    verbose: bool,
    collection_name: str
) -> Optional[Dict]:
    """
    Processes a single Zotero item: downloads attachments, extracts text, builds record.
    Designed to be run concurrently. Returns the record dict or None if skipped/error.
    """
    item_key = item.get('key', 'UNKNOWN_KEY')
    item_data = item.get('data', {})

    # Skip attachments themselves
    if item_data.get('itemType') == 'attachment':
        logger.debug(f"Skipping attachment item {item_key} during processing.")
        return None

    record = {}
    try:
        # This call returns a list of dicts, one for each processed attachment
        attachments_data = await process_item_for_fulltext(zot, item, download_dir, verbose)

        authors_list = []
        for c in item_data.get('creators', []):
            name = ""
            if c.get('lastName') and c.get('firstName'): name = f"{c.get('firstName')} {c.get('lastName')}"
            elif c.get('lastName'): name = c['lastName']
            elif c.get('firstName'): name = c['firstName']
            elif c.get('name'): name = c['name']
            if name: authors_list.append(name.strip())

        # Base record
        record = {
            'zotero_key': item_key,
            'item_type': item_data.get('itemType'),
            'title': item_data.get('title'),
            'authors': "; ".join(authors_list) if authors_list else None,
            'date': item_data.get('date'),
            'publication_title': item_data.get('publicationTitle'),
            'doi': item_data.get('DOI'),
            'zotero_metadata': json.dumps(item_data),
            'ingested_at': datetime.utcnow().isoformat(),
            'collection_name': collection_name,
            'content': None,
            'tika_metadata': None, # Add tika_metadata field
            'attachment_key': None,
            'attachment_filename': None,
            'downloaded_file_path': None
        }

        # --- Update population logic ---
        # Find the first attachment with extracted text
        first_successful_attachment = next((att for att in attachments_data if att.get('full_text')), None)
        # If none with text, find the first one that was downloaded (even if extraction failed)
        first_downloaded_attachment = next((att for att in attachments_data if att.get('downloaded_path')), None)

        if first_successful_attachment:
            logger.debug(f"Using attachment {first_successful_attachment.get('attachment_key')} for content/metadata.")
            record['content'] = first_successful_attachment.get('full_text')
            # --- Store tika_metadata correctly ---
            tika_meta = first_successful_attachment.get('tika_metadata')
            record['tika_metadata'] = json.dumps(tika_meta) if tika_meta else None # Store as JSON string
            # --- End store tika_metadata ---
            record['attachment_key'] = first_successful_attachment.get('attachment_key')
            record['attachment_filename'] = first_successful_attachment.get('filename')
            record['downloaded_file_path'] = first_successful_attachment.get('downloaded_path')
        elif first_downloaded_attachment:
             # Store info about the first downloaded file, even if text extraction failed
             logger.warning(f"Item {item_key} had attachments downloaded but none yielded text. Storing path and key for first download: {first_downloaded_attachment.get('attachment_key')}.")
             record['attachment_key'] = first_downloaded_attachment.get('attachment_key')
             record['attachment_filename'] = first_downloaded_attachment.get('filename')
             record['downloaded_file_path'] = first_downloaded_attachment.get('downloaded_path')
             # Ensure content and tika_metadata are explicitly None
             record['content'] = None
             record['tika_metadata'] = None
        else:
             logger.debug(f"No relevant attachments processed for item {item_key}")
        # --- End update population logic ---

        return record

    except Exception as e:
        logger.error(f"Error processing item {item_key}: {e}", exc_info=True)
        return None

# Example of how to run directly (for testing)
# if __name__ == "__main__":
#     # Requires environment variables ZOTERO_API_KEY, ZOTERO_LIBRARY_ID
#     # or a config file logic added here
#     async def main():
#         await process_zotero_ingest(
#             api_key=os.environ['ZOTERO_API_KEY'],
#             library_id=os.environ['ZOTERO_LIBRARY_ID'],
#             library_type='user', # or 'group'
#             collection_name="anthro", # Your collection name
#             verbose=True
#         )
#     asyncio.run(main()) 