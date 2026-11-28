"""Release boundary for optional local computation."""
from __future__ import annotations

import sys
from functools import lru_cache

from app.core.config import settings


def require_optional_execution() -> None:
    if getattr(sys, "frozen", False) or settings.environment.lower() != "test":
        raise ValueError("findings_execution_unavailable")


@lru_cache(maxsize=1)
def _packaged_release():
    from app.services.nlp_setup import load_execution_release
    return load_execution_release()


def installed_execution_release():
    if getattr(sys, "frozen", False):
        return _packaged_release()
    return None
