"""
Server configuration for multi-database Doctrail server.

This module handles loading and validating server configuration,
including multi-database support with per-database schema settings.

Directory structure for each database:
    /data/literature/
    ├── literature.db          # SQLite database
    ├── chroma_db/             # Vector index (optional)
    ├── doctrail.yaml          # Database config (optional)
    └── help.md                # Database-specific help (optional)
"""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Any, List
import yaml
import sqlite3

from .db_operations import _quote_identifier

logger = logging.getLogger(__name__)


@dataclass
class SchemaConfig:
    """Schema configuration for a database."""
    pk_column: str = "sha1"
    pk_type: str = "text"  # "text" or "integer"
    content_column: str = "raw_content"
    title_column: str = "title"
    chunks_table: Optional[str] = None  # If set, enables chunk-based FTS
    documents_table: str = "documents"


@dataclass
class DatabaseConfig:
    """Configuration for a single database."""
    name: str
    path: Path  # Directory path containing the database
    db_file: Path  # Actual .db file path
    description: str = ""
    corpus_type: str = "general"  # "primary", "secondary", "general"
    schema: SchemaConfig = field(default_factory=SchemaConfig)
    chroma_path: Optional[Path] = None
    help_path: Optional[Path] = None
    sync_on: bool = False
    sync_interval: int = 30  # minutes

    @classmethod
    def from_directory(cls, name: str, directory: Path) -> "DatabaseConfig":
        """
        Create DatabaseConfig by scanning a directory.

        Looks for:
        - *.db file (the database)
        - doctrail.yaml (config)
        - chroma_db/ (vector store)
        - help.md (documentation)
        """
        directory = Path(directory).expanduser().resolve()

        if not directory.exists():
            raise ValueError(f"Database directory does not exist: {directory}")

        if not directory.is_dir():
            raise ValueError(f"Path is not a directory: {directory}")

        # Find .db file
        db_files = list(directory.glob("*.db"))
        if not db_files:
            raise ValueError(f"No .db file found in directory: {directory}")
        if len(db_files) > 1:
            logger.warning(f"Multiple .db files in {directory}, using first: {db_files[0]}")
        db_file = db_files[0]

        # Load doctrail.yaml if present
        config_file = directory / "doctrail.yaml"
        config_data = {}
        if config_file.exists():
            with open(config_file) as f:
                config_data = yaml.safe_load(f) or {}
            logger.info(f"Loaded config from {config_file}")

        # Build schema config
        schema_data = config_data.get("schema", {})
        schema = SchemaConfig(
            pk_column=schema_data.get("pk_column", "sha1"),
            pk_type=schema_data.get("pk_type", "text"),
            content_column=schema_data.get("content_column", "raw_content"),
            title_column=schema_data.get("title_column", "title"),
            chunks_table=schema_data.get("chunks_table"),
            documents_table=schema_data.get("documents_table", "documents"),
        )

        # Check for chroma_db
        chroma_path = directory / "chroma_db"
        if not chroma_path.exists():
            chroma_path = None

        # Check for help.md
        help_path = directory / "help.md"
        if not help_path.exists():
            help_path = None

        return cls(
            name=name,
            path=directory,
            db_file=db_file,
            description=config_data.get("description", ""),
            corpus_type=config_data.get("corpus_type", "general"),
            schema=schema,
            chroma_path=chroma_path,
            help_path=help_path,
            sync_on=config_data.get("sync_on", False),
            sync_interval=config_data.get("sync_interval", 30),
        )

    def get_connection(self) -> sqlite3.Connection:
        """Get a database connection with appropriate settings."""
        conn = sqlite3.connect(str(self.db_file))
        conn.row_factory = sqlite3.Row

        # Set pragmas for concurrent access
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")

        return conn

    def has_chunks(self) -> bool:
        """Check if database has chunks table for FTS."""
        if self.schema.chunks_table:
            return True
        # Also check if chunks table exists in database
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'"
                )
                return cursor.fetchone() is not None
        except Exception:
            return False

    def has_chroma(self) -> bool:
        """Check if Chroma vector store is available."""
        return self.chroma_path is not None and self.chroma_path.exists()

    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        stats = {
            "name": self.name,
            "path": str(self.db_file),
            "description": self.description,
            "corpus_type": self.corpus_type,
            "has_chunks": self.has_chunks(),
            "has_chroma": self.has_chroma(),
        }

        try:
            with self.get_connection() as conn:
                # Document count
                table = self.schema.documents_table
                table_ref = _quote_identifier(table, "documents table")
                cursor = conn.execute(f"SELECT COUNT(*) FROM {table_ref}")
                stats["document_count"] = cursor.fetchone()[0]

                # Chunk count if available
                if self.has_chunks():
                    cursor = conn.execute("SELECT COUNT(*) FROM chunks")
                    stats["chunk_count"] = cursor.fetchone()[0]

                # Get tables
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
                stats["tables"] = [row[0] for row in cursor.fetchall()]

        except Exception as e:
            logger.warning(f"Error getting stats for {self.name}: {e}")
            stats["error"] = str(e)

        return stats


@dataclass
class ServerConfig:
    """Configuration for the multi-database server."""
    host: str = "127.0.0.1"
    port: int = 8000
    databases: Dict[str, DatabaseConfig] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> "ServerConfig":
        """Load server configuration from YAML file."""
        path = Path(path).expanduser().resolve()

        if not path.exists():
            raise FileNotFoundError(f"Server config not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError(f"Empty config file: {path}")

        # Parse server section
        server_data = data.get("server", {})
        host = server_data.get("host", "127.0.0.1")
        port = server_data.get("port", 8000)

        # Parse databases section
        databases_data = data.get("databases", {})
        if not databases_data:
            raise ValueError("No databases configured in server config")

        databases = {}
        for name, value in databases_data.items():
            if isinstance(value, str):
                # Simple format: name: /path/to/directory
                directory = Path(value)
            elif isinstance(value, dict):
                # Detailed format with path and optional overrides
                directory = Path(value.get("path", ""))
            else:
                raise ValueError(f"Invalid database config for {name}: {value}")

            try:
                db_config = DatabaseConfig.from_directory(name, directory)

                # Apply any overrides from server config
                if isinstance(value, dict):
                    if "description" in value:
                        db_config.description = value["description"]
                    if "corpus_type" in value:
                        db_config.corpus_type = value["corpus_type"]
                    if "sync_on" in value:
                        db_config.sync_on = value["sync_on"]
                    if "sync_interval" in value:
                        db_config.sync_interval = value["sync_interval"]

                databases[name] = db_config
                logger.info(f"Loaded database '{name}' from {directory}")

            except Exception as e:
                logger.error(f"Failed to load database '{name}': {e}")
                raise

        return cls(host=host, port=port, databases=databases)

    def get_database(self, name: str) -> DatabaseConfig:
        """Get a database config by name."""
        if name not in self.databases:
            available = list(self.databases.keys())
            raise KeyError(f"Database '{name}' not found. Available: {available}")
        return self.databases[name]

    def list_databases(self) -> List[Dict[str, Any]]:
        """List all databases with basic info."""
        return [
            {
                "name": name,
                "description": db.description,
                "corpus_type": db.corpus_type,
                "has_chunks": db.has_chunks(),
                "has_chroma": db.has_chroma(),
            }
            for name, db in self.databases.items()
        ]


def load_server_config(path: Optional[str] = None) -> ServerConfig:
    """
    Load server configuration from file or environment.

    Checks (in order):
    1. Provided path argument
    2. DOCTRAIL_SERVER_CONFIG environment variable
    3. ./doctrail-server.yaml in current directory
    """
    if path:
        config_path = Path(path)
    elif os.environ.get("DOCTRAIL_SERVER_CONFIG"):
        config_path = Path(os.environ["DOCTRAIL_SERVER_CONFIG"])
    else:
        config_path = Path("doctrail-server.yaml")

    return ServerConfig.from_yaml(config_path)


__all__ = [
    "SchemaConfig",
    "DatabaseConfig",
    "ServerConfig",
    "load_server_config",
]
