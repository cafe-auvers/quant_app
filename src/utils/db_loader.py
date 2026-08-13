"""Compatibility alias for :mod:`src.infrastructure.database`."""
import sys
from src.infrastructure import database as _database

sys.modules[__name__] = _database
