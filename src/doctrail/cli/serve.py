"""
Serve command - start the Doctrail multi-database server.
"""
import os
from typing import Optional

import click

from .main import cli
from .utils import _exit_error, setup_logging


@cli.command()
@click.option('--config', 'config_path', type=click.Path(exists=True),
              help='Server configuration file (default: doctrail-server.yaml)')
@click.option('--host', default=None, help='Override host from config')
@click.option('--port', default=None, type=int, help='Override port from config')
@click.option('--verbose', is_flag=True, help='Enable verbose logging')
@click.pass_context
def serve(ctx, config_path: Optional[str], host: Optional[str], port: Optional[int], verbose: bool):
    """
    Start the Doctrail multi-database server.

    The server provides HTTP endpoints for searching and enriching multiple
    SQLite databases. Configure databases in a YAML file:

    \b
    # doctrail-server.yaml
    server:
      host: 0.0.0.0
      port: 8000
    databases:
      literature: /data/literature    # directory containing .db file
      organs: /data/organs

    Each database directory should contain:
    - A .db file (the SQLite database)
    - Optional: doctrail.yaml (schema config)
    - Optional: chroma_db/ (vector store for semantic search)
    - Optional: help.md (database-specific documentation)

    Example:
        doctrail serve --config doctrail-server.yaml
        doctrail serve --port 9000
    """
    setup_logging(verbose)

    try:
        import uvicorn
    except ImportError:
        _exit_error("uvicorn not installed. Add it to the project environment.")

    # Set config path in environment for server to pick up
    if config_path:
        os.environ["DOCTRAIL_SERVER_CONFIG"] = config_path
    elif os.path.exists("doctrail-server.yaml"):
        os.environ["DOCTRAIL_SERVER_CONFIG"] = "doctrail-server.yaml"

    # Load config to get defaults
    final_host = host or "0.0.0.0"
    final_port = port or 8000

    if os.environ.get("DOCTRAIL_SERVER_CONFIG"):
        try:
            from ..server_config import load_server_config
            server_config = load_server_config(os.environ["DOCTRAIL_SERVER_CONFIG"])
            final_host = host or server_config.host
            final_port = port or server_config.port
        except Exception as e:
            if verbose:
                click.echo(f"Could not load config: {e}", err=True)

    click.echo(f"Starting Doctrail server on http://{final_host}:{final_port}")

    # Run server
    uvicorn.run(
        "doctrail.server:app",
        host=final_host,
        port=final_port,
        log_level="debug" if verbose else "info",
        reload=False,
    )
