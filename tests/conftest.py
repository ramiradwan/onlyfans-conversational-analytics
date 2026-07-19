"""Backend test process configuration.

The application defaults to durable SQLite in production.  The shared test
application is deliberately disposable; restart/durability tests construct
explicit temporary SQLite repositories themselves.
"""

from __future__ import annotations

import os


os.environ.setdefault("CANONICAL_PERSISTENCE_BACKEND", "memory")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("WEBSOCKET_AUTH_MODE", "development_stub")
