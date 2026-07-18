from pathlib import Path

import pytest
from pydantic import ValidationError

from app.protocol import (
    AGENT_TO_BRAIN_ADAPTER,
    BRAIN_TO_AGENT_ADAPTER,
    BRAIN_TO_BRIDGE_ADAPTER,
    BRIDGE_TO_BRAIN_ADAPTER,
    AgentConfigDocumentResponse,
    AgentConfigGetRequest,
)


FIXTURE_ROOT = Path(__file__).parents[1] / "shared" / "fixtures" / "protocol" / "v1"

AGENT_TO_BRAIN = {
    "agent.hello",
    "agent.heartbeat",
    "ingest.snapshot",
    "ingest.delta",
    "presence.observed",
    "config.applied",
    "command.result",
}
BRAIN_TO_AGENT = {
    "agent.session",
    "sync.required",
    "ingest.ack",
    "ingest.rejected",
    "protocol.error",
    "config.available",
    "command.execute",
    "command.result.ack",
}
BRIDGE_TO_BRAIN = {"bridge.hello", "state.resync"}
BRAIN_TO_BRIDGE = {
    "bridge.session",
    "state.snapshot",
    "state.delta",
    "presence.state",
    "agent.state",
    "system.state",
}


def parse_valid_fixture(path: Path) -> object:
    operation = path.stem
    document = path.read_text(encoding="utf-8")
    if operation in AGENT_TO_BRAIN:
        return AGENT_TO_BRAIN_ADAPTER.validate_json(document)
    if operation in BRAIN_TO_AGENT:
        return BRAIN_TO_AGENT_ADAPTER.validate_json(document)
    if operation in BRIDGE_TO_BRAIN:
        return BRIDGE_TO_BRAIN_ADAPTER.validate_json(document)
    if operation in BRAIN_TO_BRIDGE:
        return BRAIN_TO_BRIDGE_ADAPTER.validate_json(document)
    if operation == "agent.config.get":
        return AgentConfigGetRequest.model_validate_json(document)
    if operation == "agent.config.document":
        return AgentConfigDocumentResponse.model_validate_json(document)
    raise AssertionError(f"Unmapped operation fixture: {operation}")


VALID_FIXTURES = sorted(path for path in FIXTURE_ROOT.glob("*.json"))


@pytest.mark.parametrize("fixture", VALID_FIXTURES, ids=lambda path: path.stem)
def test_every_operation_has_a_valid_golden_fixture(fixture: Path) -> None:
    assert len(VALID_FIXTURES) == 25
    assert parse_valid_fixture(fixture) is not None


INVALID_ADAPTERS = {
    "wrong-enum.agent.state": BRAIN_TO_BRIDGE_ADAPTER,
    "missing-identity.ingest.delta": AGENT_TO_BRAIN_ADAPTER,
    "unknown-extra.bridge.hello": BRIDGE_TO_BRAIN_ADAPTER,
    "wrong-type.state.snapshot": BRAIN_TO_BRIDGE_ADAPTER,
    "malformed-discriminator.unknown-command": BRAIN_TO_AGENT_ADAPTER,
}


@pytest.mark.parametrize("fixture", sorted((FIXTURE_ROOT / "invalid").glob("*.json")), ids=lambda path: path.stem)
def test_every_invalid_fixture_is_rejected(fixture: Path) -> None:
    assert len(INVALID_ADAPTERS) == 5
    with pytest.raises(ValidationError):
        INVALID_ADAPTERS[fixture.stem].validate_json(fixture.read_text(encoding="utf-8"))


def test_protocol_error_is_valid_for_both_recipient_roles() -> None:
    document = (FIXTURE_ROOT / "protocol.error.json").read_text(encoding="utf-8")
    assert BRAIN_TO_AGENT_ADAPTER.validate_json(document) is not None
    assert BRAIN_TO_BRIDGE_ADAPTER.validate_json(document) is not None


def test_role_specific_unions_reject_wrong_role_messages() -> None:
    bridge_hello = (FIXTURE_ROOT / "bridge.hello.json").read_text(encoding="utf-8")
    with pytest.raises(ValidationError):
        AGENT_TO_BRAIN_ADAPTER.validate_json(bridge_hello)
