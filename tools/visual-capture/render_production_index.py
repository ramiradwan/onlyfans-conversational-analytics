"""Writes the production Bridge index, as serve_frontend renders it, to the given path.

Runs the frontend router alone under disposable test settings. The frontend build in
app/static/dist must exist.
"""

from __future__ import annotations

import os
import secrets
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


PRODUCT_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIRECTORY = tempfile.TemporaryDirectory(prefix="ofca-first-paint-")
_DATA_ROOT = Path(_DATA_DIRECTORY.name)
os.environ.update(
    {
        "CANONICAL_PERSISTENCE_BACKEND": "memory",
        "ENVIRONMENT": "test",
        "WEBSOCKET_AUTH_MODE": "development_stub",
        "OFCA_TEST_DATABASE_MASTER_KEY_HEX": secrets.token_hex(32),
        "LOCAL_ANALYTICS_DATA_DIR": str(_DATA_ROOT),
        "AUTH_DATABASE_PATH": str(_DATA_ROOT / "auth.sqlite3"),
        "CANONICAL_DATABASE_PATH": str(_DATA_ROOT / "canonical.sqlite3"),
        "PROJECTION_DATABASE_PATH": str(_DATA_ROOT / "projections.sqlite3"),
        "ANALYTICS_PROJECTION_DATABASE_PATH": str(_DATA_ROOT / "analytics-projections.sqlite3"),
    }
)
sys.path.insert(0, str(PRODUCT_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.endpoints import frontend  # noqa: E402
from app.core.config import settings  # noqa: E402


def main(output: Path) -> None:
    settings.environment = "production"
    application = FastAPI()
    application.include_router(frontend.router)
    application.dependency_overrides[frontend.get_runtime_policy] = lambda: SimpleNamespace(identity=None)
    response = TestClient(application).get("/")
    if response.status_code != 200:
        raise SystemExit(f"serve_frontend returned {response.status_code}: {response.text[:400]}")
    output.write_text(response.text, encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
