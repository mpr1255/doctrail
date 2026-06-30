"""Compatibility wrapper for Doctrail core operations.

Implementation modules live under :mod:`doctrail.core_runtime`. Existing callers can
continue importing from ``doctrail.core``.
"""

import sys
from types import ModuleType

from .core_runtime import *
from .core_runtime import __all__
from .core_runtime import batch as _batch_module
from .core_runtime import commands as _commands_module
from .core_runtime import enrichment as _enrichment_module
from .core_runtime import shared as _shared_module
from .db_operations import execute_query, get_db_connection
from .llm_operations import process_enrichment

_LINKED_MODULES = [_shared_module, _enrichment_module, _batch_module, _commands_module]


class _CoreCompatibilityModule(ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for module in _LINKED_MODULES:
            if name in module.__dict__:
                setattr(module, name, value)


sys.modules[__name__].__class__ = _CoreCompatibilityModule
