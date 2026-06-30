"""
CLI entry point - re-exports from modular cli/ package.

Commands are organized in src/doctrail/cli/:
    - main.py: cli group, init, new, view
    - enrich.py: enrich, list_enrichments
    - ingest.py: ingest
    - query.py: query, sql, document, stats
    - export.py: export
    - serve.py: serve
    - review.py: review
"""

from .cli import cli, __version__, _load_project_config

__all__ = ['cli', '__version__', '_load_project_config']

if __name__ == '__main__':
    cli()
