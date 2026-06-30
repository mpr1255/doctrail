"""
CLI module - modular command structure for doctrail.

Usage:
    from doctrail.cli import cli
    cli()

Each command group is in its own file:
    - main.py: cli group, init, new, view
    - enrich.py: enrich, list_enrichments
    - ingest.py: ingest
    - query.py: query, sql, document, stats
    - export.py: export
    - serve.py: serve
    - review.py: review
"""

# Import CLI group from main
from .main import cli
from .utils import __version__, _load_project_config

# Import command modules to register them with cli
from . import ingest
from . import enrich
from . import query
from . import export
from . import serve
from . import review
from . import icr
from . import models

__all__ = ['cli', '__version__', '_load_project_config']
