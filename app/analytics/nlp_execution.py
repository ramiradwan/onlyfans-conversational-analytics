"""Release boundary for optional worker execution."""

from app.core.optional_execution import installed_execution_release, require_optional_execution


def require_findings_execution() -> None:
    if installed_execution_release() is None:
        require_optional_execution()
