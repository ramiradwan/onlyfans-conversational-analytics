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
    from app.nlp.preparation import load_preparation_release
    admission = load_preparation_release()
    return admission.execution if admission is not None else None


def installed_execution_release():
    if getattr(sys, "frozen", False):
        return _packaged_release()
    return None


def require_findings_execution() -> None:
    if installed_execution_release() is None:
        require_optional_execution()
