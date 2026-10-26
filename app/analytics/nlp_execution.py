"""Release boundary for optional worker execution."""

from app.core.optional_execution import require_optional_execution


def require_findings_execution() -> None:
    require_optional_execution()
