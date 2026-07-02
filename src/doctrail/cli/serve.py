"""
Serve command - start the Doctrail multi-database server.
"""
import os
import importlib.util
from typing import Optional

import click

from .main import cli
from .utils import _exit_error, setup_logging


@cli.command()
@click.option('--config', 'config_path', type=click.Path(exists=True),
              help='Server configuration file (default: doctrail-server.yaml)')
@click.option('--host', default=None, help='Override host from config')
@click.option('--port', default=None, type=int, help='Override port from config')
@click.option('--enable-write-api', is_flag=True,
              help='Enable legacy /enrich, /ingest, and /export HTTP endpoints')
@click.option('--token', default=None,
              help='Bearer token required for write endpoints when enabled')
@click.option('--verbose', is_flag=True, help='Enable verbose logging')
@click.pass_context
def serve(
    ctx,
    config_path: Optional[str],
    host: Optional[str],
    port: Optional[int],
    enable_write_api: bool,
    token: Optional[str],
    verbose: bool,
):
    """
    Start the Doctrail multi-database server.

    The server provides HTTP endpoints for searching and enriching multiple
    SQLite databases. Configure databases in a YAML file:

    \b
    # doctrail-server.yaml
    server:
      host: 127.0.0.1
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
        doctrail serve --host 0.0.0.0 --enable-write-api --token "$DOCTRAIL_SERVER_TOKEN"
    """
    setup_logging(verbose)

    if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("uvicorn") is None:
        _exit_error("Server dependencies are not installed. Install with `uv tool install 'doctrail[server]'`.")

    try:
        import uvicorn
    except ImportError:
        _exit_error("Server dependencies are not installed. Install with `uv tool install 'doctrail[server]'`.")

    # Set config path in environment for server to pick up
    if config_path:
        os.environ["DOCTRAIL_SERVER_CONFIG"] = config_path
    elif os.path.exists("doctrail-server.yaml"):
        os.environ["DOCTRAIL_SERVER_CONFIG"] = "doctrail-server.yaml"

    # Load config to get defaults
    final_host = host or "127.0.0.1"
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

    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    wide_bind = final_host not in loopback_hosts
    if enable_write_api:
        os.environ["DOCTRAIL_ENABLE_WRITE_API"] = "1"
        if token:
            os.environ["DOCTRAIL_SERVER_TOKEN"] = token
        if wide_bind and not os.environ.get("DOCTRAIL_SERVER_TOKEN"):
            _exit_error(
                "Refusing to enable write endpoints on a non-loopback host without "
                "--token or DOCTRAIL_SERVER_TOKEN."
            )
    else:
        os.environ.pop("DOCTRAIL_ENABLE_WRITE_API", None)

    os.environ["DOCTRAIL_SERVER_HOST"] = final_host

    click.echo(f"Starting Doctrail server on http://{final_host}:{final_port}")
    if not enable_write_api:
        click.echo("Legacy write endpoints are disabled. Use --enable-write-api to enable them.")

    # Run server
    uvicorn.run(
        "doctrail.server:app",
        host=final_host,
        port=final_port,
        log_level="debug" if verbose else "info",
        reload=False,
    )
