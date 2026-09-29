from __future__ import annotations

import asyncio
import json
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import settings
from app.persistence.factory import create_canonical_repositories
from app.services import agent_configuration as config
from app.transport.manager import InMemoryTransportManager


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "socket-test-account"
IDENTITY = "socket-test-identity"
OLD_ISSUED_AT = datetime(2026, 9, 15, 11, 30, tzinfo=timezone.utc)
OLD_CAPTURE = {
    "observation_interval_seconds": 30,
    "rules": [
        {"resource": "chats", "url_pattern": "/api2/v2/chats", "enabled": True},
        {"resource": "chats", "url_pattern": "/api2/v2/users/*/chats", "enabled": True},
        {"resource": "messages", "url_pattern": "/api2/v2/chats/*/messages", "enabled": True},
        {"resource": "messages", "url_pattern": "/ws3", "enabled": True},
    ],
}
OLD_COMMAND = {"allowed_actions": [], "max_text_length": 1000, "require_idempotency": True}
OLD_HISTORY = {
    "enabled": False, "consent_revision": None,
    "authorized_platform_creator_id": IDENTITY, "recent_window_days": 30,
    "page_size": 100, "pages_per_wake": 2, "request_interval_ms": 500, "retry_limit": 3,
}


@pytest.fixture(autouse=True)
def local_authority(monkeypatch):
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")


def authority(repository):
    return config.AgentConfigurationAuthority(
        repository, authorized_accounts=lambda: frozenset({ACCOUNT}),
        authorized_platform_identities=lambda: {ACCOUNT: IDENTITY},
    )


def seed(repository, *, revision="config-10", capture=None, history=None):
    document = config.build_config_document(
        creator_account_id=ACCOUNT, config_revision=revision, issued_at=OLD_ISSUED_AT,
        capture_policy=deepcopy(OLD_CAPTURE if capture is None else capture),
        command_policy=OLD_COMMAND,
        history_acquisition=deepcopy(OLD_HISTORY if history is None else history),
    )
    repository.add_document(document)
    repository.require_for_account(ACCOUNT, revision)
    return document


def history_values():
    return {
        **OLD_HISTORY, "enabled": True, "consent_revision": "socket-test-consent",
        "consent_policy_version": "1", "desired_state": "running",
        "recent_window_days": 14, "page_size": 50, "pages_per_wake": 1,
        "request_interval_ms": 750, "retry_limit": 2,
    }


def seed_history_copy(repositories, monkeypatch):
    with monkeypatch.context() as legacy:
        legacy.setattr(config, "BOOTSTRAP_CONFIG_REVISION", "config-10")
        legacy.setattr(config, "BOOTSTRAP_ISSUED_AT", OLD_ISSUED_AT)
        legacy.setattr(config, "BOOTSTRAP_CAPTURE_POLICY", deepcopy(OLD_CAPTURE))
        # Use the old publication path without invoking a later repair.
        legacy.setattr(config.AgentConfigurationAuthority, "bootstrap_account", lambda *args: None)
        seed(repositories.configuration)
        manager = InMemoryTransportManager(repositories)
        manager.config_authority = authority(repositories.configuration)
        row = repositories.history.update_history_settings(
            ACCOUNT, expected_revision=0, values=history_values(),
        )
        document = asyncio.run(manager.publish_history_settings(ACCOUNT, row))
    return document


def assert_paths(document):
    script = """
import { captureIsEnabled } from './extension/transport/read-only-capture-ingestion.mjs';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const document = JSON.parse(input);
console.log(JSON.stringify(['/ws3/17', '/ws3', '/ws3/17/x', '/ws3x']
  .map(path => captureIsEnabled(document, 'messages', path))));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], input=document.model_dump_json(),
        text=True, capture_output=True, cwd=ROOT, check=True,
    )
    assert json.loads(result.stdout) == [True, True, False, False]


@pytest.mark.parametrize("entry", ["required", "admission"])
@pytest.mark.parametrize("source", ["fresh", "config10", "history_copy", "identity_copy"])
def test_t1_served_paths(tmp_path, monkeypatch, source, entry):
    repositories = create_canonical_repositories("sqlite", canonical_path=tmp_path / "canonical.sqlite3")
    repository = repositories.configuration
    previous = None
    if source == "config10":
        previous = seed(repository)
    elif source == "history_copy":
        previous = seed_history_copy(repositories, monkeypatch)
    elif source == "identity_copy":
        old = seed(repository, history={**OLD_HISTORY, "authorized_platform_creator_id": None})
        previous = authority(repository)._publish_identity_upgrade(ACCOUNT, old)
    configured = authority(repository)
    if entry == "admission":
        record = configured.bind_installation(
            ACCOUNT, uuid4(), previous.config_revision if previous else None,
        )
        document = repository.document(ACCOUNT, record.required_config_revision)
    else:
        document = configured.required_document(ACCOUNT)
    assert_paths(document)
    if previous:
        assert repository.document(ACCOUNT, previous.config_revision).model_dump_json() == previous.model_dump_json()
        if source.endswith("copy"):
            assert document.history_acquisition == previous.history_acquisition
            assert document.command_policy == previous.command_policy
            assert document.config_schema_version == previous.config_schema_version
            expected = previous.capture_policy.model_dump(mode="json")
            expected["rules"].append({"resource": "messages", "url_pattern": "/ws3/*", "enabled": True})
            assert document.capture_policy.model_dump(mode="json") == expected


def test_t2_shared_fixture_equals_served_bootstrap():
    served = authority(config.InMemoryAgentConfigRepository()).required_document(ACCOUNT)
    fixture = json.loads((ROOT / "shared/fixtures/protocol/live-socket-capture-policy.json").read_text())
    assert served.capture_policy.model_dump(mode="json") == fixture
    assert_paths(served)


def test_t1_copy_preserves_rule_order_and_custom_policies():
    repository = config.InMemoryAgentConfigRepository()
    capture = deepcopy(OLD_CAPTURE)
    capture["observation_interval_seconds"] = 45
    capture["rules"].append({"resource": "chats", "url_pattern": "/custom", "enabled": False})
    previous = config.build_config_document(
        creator_account_id=ACCOUNT, config_revision="config-21", issued_at=OLD_ISSUED_AT,
        capture_policy=capture, command_policy={**OLD_COMMAND, "max_text_length": 777},
        history_acquisition={**OLD_HISTORY, "authorized_platform_creator_id": None, "page_size": 50},
        config_schema_version="2",
    )
    repository.add_document(previous)
    repository.require_for_account(ACCOUNT, previous.config_revision)
    current = authority(repository).required_document(ACCOUNT)
    expected = deepcopy(capture)
    expected["rules"].insert(4, {"resource": "messages", "url_pattern": "/ws3/*", "enabled": True})
    assert current.capture_policy.model_dump(mode="json") == expected
    assert current.command_policy == previous.command_policy
    assert current.config_schema_version == previous.config_schema_version
    assert current.history_acquisition.model_dump(mode="json") == {
        **previous.history_acquisition.model_dump(mode="json"), "authorized_platform_creator_id": IDENTITY,
    }
    assert current.config_revision == "config-22"
    assert repository.document(ACCOUNT, "config-21").model_dump_json() == previous.model_dump_json()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_t3_upgrade_is_restart_idempotent(tmp_path, backend):
    path = tmp_path / "canonical.sqlite3"
    repository = (config.InMemoryAgentConfigRepository() if backend == "memory" else
                  create_canonical_repositories("sqlite", canonical_path=path).configuration)
    previous = seed(repository, revision="config-21")
    installation = uuid4()
    configured = authority(repository)
    current = configured.required_document(ACCOUNT)
    assert current.config_revision == "config-22"
    for _ in range(3):
        assert configured.required_document(ACCOUNT).model_dump_json() == current.model_dump_json()
        assert configured.bind_installation(ACCOUNT, installation, "config-22").required_config_revision == "config-22"
    if backend == "sqlite":
        repository = create_canonical_repositories("sqlite", canonical_path=path).configuration
    restarted = authority(repository)
    assert restarted.required_document(ACCOUNT).model_dump_json() == current.model_dump_json()
    assert restarted.bind_installation(ACCOUNT, installation, "config-22").required_config_revision == "config-22"
    assert repository.next_revision(ACCOUNT) == "config-23"
    assert repository.document(ACCOUNT, "config-23") is None
    assert repository.document(ACCOUNT, "config-21").digest == previous.digest


def test_t4_effective_history_survives_upgrade(tmp_path, monkeypatch):
    repositories = create_canonical_repositories("sqlite", canonical_path=tmp_path / "canonical.sqlite3")
    old = seed_history_copy(repositories, monkeypatch)
    history = repositories.history
    before = history.mark_history_config_applied(ACCOUNT, old.config_revision)
    assert before["effective_state"] == "running"
    manager = InMemoryTransportManager(repositories)
    manager.config_authority = authority(repositories.configuration)
    installation = uuid4()
    record = manager.config_authority.bind_installation(ACCOUNT, installation, old.config_revision)
    current = manager.required_config_document(ACCOUNT)
    assert record.required_config_revision != old.config_revision
    assert current.history_acquisition == old.history_acquisition
    assert current.history_acquisition.authorized_platform_creator_id == IDENTITY
    manager.config_authority.record_report(
        ACCOUNT, installation, config_revision=current.config_revision, digest=current.digest,
        outcome="applied", capability_details=[],
    )
    after = history.mark_history_config_applied(ACCOUNT, current.config_revision)
    assert after == before
    manager.active_agents[ACCOUNT] = SimpleNamespace(
        agent_installation_id=installation, status="connected", connection_id=uuid4(),
        last_heartbeat_at=datetime.now(timezone.utc), applied_config_revision=current.config_revision,
    )
    assert manager.agent_state_payload(ACCOUNT)["degraded_reason"] is None
    monkeypatch.setattr(manager.projection, "state", lambda _: {
        "status": "current", "projected_revision": 0, "canonical_revision": 0,
    })
    assert "configuration=aligned" in manager.system_state_payload(ACCOUNT)["detail"]
    manager.active_agents.clear()
    row = history.update_history_settings(
        ACCOUNT, expected_revision=before["settings_revision"],
        values={**history_values(), "desired_state": "paused"},
    )
    later = asyncio.run(manager.publish_history_settings(ACCOUNT, row))
    assert_paths(later)
    applied = history.mark_history_config_applied(ACCOUNT, later.config_revision)
    assert applied["effective_state"] == "paused"
    assert applied["effective_config_revision"] == later.config_revision


@pytest.mark.parametrize("kind", ["disabled", "absent", "wildcard_disabled"])
def test_t5_custom_policy_is_unchanged(kind):
    repository = config.InMemoryAgentConfigRepository()
    capture = deepcopy(OLD_CAPTURE)
    if kind == "disabled":
        capture["rules"][-1]["enabled"] = False
    elif kind == "absent":
        capture["rules"].pop()
    else:
        capture["rules"].append({"resource": "messages", "url_pattern": "/ws3/*", "enabled": False})
    previous = seed(repository, revision="config-21", capture=capture)
    configured = authority(repository)
    assert configured.required_document(ACCOUNT).model_dump_json() == previous.model_dump_json()
    assert configured.bind_installation(ACCOUNT, uuid4(), "config-21").required_config_revision == "config-21"
    assert repository.next_revision(ACCOUNT) == "config-22"
