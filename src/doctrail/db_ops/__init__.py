"""Database operation modules with the legacy public API re-exported."""

from . import common as _common
from . import audit_runs as _audit_runs
from . import enrichments as _enrichments
from . import views as _views
from . import migrations as _migrations

from .common import *
from .audit_runs import *
from .enrichments import *
from .views import *
from .migrations import *

_modules = [_common, _audit_runs, _enrichments, _views, _migrations]
__all__ = []
for _module in _modules:
    __all__.extend(getattr(_module, "__all__", []))

_exports = {name: globals()[name] for name in __all__}
for _module in _modules:
    _module.__dict__.update(_exports)

del _exports, _module, _modules, _common, _audit_runs, _enrichments, _views, _migrations
