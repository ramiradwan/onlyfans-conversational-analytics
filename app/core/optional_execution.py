"""Release boundary for optional local computation."""
from __future__ import annotations

import sys

from app.core.config import settings


def require_optional_execution() -> None:
    if getattr(sys, "frozen", False) or settings.environment.lower() != "test":
        raise ValueError("findings_execution_unavailable")
