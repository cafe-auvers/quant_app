"""Database infrastructure facade preserving the legacy db_loader API.

P2 divides connection, mirror, schema, refresh, and repository concerns into
focused modules. The synchronized namespace is temporary compatibility glue:
existing callers and tests can keep importing/monkeypatching
``src.utils.db_loader`` while consumers migrate to the focused modules.
"""
from __future__ import annotations

import sys
import types

from src.infrastructure.database import _shared, engine, mirror, refresh, schema
from src.infrastructure.database.repositories import market_data, scanner

_P2_MODULES = (_shared, engine, mirror, schema, market_data, refresh, scanner)


def _p2_exports(module):
    return {
        name: value
        for name, value in vars(module).items()
        if not name.startswith("__")
    }


_P2_EXPORTS = {}
for _module in _P2_MODULES:
    _P2_EXPORTS.update(_p2_exports(_module))
globals().update(_P2_EXPORTS)

# Existing functions historically shared one module-global namespace. Populate
# missing cross-module names so their runtime lookup behavior stays unchanged.
for _module in _P2_MODULES:
    for _name, _value in _P2_EXPORTS.items():
        _module.__dict__.setdefault(_name, _value)


class _DatabaseFacadeModule(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("_P2_"):
            return
        for module in _P2_MODULES:
            if name in module.__dict__:
                module.__dict__[name] = value


sys.modules[__name__].__class__ = _DatabaseFacadeModule
__all__ = sorted(name for name in _P2_EXPORTS if not name.startswith("_"))
