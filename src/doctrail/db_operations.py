"""Compatibility wrapper for database operations.

Implementation modules live under :mod:`doctrail.db_ops`. Existing callers can
continue importing from ``doctrail.db_operations``.
"""

from .db_ops import *
from .db_ops import __all__
