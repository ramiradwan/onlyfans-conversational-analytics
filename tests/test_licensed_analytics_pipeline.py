from __future__ import annotations

import pytest

from app.analytics.licensed_pipeline import LicensedAnalyticsPipeline
from app.security.runtime_policy import RuntimeAuthorizationDenied


class _UnusedSource:
    def __init__(self) -> None:
        self.touched = False

    def account_exists(self, creator_account_id: str) -> bool:
        self.touched = True
        raise AssertionError("canonical state must not be read before authorization")

    def account_read_model(self, creator_account_id: str):
        self.touched = True
        raise AssertionError("canonical state must not be read before authorization")

    def account_revisions(self):
        self.touched = True
        raise AssertionError("canonical state must not be read before authorization")


def test_candidate_build_denies_before_canonical_analysis_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _UnusedSource()
    pipeline = LicensedAnalyticsPipeline(source)  # type: ignore[arg-type]

    def deny(store, creator_account_id: str):
        raise RuntimeAuthorizationDenied("Current analysis admission is required")

    monkeypatch.setattr(
        "app.analytics.licensed_pipeline.require_cached_analysis_run",
        deny,
    )

    with pytest.raises(RuntimeAuthorizationDenied, match="analysis admission"):
        pipeline.build_candidate("creator-1")

    assert source.touched is False
