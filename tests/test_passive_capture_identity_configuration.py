from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.core.config import settings
from app.services.agent_configuration import (
    BOOTSTRAP_CAPTURE_POLICY,
    BOOTSTRAP_COMMAND_POLICY,
    BOOTSTRAP_CONFIG_REVISION,
    AgentConfigurationAuthority,
    InMemoryAgentConfigRepository,
    build_config_document,
)


ACCOUNT_ID = "creator-account-a"
PLATFORM_ID = "platform-creator-a"


def authority(
    repository: InMemoryAgentConfigRepository,
    *,
    bootstrap: bool = True,
    platform_id: str | None = PLATFORM_ID,
) -> AgentConfigurationAuthority:
    return AgentConfigurationAuthority(
        repository,
        bootstrap_account_id=ACCOUNT_ID if bootstrap else None,
        authorized_accounts=lambda: frozenset({ACCOUNT_ID}),
        authorized_platform_identities=lambda: (
            {} if platform_id is None else {ACCOUNT_ID: platform_id}
        ),
    )


def test_bootstrap_pins_durable_platform_identity_without_enabling_history() -> None:
    repository = InMemoryAgentConfigRepository()
    configured = authority(repository)

    document = configured.required_document(ACCOUNT_ID)

    assert document.config_revision == BOOTSTRAP_CONFIG_REVISION == "config-10"
    assert document.history_acquisition.enabled is False
    assert document.history_acquisition.consent_revision is None
    assert document.history_acquisition.authorized_platform_creator_id == PLATFORM_ID


def test_history_revocation_keeps_platform_pin_for_passive_capture() -> None:
    repository = InMemoryAgentConfigRepository()
    configured = authority(repository)
    current = configured.required_document(ACCOUNT_ID)

    published = asyncio.run(
        configured.publish(
            ACCOUNT_ID,
            capture_policy=current.capture_policy.model_dump(mode="json"),
            command_policy=current.command_policy.model_dump(mode="json"),
            history_acquisition={
                "enabled": False,
                "consent_revision": None,
                "authorized_platform_creator_id": None,
                "recent_window_days": 30,
                "page_size": 100,
                "pages_per_wake": 2,
                "request_interval_ms": 500,
                "retry_limit": 3,
            },
        )
    )

    assert published.config_revision == "config-11"
    assert published.history_acquisition.enabled is False
    assert published.history_acquisition.consent_revision is None
    assert published.history_acquisition.authorized_platform_creator_id == PLATFORM_ID


def test_history_identity_must_match_durable_platform_binding() -> None:
    repository = InMemoryAgentConfigRepository()
    configured = authority(repository)
    current = configured.required_document(ACCOUNT_ID)

    with pytest.raises(
        ValueError,
        match="History platform identity disagrees with durable account authorization",
    ):
        asyncio.run(
            configured.publish(
                ACCOUNT_ID,
                capture_policy=current.capture_policy.model_dump(mode="json"),
                command_policy=current.command_policy.model_dump(mode="json"),
                history_acquisition={
                    "enabled": True,
                    "consent_revision": "consent-a",
                    "authorized_platform_creator_id": "platform-creator-b",
                    "recent_window_days": 30,
                    "page_size": 100,
                    "pages_per_wake": 2,
                    "request_interval_ms": 500,
                    "retry_limit": 3,
                },
            )
        )


def test_missing_durable_platform_identity_fails_closed() -> None:
    repository = InMemoryAgentConfigRepository()

    with pytest.raises(LookupError, match="No authorized platform identity"):
        authority(repository, platform_id=None)


def test_existing_config_9_advances_to_account_bound_config_10(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")
    repository = InMemoryAgentConfigRepository()
    old = build_config_document(
        creator_account_id=ACCOUNT_ID,
        config_revision="config-9",
        issued_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        capture_policy=BOOTSTRAP_CAPTURE_POLICY,
        command_policy=BOOTSTRAP_COMMAND_POLICY,
        history_acquisition={
            "enabled": False,
            "consent_revision": None,
            "authorized_platform_creator_id": None,
            "recent_window_days": 30,
            "page_size": 100,
            "pages_per_wake": 2,
            "request_interval_ms": 500,
            "retry_limit": 3,
        },
    )
    repository.add_document(old)
    repository.require_for_account(ACCOUNT_ID, "config-9")

    configured = authority(repository, bootstrap=False)
    current = configured.required_document(ACCOUNT_ID)

    assert repository.document(ACCOUNT_ID, "config-9") is not None
    assert current.config_revision == "config-10"
    assert current.history_acquisition.authorized_platform_creator_id == PLATFORM_ID


def test_occupied_config_10_advances_to_config_11_without_losing_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")
    repository = InMemoryAgentConfigRepository()
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
    repository.add_document(occupied)
    repository.require_for_account(ACCOUNT_ID, "config-10")

    configured = authority(repository, bootstrap=False)
    current = configured.required_document(ACCOUNT_ID)

    preserved = repository.document(ACCOUNT_ID, "config-10")
    assert preserved is not None
    assert preserved.digest == occupied.digest
    assert current.config_revision == "config-11"
    assert current.capture_policy.model_dump(mode="json") == occupied.capture_policy.model_dump(mode="json")
    assert current.command_policy.model_dump(mode="json") == occupied.command_policy.model_dump(mode="json")
    assert current.history_acquisition.enabled is False
    assert current.history_acquisition.consent_revision is None
    assert current.history_acquisition.recent_window_days == 14
    assert current.history_acquisition.page_size == 50
    assert current.history_acquisition.pages_per_wake == 1
    assert current.history_acquisition.request_interval_ms == 750
    assert current.history_acquisition.retry_limit == 2
    assert current.history_acquisition.authorized_platform_creator_id == PLATFORM_ID
