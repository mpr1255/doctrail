"""Repository for enrichment-related database operations."""

from typing import List, Dict, Optional, Any
from datetime import datetime
import json

from .base_repository import BaseRepository
from ..types import RowDict


class EnrichmentRepository(BaseRepository):
    """Repository for enrichment operations."""
    
    def store_enrichment_response(
        self,
        sha1: str,
        enrichment_name: str,
        raw_json: str,
        model_used: str,
        enrichment_id: Optional[str] = None,
        prompt_id: Optional[str] = None,
        full_prompt: Optional[str] = None
    ) -> None:
        """Store raw enrichment response.
        
        Args:
            sha1: Document SHA1
            enrichment_name: Name of enrichment
            raw_json: Raw JSON response
            model_used: Model used
            enrichment_id: Optional enrichment ID
            prompt_id: Optional prompt ID
            full_prompt: Optional full prompt text
        """
        query = """
            INSERT INTO enrichment_responses 
            (enrichment_id, sha1, enrichment_name, raw_json, model_used, 
             prompt_id, full_prompt, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        
        params = (
            enrichment_id,
            sha1,
            enrichment_name,
            raw_json,
            model_used,
            prompt_id,
            full_prompt,
            datetime.now().isoformat()
        )
        
        self.execute_update(query, params)
    
    def get_enrichment_history(
        self,
        sha1: Optional[str] = None,
        enrichment_name: Optional[str] = None,
        limit: int = 100
    ) -> List[RowDict]:
        """Get enrichment response history.
        
        Args:
            sha1: Optional SHA1 filter
            enrichment_name: Optional enrichment name filter
            limit: Maximum results to return
            
        Returns:
            List of enrichment response records
        """
        query = "SELECT * FROM enrichment_responses WHERE 1=1"
        params = []
        
        if sha1:
            query += " AND sha1 = ?"
            params.append(sha1)
        
        if enrichment_name:
            query += " AND enrichment_name = ?"
            params.append(enrichment_name)
        
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        
        return self.execute_query(query, tuple(params))
    
    def ensure_enrichment_responses_table(self) -> None:
        """Ensure enrichment_responses table exists."""
        query = """
            CREATE TABLE IF NOT EXISTS enrichment_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                enrichment_id TEXT,
                sha1 TEXT NOT NULL,
                enrichment_name TEXT NOT NULL,
                raw_json TEXT,
                model_used TEXT,
                prompt_id TEXT,
                full_prompt TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (prompt_id) REFERENCES prompts(prompt_id)
            )
        """
        self.execute_update(query)
        
        # Create indexes
        self.execute_update(
            "CREATE INDEX IF NOT EXISTS idx_enrichment_responses_sha1 "
            "ON enrichment_responses(sha1)"
        )
        self.execute_update(
            "CREATE INDEX IF NOT EXISTS idx_enrichment_responses_name "
            "ON enrichment_responses(enrichment_name)"
        )
        self.execute_update(
            "CREATE INDEX IF NOT EXISTS idx_enrichment_responses_created "
            "ON enrichment_responses(created_at DESC)"
        )
    
    def get_existing_enrichment(
        self,
        sha1: str,
        enrichment_name: str,
        model: Optional[str] = None
    ) -> Optional[RowDict]:
        """Check if enrichment already exists.
        
        Args:
            sha1: Document SHA1
            enrichment_name: Enrichment name
            model: Optional model filter
            
        Returns:
            Existing enrichment record or None
        """
        if model:
            query = """
                SELECT * FROM enrichment_responses 
                WHERE sha1 = ? AND enrichment_name = ? AND model_used = ?
                ORDER BY created_at DESC LIMIT 1
            """
            params = (sha1, enrichment_name, model)
        else:
            query = """
                SELECT * FROM enrichment_responses 
                WHERE sha1 = ? AND enrichment_name = ?
                ORDER BY created_at DESC LIMIT 1
            """
            params = (sha1, enrichment_name)
        
        results = self.execute_query(query, params)
        return results[0] if results else None