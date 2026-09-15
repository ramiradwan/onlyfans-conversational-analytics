from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.config import settings
from app.persistence.factory import create_canonical_repositories
from app.services.agent_configuration import (
    AgentConfigurationAuthority,
    build_config_document,
)


ACCOUNT_ID = "creator-account-sqlite"
PLATFORM_ID = "platform-creator-sqlite"


def authority(repository) -> AgentConfigurationAuthority:
    return AgentConfigurationAuthority(
        repository,
        bootstrap_account_id=None,
        authorized_accounts=lambda: frozenset({ACCOUNT_ID}),
        authorized_platform_identities=lambda: {ACCOUNT_ID: PLATFORM_ID},
    )


def test_occupied_config_10_identity_migration_is_transactional_and_restart_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")
    path = tmp_path / "canonical.sqlite3"
    first = create_canonical_repositories("sqlite", canonical_path=path)
    occupied = build_config_document(
        creator_account_id=ACCOUNT_ID,
        config_revision="config-10",
        issued_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        capture_policy={
            "observation_interval_seconds": 45,
            "rules": [
                {
                    "resource": "chats",
                    "url_pattern": "/api2/v2/chats",
                    "enabled": True,
                }
            ],
        },
        command_policy={
            "allowed_actions": [],
            "max_text_length": 777,
            "require_idempotency": True,
        },
        history_acquisition={
            "enabled": False,
            "consent_revision": None,
            "authorized_platform_creator_id": None,
            "recent_window_days": 14,
            "page_size": 50,
            "pages_per_wake": 1,
            "request_interval_ms": 750,
            "retry_limit": 2,
        },
    )
    first.configuration.publish_document(occupied)

    migrated = authority(first.configuration).required_document(ACCOUNT_ID)

    assert migrated.config_revision == "config-11"
    assert first.configuration.document(ACCOUNT_ID, "config-10").digest == occupied.digest
    assert migrated.capture_policy.model_dump(mode="json") == occupied.capture_policy.model_dump(mode="json")
    assert migrated.command_policy.model_dump(mode="json") == occupied.command_policy.model_dump(mode="json")
    assert migrated.history_acquisition.recent_window_days == 14
    assert migrated.history_acquisition.page_size == 50
    assert migrated.history_acquisition.pages_per_wake == 1
    assert migrated.history_acquisition.request_interval_ms == 750
    assert migrated.history_acquisition.retry_limit == 2
    assert migrated.history_acquisition.authorized_platform_creator_id == PLATFORM_ID

    reopened = create_canonical_repositories("sqlite", canonical_path=path)
    current = authority(reopened.configuration).required_document(ACCOUNT_ID)

    assert current.config_revision == "config-11"
    assert current.digest == migrated.digest
    assert reopened.configuration.document(ACCOUNT_ID, "config-12") is None
