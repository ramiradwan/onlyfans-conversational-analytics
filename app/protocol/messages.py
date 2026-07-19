"""Role-specific discriminated unions for WebSocket protocol v2."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias, Union
from uuid import UUID

from pydantic import Field, TypeAdapter

from .common import StrictModel
from .payloads import *


class WebSocketMessage(StrictModel):
    protocol_version: Literal["2"]
    message_id: UUID
    correlation_id: UUID | None = None


def _message(name: str, value: str, payload_type):
    return type(name, (WebSocketMessage,), {
        "__annotations__": {"type": Literal[value], "payload": payload_type},
        "__module__": __name__,
    })


AgentHelloMessage = _message("AgentHelloMessage", "agent.hello", AgentHelloPayload)
AgentSessionMessage = _message("AgentSessionMessage", "agent.session", AgentSessionPayload)
BridgeHelloMessage = _message("BridgeHelloMessage", "bridge.hello", BridgeHelloPayload)
BridgeSessionMessage = _message("BridgeSessionMessage", "bridge.session", BridgeSessionPayload)
AgentHeartbeatMessage = _message("AgentHeartbeatMessage", "agent.heartbeat", AgentHeartbeatPayload)
SyncRequiredMessage = _message("SyncRequiredMessage", "sync.required", SyncRequiredPayload)
IngestSnapshotMessage = _message("IngestSnapshotMessage", "ingest.snapshot", IngestSnapshotPayload)
IngestDeltaMessage = _message("IngestDeltaMessage", "ingest.delta", IngestDeltaPayload)
IngestAckMessage = _message("IngestAckMessage", "ingest.ack", IngestAckPayload)
IngestRejectedMessage = _message("IngestRejectedMessage", "ingest.rejected", IngestRejectedPayload)
StateSnapshotMessage = _message("StateSnapshotMessage", "state.snapshot", StateSnapshotPayload)
StateDeltaMessage = _message("StateDeltaMessage", "state.delta", StateDeltaPayload)
StateResyncMessage = _message("StateResyncMessage", "state.resync", StateResyncPayload)
PresenceObservedMessage = _message("PresenceObservedMessage", "presence.observed", PresenceObservedPayload)
PresenceStateMessage = _message("PresenceStateMessage", "presence.state", PresenceStatePayload)
AgentStateMessage = _message("AgentStateMessage", "agent.state", AgentStatePayload)
SystemStateMessage = _message("SystemStateMessage", "system.state", SystemStatePayload)
ProtocolErrorMessage = _message("ProtocolErrorMessage", "protocol.error", ProtocolErrorPayload)
ConfigAvailableMessage = _message("ConfigAvailableMessage", "config.available", ConfigAvailablePayload)
ConfigAppliedMessage = _message("ConfigAppliedMessage", "config.applied", ConfigAppliedPayload)
CommandExecuteMessage = _message("CommandExecuteMessage", "command.execute", CommandExecutePayload)
CommandResultMessage = _message("CommandResultMessage", "command.result", CommandResultPayload)
CommandResultAckMessage = _message("CommandResultAckMessage", "command.result.ack", CommandResultAckPayload)

AgentToBrainMessage: TypeAlias = Annotated[Union[
    AgentHelloMessage, AgentHeartbeatMessage, IngestSnapshotMessage, IngestDeltaMessage,
    PresenceObservedMessage, ConfigAppliedMessage, CommandResultMessage,
], Field(discriminator="type")]
BrainToAgentMessage: TypeAlias = Annotated[Union[
    AgentSessionMessage, SyncRequiredMessage, IngestAckMessage, IngestRejectedMessage,
    ProtocolErrorMessage, ConfigAvailableMessage, CommandExecuteMessage, CommandResultAckMessage,
], Field(discriminator="type")]
BridgeToBrainMessage: TypeAlias = Annotated[Union[BridgeHelloMessage, StateResyncMessage], Field(discriminator="type")]
BrainToBridgeMessage: TypeAlias = Annotated[Union[
    BridgeSessionMessage, StateSnapshotMessage, StateDeltaMessage, PresenceStateMessage,
    AgentStateMessage, SystemStateMessage, ProtocolErrorMessage,
], Field(discriminator="type")]

AGENT_TO_BRAIN_ADAPTER = TypeAdapter(AgentToBrainMessage)
BRAIN_TO_AGENT_ADAPTER = TypeAdapter(BrainToAgentMessage)
BRIDGE_TO_BRAIN_ADAPTER = TypeAdapter(BridgeToBrainMessage)
BRAIN_TO_BRIDGE_ADAPTER = TypeAdapter(BrainToBridgeMessage)
