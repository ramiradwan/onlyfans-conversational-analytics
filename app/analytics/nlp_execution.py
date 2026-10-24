"""Release boundary for optional worker execution."""

from app.core.config import settings


def require_findings_execution() -> None:
    if settings.environment.lower() != 'test':
        raise ValueError('findings_execution_unavailable')
