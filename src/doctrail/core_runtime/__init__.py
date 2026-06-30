"""Core runtime modules with the legacy public API re-exported."""

from . import shared as _shared
from . import enrichment as _enrichment
from . import batch as _batch
from . import commands as _commands

from .shared import *
from .enrichment import *
from .batch import *
from .commands import *

_modules = [_shared, _enrichment, _batch, _commands]
__all__ = []
for _module in _modules:
    __all__.extend(getattr(_module, "__all__", []))

_exports = {name: globals()[name] for name in __all__}
for _module in _modules:
    _module.__dict__.update(_exports)

del _exports, _module, _modules, _shared, _enrichment, _batch, _commands
