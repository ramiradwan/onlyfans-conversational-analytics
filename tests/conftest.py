"""Backend test process configuration.

The application defaults to durable SQLite in production.  The shared test
application is deliberately disposable; restart/durability tests construct
explicit temporary SQLite repositories themselves.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


os.environ.setdefault("CANONICAL_PERSISTENCE_BACKEND", "memory")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("WEBSOCKET_AUTH_MODE", "development_stub")
os.environ.setdefault(
    "OFCA_TEST_DATABASE_MASTER_KEY_HEX",
    "4d7d278b49d90e4d747ac36d4f65661ec089df85a8f6f22851a628c880f7a4e2",
)

# Application modules construct some stores while the test collection graph is
# imported. Keep those stores in one isolated process-lifetime directory so a
# test run can never open or modify database files in the source checkout.
_TEST_DATABASE_DIRECTORY = tempfile.TemporaryDirectory(prefix="ofca-tests-")
_TEST_DATABASE_ROOT = Path(_TEST_DATABASE_DIRECTORY.name)
for _environment_name, _filename in (
    ("AUTH_DATABASE_PATH", "auth.sqlite3"),
    ("CANONICAL_DATABASE_PATH", "canonical.sqlite3"),
    ("PROJECTION_DATABASE_PATH", "projections.sqlite3"),
    ("ANALYTICS_PROJECTION_DATABASE_PATH", "analytics-projections.sqlite3"),
):
    os.environ[_environment_name] = str(_TEST_DATABASE_ROOT / _filename)
