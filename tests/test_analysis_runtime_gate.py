from __future__ import annotations

import pytest

from app.analytics import runtime as analytics_runtime
from app.api.endpoints import transport_ws
from app.security.runtime_policy import RuntimeAuthorizationDenied


@pytest.mark.asyncio
async def test_post_ingest_rebuild_is_not_scheduled_when_analysis_run_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: list[str] = []
    policy = object()

    monkeypatch.setattr(transport_ws, "build_runtime_policy", lambda identity: policy)

    def deny(candidate: object) -> None:
        assert candidate is policy
        raise RuntimeAuthorizationDenied("CapabilityLicense authority is required")

    monkeypatch.setattr(transport_ws, "require_current_analysis_run", deny)

    async def schedule(account_id: str, *, source=None) -> bool:
        scheduled.append(account_id)
        return True

    monkeypatch.setattr(analytics_runtime, "request_projection_rebuild", schedule)

    await transport_ws._schedule_analytics_rebuild("creator-1", "principal-1")

    assert scheduled == []


@pytest.mark.asyncio
async def test_post_ingest_rebuild_is_scheduled_after_analysis_run_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: list[str] = []
    policy = object()

    monkeypatch.setattr(transport_ws, "build_runtime_policy", lambda identity: policy)
    monkeypatch.setattr(
        transport_ws,
        "require_current_analysis_run",
        lambda candidate: None if candidate is policy else (_ for _ in ()).throw(AssertionError()),
    )

    async def schedule(account_id: str, *, source=None) -> bool:
        scheduled.append(account_id)
        return True

    monkeypatch.setattr(analytics_runtime, "request_projection_rebuild", schedule)

    await transport_ws._schedule_analytics_rebuild("creator-1", "principal-1")

    assert scheduled == ["creator-1"]
