#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "sqlite-utils",
#     "loguru",
#     "rich",
#     "requests",
# ]
# ///

"""Example Custom Plugin - Template for creating your own ingestion plugins"""

import hashlib
import json
from datetime import datetime
from typing import Dict, Optional
import requests

from loguru import logger
from rich.console import Console
import sqlite_utils

console = Console()


class Plugin:
    """Example plugin that ingests data from a JSON API"""
    
    @property
    def name(self) -> str:
        return "example_api"
    
    @property
    def description(self) -> str:
        return "Example plugin that ingests data from a REST API"
    
    @property
    def target_table(self) -> str:
        return "api_data"
    
    async def ingest(
        self,
        db_path: str,
        config: Dict,
        verbose: bool = False,
        overwrite: bool = False,
        limit: Optional[int] = None,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        **kwargs
    ) -> Dict[str, int]:
        """
        Example ingestion from a REST API.
        
        Args:
            api_url: URL of the API endpoint
            api_key: Optional API key for authentication
        """
        if not api_url:
            raise ValueError("--api-url is required for this plugin")
        
        # Set up logging
        if verbose:
            logger.info(f"Fetching data from API: {api_url}")
        
        # Example: Fetch data from API
        headers = {}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        
        try:
            response = requests.get(api_url, headers=headers)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"Failed to fetch data from API: {e}")
            return {"total": 0, "success_count": 0, "error_count": 1, "skipped_count": 0}
        
        # Ensure data is a list
        if not isinstance(data, list):
            data = [data]
        
        if limit:
            data = data[:limit]
        
        # Create database connection
        db = sqlite_utils.Database(db_path)
        
        # Ensure table exists with appropriate schema
        if not db[self.target_table].exists():
            # Define your schema here based on your data structure
            db[self.target_table].create({
                "id": str,
                "content": str,
                "metadata": str,
                "created_at": str
            }, pk="id")
        
        # Process data
        success_count = 0
        error_count = 0
        skipped_count = 0
        
        for item in data:
            try:
                # Generate unique ID (customize based on your data)
                item_id = hashlib.sha1(
                    json.dumps(item, sort_keys=True).encode()
                ).hexdigest()
                
                # Check if already exists
                if not overwrite and db[self.target_table].get(item_id):
                    skipped_count += 1
                    continue
                
                # Transform data as needed
                record = {
                    "id": item_id,
                    "content": json.dumps(item),  # Store raw data
                    "metadata": json.dumps({"source": api_url}),
                    "created_at": datetime.now().isoformat()
                }
                
                # Insert or update
                db[self.target_table].insert(record, replace=True)
                success_count += 1
                
            except Exception as e:
                logger.error(f"Error processing item: {e}")
                error_count += 1
        
        # Summary
        console.print(f"[green]Successfully ingested {success_count} items[/green]")
        if error_count:
            console.print(f"[red]Failed to process {error_count} items[/red]")
        if skipped_count:
            console.print(f"[yellow]Skipped {skipped_count} existing items[/yellow]")
        
        return {
            "total": len(data),
            "success_count": success_count,
            "error_count": error_count,
            "skipped_count": skipped_count
        } 