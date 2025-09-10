#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyzotero",
#     "sqlite-utils",
#     "loguru",
#     "rich",
#     "tqdm",
#     "aiofiles",
# ]
# ///

"""Zotero Plugin - Ingests academic literature from Zotero collections"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, Tuple
import re

from loguru import logger
from pyzotero import zotero
from rich.console import Console
from rich.panel import Panel
from tqdm import tqdm
import sqlite_utils

# Import the process_document function from ingest package
from ..ingest import process_document

console = Console()


class Plugin:
    """Zotero literature connector for ingesting academic papers from Zotero collections"""
    
    @property
    def name(self) -> str:
        return "zotero"
    
    @property
    def description(self) -> str:
        return "Ingest academic literature from Zotero collections with full text extraction"
    
    @property
    def target_table(self) -> str:
        return getattr(self, '_table_name', 'literature')  # Dynamic table name with default
    
    async def ingest(
        self,
        db_path: str,
        config: Dict,
        verbose: bool = False,
        overwrite: bool = False,
        limit: Optional[int] = None,
        collection: Optional[str] = None,
        table: Optional[str] = None,
        api_key: Optional[str] = None,
        user_id: Optional[str] = None,
        zotero_dir: Optional[str] = None,
        **kwargs
    ) -> Dict[str, int]:
        """
        Ingest documents from Zotero collection.
        
        Args:
            db_path: Target database path (REQUIRED)
            config: Configuration dictionary
            collection: Zotero collection name (REQUIRED)
            table: Target table name (default: 'literature')
            api_key: Zotero API key (or from env/config)
            user_id: Zotero user ID (or from env/config)
            zotero_dir: Path to Zotero data directory (default: ~/Zotero)
            
        Returns:
            Stats dictionary
        """
        # Validate required parameters
        if not collection:
            raise ValueError(
                "ERROR: --collection is required for the Zotero literature plugin.\n\n"
                "Please specify the name of the Zotero collection to import.\n\n"
                "Example:\n"
                "  ./doctrail.py ingest --plugin zotero --collection 'My Research' "
                "--db-path ./literature.db\n"
            )
        
        # Check if the database directory exists
        db_path_obj = Path(db_path)
        db_dir = db_path_obj.parent
        
        # If parent is empty (e.g., just "test.db"), use current directory
        if str(db_dir) == ".":
            db_dir = Path.cwd()
        
        if not db_dir.exists():
            raise ValueError(
                f"ERROR: Directory does not exist: {db_dir}\n\n"
                f"Please create the directory first or specify a valid path.\n\n"
                f"Example:\n"
                f"  mkdir -p {db_dir}\n"
                f"  ./doctrail.py ingest --plugin zotero --collection '{collection}' --db-path {db_path}\n"
            )
        
        # Set default table name
        if not table:
            table = "literature"
        
        # Override target_table property with CLI parameter
        self._table_name = table
        
        # Set up logging
        log_level = "DEBUG" if verbose else "INFO"
        logger.remove()
        logger.add(sys.stderr, level=log_level)
        
        # Get Zotero credentials
        final_api_key = api_key or config.get('zotero_api_key') or os.environ.get('ZOTERO_API_KEY')
        final_user_id = user_id or config.get('zotero_user_id') or os.environ.get('ZOTERO_USER_ID')
        
        if not final_api_key or not final_user_id:
            # Try to load from the .env file
            env_path = Path("/Users/m/libs/zotero_enricher/.env")
            if env_path.exists():
                logger.info(f"Loading Zotero credentials from {env_path}")
                with open(env_path) as f:
                    for line in f:
                        if line.startswith('ZOTERO_API_KEY='):
                            final_api_key = line.split('=', 1)[1].strip().strip('"')
                        elif line.startswith('ZOTERO_USER_ID='):
                            final_user_id = line.split('=', 1)[1].strip().strip('"')
        
        if not final_api_key or not final_user_id:
            raise ValueError(
                "ERROR: Zotero API credentials not found.\n\n"
                "Please provide credentials via:\n"
                "1. Command line: --api-key and --user-id\n"
                "2. Config file: zotero_api_key and zotero_user_id\n"
                "3. Environment: ZOTERO_API_KEY and ZOTERO_USER_ID\n"
                "4. .env file at /Users/m/libs/zotero_enricher/.env\n"
            )
        
        # Set default Zotero directory - try common locations
        if zotero_dir:
            final_zotero_dir = Path(zotero_dir)
        else:
            # Try common Zotero locations
            possible_locations = [
                Path.home() / "docs" / "Zotero",
                Path.home() / "Zotero",
                Path.home() / "Documents" / "Zotero"
            ]
            for location in possible_locations:
                if (location / "storage").exists():
                    final_zotero_dir = location
                    break
            else:
                # Default to first option
                final_zotero_dir = possible_locations[0]
        
        storage_dir = final_zotero_dir / "storage"
        
        if not storage_dir.exists():
            raise ValueError(
                f"ERROR: Zotero storage directory not found at {storage_dir}\n\n"
                f"Please specify the correct path with --zotero-dir\n"
                f"Default location: ~/Zotero\n"
            )
        
        logger.info(f"Zotero Literature Plugin starting")
        logger.info(f"Collection: {collection}")
        logger.info(f"Target table: {table}")
        logger.info(f"Zotero storage: {storage_dir}")
        
        # Connect to Zotero API
        zot = zotero.Zotero(final_user_id, 'user', final_api_key)
        
        # Verify connection
        try:
            zot.key_info()
            logger.info("Zotero API connection verified")
        except Exception as e:
            raise ConnectionError(f"Failed to connect to Zotero API: {e}")
        
        # Find collection by name
        collection_id = await self._find_collection_id(zot, collection)
        if not collection_id:
            # Get available collections for helpful error
            all_collections = zot.collections()
            collection_names = [c['data']['name'] for c in all_collections]
            raise ValueError(
                f"Collection '{collection}' not found.\n\n"
                f"Available collections: {', '.join(collection_names) if collection_names else 'None'}"
            )
        
        logger.info(f"Found collection ID: {collection_id}")
        
        # Get items in collection
        all_items = []
        start = 0
        limit_per_request = 100
        
        while True:
            batch = zot.collection_items(collection_id, start=start, limit=limit_per_request)
            if not batch:
                break
            all_items.extend(batch)
            start += len(batch)
        
        # Filter to only primary items (not attachments, notes, etc.)
        items = []
        for item in all_items:
            item_type = item['data']['itemType']
            # Skip attachments, notes, and other non-primary items
            if item_type not in ['attachment', 'note', 'annotation']:
                items.append(item)
        
        # Apply limit to filtered items
        if limit and len(items) > limit:
            items = items[:limit]
        
        logger.info(f"Found {len(all_items)} total items, {len(items)} primary items (papers/books/etc)")
        
        # Ensure database schema before checking existing items
        self._ensure_literature_schema(db_path)
        
        # Connect to database
        db = sqlite_utils.Database(db_path)
        
        # OPTIMIZATION: Check which items already exist in the database
        items_to_process = items
        skipped_existing = 0
        
        if not overwrite:
            # Get all existing zotero_keys from the database
            existing_keys = set()
            try:
                for row in db[self.target_table].rows_where(select="zotero_key"):
                    existing_keys.add(row['zotero_key'])
                logger.info(f"Found {len(existing_keys)} existing items in database")
                
                # Filter to only new items
                items_to_process = [item for item in items if item['key'] not in existing_keys]
                skipped_existing = len(items) - len(items_to_process)
                
                if skipped_existing > 0:
                    logger.info(f"Skipping {skipped_existing} items that already exist in database")
            except Exception as e:
                logger.warning(f"Could not check existing items: {e}")
                # Continue with all items if we can't check
        
        # Show summary
        console.print(Panel.fit(
            f"[bold]Zotero Literature Ingestion[/bold]\n\n"
            f"Collection: [cyan]{collection}[/cyan]\n"
            f"Papers/Books: [cyan]{len(items)}[/cyan] (out of {len(all_items)} total items)\n"
            f"New to process: [green]{len(items_to_process)}[/green]\n"
            f"Already in DB: [yellow]{skipped_existing}[/yellow]\n"
            f"Target: [cyan]{db_path}[/cyan]\n"
            f"Table: [cyan]{table}[/cyan]\n"
            f"Storage: [cyan]{storage_dir}[/cyan]\n"
            f"Overwrite: [cyan]{'Yes' if overwrite else 'No'}[/cyan]",
            title="📚 Literature Ingestion",
            border_style="blue"
        ))
        
        # If no items to process, exit early
        if not items_to_process:
            logger.info("No new items to process")
            console.print(Panel.fit(
                f"[bold]Nothing to do![/bold]\n\n"
                f"All {len(items)} items already exist in the database.\n"
                f"Use --overwrite to re-process existing items.",
                title="✅ Complete",
                border_style="green"
            ))
            
            return {
                "total": len(items),
                "success_count": 0,
                "error_count": 0,
                "skipped_count": skipped_existing,
                "no_attachment_count": 0
            }
        
        # OPTIMIZATION: Fetch all attachments and BibTeX entries upfront
        logger.info(f"Fetching metadata for {len(items_to_process)} new items from Zotero API...")
        items_with_metadata = []
        
        # Create a semaphore to limit concurrent API calls
        semaphore = asyncio.Semaphore(20)
        
        async def fetch_item_metadata(item):
            """Fetch attachments and BibTeX for a single item"""
            async with semaphore:
                item_key = item['key']
                
                # Get attachments for this item
                try:
                    # Run synchronous API call in executor
                    loop = asyncio.get_event_loop()
                    attachments = await loop.run_in_executor(None, zot.children, item_key)
                except Exception as e:
                    logger.warning(f"Failed to fetch attachments for {item_key}: {e}")
                    attachments = []
                
                # Get BibTeX entry
                try:
                    loop = asyncio.get_event_loop()
                    bibtex_entry = await loop.run_in_executor(None, self._get_bibtex_entry, zot, item_key)
                except Exception as e:
                    logger.warning(f"Failed to fetch BibTeX for {item_key}: {e}")
                    bibtex_entry = ""
                
                return {
                    'item': item,
                    'attachments': attachments,
                    'bibtex_entry': bibtex_entry
                }
        
        # Fetch all metadata in parallel
        with tqdm(total=len(items_to_process), desc="Fetching attachments") as pbar:
            async def fetch_with_progress(item):
                pbar.set_description(f"Fetching: {item['data'].get('title', 'Unknown')[:50]}")
                result = await fetch_item_metadata(item)
                pbar.update(1)
                return result
            
            # Run all fetches concurrently
            items_with_metadata = await asyncio.gather(*[fetch_with_progress(item) for item in items_to_process])
        
        logger.info(f"Metadata fetched for {len(items_with_metadata)} items")
        
        # Process statistics
        success_count = 0
        error_count = 0
        skipped_count = 0
        no_attachment_count = 0
        
        # Process each item locally (no more API calls)
        with tqdm(total=len(items_with_metadata), desc="Processing items locally") as pbar:
            for item_data in items_with_metadata:
                item = item_data['item']
                pbar.set_description(f"Processing: {item['data'].get('title', 'Unknown')[:50]}")
                
                try:
                    # Process this item with pre-fetched metadata
                    result = await self._process_item_local(
                        item_data, storage_dir, db, overwrite, verbose
                    )
                    
                    if result['status'] == 'success':
                        success_count += 1
                    elif result['status'] == 'skipped':
                        skipped_count += 1
                    elif result['status'] == 'no_attachment':
                        no_attachment_count += 1
                    else:
                        error_count += 1
                    
                except Exception as e:
                    logger.error(f"Error processing item {item['key']}: {e}")
                    error_count += 1
                
                pbar.update(1)
        
        # Final summary
        total_skipped = skipped_count + skipped_existing
        
        console.print(Panel.fit(
            f"[bold]Ingestion Complete[/bold]\n\n"
            f"Total papers in collection: [cyan]{len(items)}[/cyan]\n"
            f"Already in database: [yellow]{skipped_existing}[/yellow]\n"
            f"Papers processed: [cyan]{len(items_with_metadata)}[/cyan]\n"
            f"Successfully ingested: [green]{success_count}[/green]\n"
            f"Skipped (file exists): [yellow]{skipped_count}[/yellow]\n"
            f"No PDF/text found: [dim]{no_attachment_count}[/dim]\n"
            f"Errors: [red]{error_count}[/red]",
            title="✅ Complete",
            border_style="green"
        ))
        
        return {
            "total": len(items),
            "success_count": success_count,
            "error_count": error_count,
            "skipped_count": total_skipped,
            "no_attachment_count": no_attachment_count
        }
    
    async def _find_collection_id(self, zot: zotero.Zotero, collection_name: str) -> Optional[str]:
        """Find collection ID by name"""
        collections = zot.collections()
        for coll in collections:
            if coll['data']['name'] == collection_name:
                return coll['key']
        return None
    
    async def _process_item_local(
        self,
        item_data: Dict,
        storage_dir: Path,
        db: sqlite_utils.Database,
        overwrite: bool,
        verbose: bool
    ) -> Dict:
        """Process a single Zotero item using pre-fetched metadata"""
        item = item_data['item']
        attachments = item_data['attachments']
        bibtex_entry = item_data['bibtex_entry']
        
        item_key = item['key']
        data = item['data']
        
        # Find PDF/HTML attachments
        relevant_attachments = []
        for attachment in attachments:
            if attachment['data']['itemType'] != 'attachment':
                continue
            
            link_mode = attachment['data'].get('linkMode', '')
            if link_mode not in ['imported_file', 'imported_url']:
                continue
            
            content_type = attachment['data'].get('contentType', '').lower()
            filename = attachment['data'].get('filename', '')
            
            if any(x in content_type for x in ['pdf', 'html']) or \
               any(filename.lower().endswith(ext) for ext in ['.pdf', '.html', '.htm', '.mhtml', '.mht']):
                relevant_attachments.append(attachment)
        
        # If no attachments but has abstract, still process
        abstract = data.get('abstractNote', '')
        if not relevant_attachments and not abstract:
            return {'status': 'no_attachment', 'item_key': item_key}
        
        # Extract bibliographic metadata
        title = data.get('title', '')
        authors = self._extract_authors(data)
        year = self._extract_year(data)
        publication = data.get('publicationTitle', '') or data.get('bookTitle', '')
        doi = data.get('DOI', '')
        
        # Extract BibTeX key from the entry
        bibtex_key = data.get('citationKey', '')
        if not bibtex_key and bibtex_entry:
            # Extract from BibTeX entry - looks for @article{key, or @book{key, etc.
            import re
            match = re.match(r'@\w+\{([^,]+),', bibtex_entry)
            if match:
                bibtex_key = match.group(1)
        
        # Process attachments to get full text
        full_text = None
        attachment_metadata = None
        file_path = None
        
        for attachment in relevant_attachments:
            att_key = attachment['key']
            att_filename = attachment['data'].get('filename', '')
            
            # Find file on disk
            attachment_dir = storage_dir / att_key
            if attachment_dir.exists():
                # Look for the actual file
                files = list(attachment_dir.glob('*'))
                pdf_files = [f for f in files if f.suffix.lower() == '.pdf']
                html_files = [f for f in files if f.suffix.lower() in ['.html', '.htm']]
                mhtml_files = [f for f in files if f.suffix.lower() in ['.mhtml', '.mht']]
                
                target_file = None
                if pdf_files:
                    target_file = pdf_files[0]
                elif html_files:
                    target_file = html_files[0]
                elif mhtml_files:
                    target_file = mhtml_files[0]
                elif files:
                    # Take any file that's not a hidden file
                    non_hidden = [f for f in files if not f.name.startswith('.')]
                    if non_hidden:
                        target_file = non_hidden[0]
                
                if target_file and target_file.exists():
                    logger.debug(f"Found attachment file: {target_file}")
                    
                    # Extract text using existing extractor
                    try:
                        # Calculate SHA1 for deduplication
                        with open(target_file, 'rb') as f:
                            content = f.read()
                            sha1 = hashlib.sha1(content).hexdigest()
                        
                        # Check if already exists
                        existing = list(db[self.target_table].rows_where("sha1 = ?", [sha1]))
                        if existing and not overwrite:
                            return {'status': 'skipped', 'item_key': item_key}
                        
                        # Extract text
                        _, extracted_text, metadata = await process_document(
                            str(target_file), sha1, use_readability=False
                        )
                        
                        if extracted_text and len(extracted_text) > 100:
                            full_text = extracted_text
                            attachment_metadata = metadata
                            file_path = str(target_file)
                            break
                    except Exception as e:
                        logger.warning(f"Failed to extract text from {target_file}: {e}")
        
        # If no full text extracted but has abstract, still save
        if not full_text and not abstract:
            return {'status': 'error', 'item_key': item_key, 'error': 'No text extracted'}
        
        # Prepare record
        record = {
            "sha1": hashlib.sha1(f"{item_key}_{title}".encode()).hexdigest(),
            "zotero_key": item_key,
            "bibtex_key": bibtex_key,
            "bibtex_entry": bibtex_entry,
            "title": title,
            "authors": authors,
            "year": year,
            "publication": publication,
            "doi": doi,
            "abstract": abstract,
            "raw_content": full_text,
            "file_path": file_path,
            "attachment_metadata": json.dumps(attachment_metadata) if attachment_metadata else None,
            "zotero_metadata": json.dumps(data),
            "collection_name": data.get('collections', []),
            "item_type": data.get('itemType'),
            "tags": json.dumps([tag['tag'] for tag in data.get('tags', [])]),
            "ingested_at": datetime.now().isoformat()
        }
        
        # Insert or update
        db[self.target_table].insert(record, replace=True)
        
        return {'status': 'success', 'item_key': item_key}
    
    def _ensure_literature_schema(self, db_path: str) -> None:
        """Ensure the literature table has the correct schema"""
        db = sqlite_utils.Database(db_path)
        
        # Create table if not exists
        if self.target_table not in db.table_names():
            db[self.target_table].create({
                "sha1": str,
                "zotero_key": str,
                "bibtex_key": str,
                "bibtex_entry": str,
                "title": str,
                "authors": str,
                "year": int,
                "publication": str,
                "doi": str,
                "abstract": str,
                "raw_content": str,
                "file_path": str,
                "attachment_metadata": str,
                "zotero_metadata": str,
                "collection_name": str,
                "item_type": str,
                "tags": str,
                "ingested_at": str
            }, pk="sha1")
            
            # Create indices
            db[self.target_table].create_index(["zotero_key"])
            db[self.target_table].create_index(["bibtex_key"])
            db[self.target_table].create_index(["doi"])
            db[self.target_table].create_index(["year"])
    
    def _extract_authors(self, item_data: Dict) -> str:
        """Extract authors from Zotero item data"""
        authors = []
        for creator in item_data.get('creators', []):
            if creator.get('creatorType') == 'author':
                name = ""
                if creator.get('lastName') and creator.get('firstName'):
                    name = f"{creator['firstName']} {creator['lastName']}"
                elif creator.get('lastName'):
                    name = creator['lastName']
                elif creator.get('name'):
                    name = creator['name']
                
                if name:
                    authors.append(name)
        
        return "; ".join(authors)
    
    def _extract_year(self, item_data: Dict) -> Optional[int]:
        """Extract year from Zotero item data"""
        date_str = item_data.get('date', '')
        if date_str:
            # Try to extract year from various date formats
            year_match = re.search(r'(\d{4})', date_str)
            if year_match:
                try:
                    return int(year_match.group(1))
                except ValueError:
                    pass
        return None
    
    def _get_bibtex_entry(self, zot: zotero.Zotero, item_key: str) -> str:
        """Get BibTeX entry for an item"""
        try:
            import urllib.request
            
            # Get API key and user ID from the zot object
            api_key = zot.api_key
            user_id = zot.library_id
            
            # Build the URL for single item BibTeX export
            url = f"https://api.zotero.org/users/{user_id}/items/{item_key}?format=bibtex"
            
            # Create request with headers
            req = urllib.request.Request(url)
            req.add_header("Zotero-API-Version", "3")
            req.add_header("Authorization", f"Bearer {api_key}")
            
            # Make the request
            with urllib.request.urlopen(req) as response:
                bibtex = response.read().decode('utf-8')
                return bibtex.strip()
                
        except Exception as e:
            logger.warning(f"Failed to get BibTeX for {item_key}: {e}")
            return ""