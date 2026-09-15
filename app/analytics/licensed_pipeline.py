"""Production analytics pipeline with fail-closed commercial admission."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.analytics.pipeline import AnalyticsPipeline
from app.core.config import settings
from app.persistence.auth import SQLiteAuthenticationStore
from app.security.analysis_authorization import require_cached_analysis_run


@lru_cache(maxsize=4)
def _auth_store(path: str) -> SQLiteAuthenticationStore:
    return SQLiteAuthenticationStore(Path(path))


class LicensedAnalyticsPipeline(AnalyticsPipeline):
    """Require a revalidated admitted policy before any new candidate build."""

    def build_candidate(self, creator_account_id: str, **kwargs):
        store = _auth_store(str(settings.auth_database_path.resolve()))
        require_cached_analysis_run(store, creator_account_id)
        return super().build_candidate(creator_account_id, **kwargs)
