"""Role-and-direction-specific WebSocket discriminated unions for protocol v1."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias, Union
from uuid import UUID

from pydantic import Field, TypeAdapter

from .common import StrictModel
from .payloads import (
    AgentHeartbeatPayload,
    AgentHelloPayload,
    AgentSessionPayload,
    AgentStatePayload,
    BridgeHelloPayload,
    BridgeSessionPayload,
    CommandExecutePayload,
    CommandResultAckPayload,
    CommandResultPayload,
    ConfigAppliedPayload,
    ConfigAvailablePayload,
    IngestAckPayload,
    IngestDeltaPayload,
    IngestRejectedPayload,
    IngestSnapshotPayload,
    PresenceObservedPayload,
    PresenceStatePayload,
    ProtocolErrorPayload,
    StateDeltaPayload,
    StateResyncPayload,
    StateSnapshotPayload,
    SyncRequiredPayload,
    SystemStatePayload,
)


class WebSocketMessage(StrictModel):
    protocol_version: Literal["1"]
    message_id: UUID
    correlation_id: UUID | None = None


class AgentHelloMessage(WebSocketMessage):
    type: Literal["agent.hello"]
    payload: AgentHelloPayload


class AgentSessionMessage(WebSocketMessage):
    type: Literal["agent.session"]
    payload: AgentSessionPayload


class BridgeHelloMessage(WebSocketMessage):
    type: Literal["bridge.hello"]
    payload: BridgeHelloPayload


class BridgeSessionMessage(WebSocketMessage):
    type: Literal["bridge.session"]
    payload: BridgeSessionPayload


class AgentHeartbeatMessage(WebSocketMessage):
    type: Literal["agent.heartbeat"]
    payload: AgentHeartbeatPayload


class SyncRequiredMessage(WebSocketMessage):
    type: Literal["sync.required"]
    payload: SyncRequiredPayload


class IngestSnapshotMessage(WebSocketMessage):
    type: Literal["ingest.snapshot"]
    payload: IngestSnapshotPayload


class IngestDeltaMessage(WebSocketMessage):
    type: Literal["ingest.delta"]
    payload: IngestDeltaPayload


class IngestAckMessage(WebSocketMessage):
    type: Literal["ingest.ack"]
    payload: IngestAckPayload


class IngestRejectedMessage(WebSocketMessage):
    type: Literal["ingest.rejected"]
    payload: IngestRejectedPayload


class StateSnapshotMessage(WebSocketMessage):
    type: Literal["state.snapshot"]
    payload: StateSnapshotPayload


class StateDeltaMessage(WebSocketMessage):
    type: Literal["state.delta"]
    payload: StateDeltaPayload


class StateResyncMessage(WebSocketMessage):
    type: Literal["state.resync"]
    payload: StateResyncPayload


class PresenceObservedMessage(WebSocketMessage):
    type: Literal["presence.observed"]
    payload: PresenceObservedPayload


class PresenceStateMessage(WebSocketMessage):
    type: Literal["presence.state"]
    payload: PresenceStatePayload


class AgentStateMessage(WebSocketMessage):
    type: Literal["agent.state"]
    payload: AgentStatePayload


class SystemStateMessage(WebSocketMessage):
    type: Literal["system.state"]
    payload: SystemStatePayload


class ProtocolErrorMessage(WebSocketMessage):
    type: Literal["protocol.error"]
    payload: ProtocolErrorPayload


class ConfigAvailableMessage(WebSocketMessage):
    type: Literal["config.available"]
    payload: ConfigAvailablePayload


class ConfigAppliedMessage(WebSocketMessage):
    type: Literal["config.applied"]
    payload: ConfigAppliedPayload


class CommandExecuteMessage(WebSocketMessage):
    type: Literal["command.execute"]
    payload: CommandExecutePayload


class CommandResultMessage(WebSocketMessage):
    type: Literal["command.result"]
    payload: CommandResultPayload


class CommandResultAckMessage(WebSocketMessage):
    type: Literal["command.result.ack"]
    payload: CommandResultAckPayload


AgentToBrainMessage: TypeAlias = Annotated[
    Union[
        AgentHelloMessage,
        AgentHeartbeatMessage,
        IngestSnapshotMessage,
        IngestDeltaMessage,
        PresenceObservedMessage,
        ConfigAppliedMessage,
        CommandResultMessage,
    ],
    Field(discriminator="type"),
]

BrainToAgentMessage: TypeAlias = Annotated[
    Union[
        AgentSessionMessage,
        SyncRequiredMessage,
        IngestAckMessage,
        IngestRejectedMessage,
        ProtocolErrorMessage,
        ConfigAvailableMessage,
        CommandExecuteMessage,
        CommandResultAckMessage,
    ],
    Field(discriminator="type"),
]

BridgeToBrainMessage: TypeAlias = Annotated[
    Union[BridgeHelloMessage, StateResyncMessage],
    Field(discriminator="type"),
]

BrainToBridgeMessage: TypeAlias = Annotated[
    Union[
        BridgeSessionMessage,
        StateSnapshotMessage,
        StateDeltaMessage,
        PresenceStateMessage,
        AgentStateMessage,
        SystemStateMessage,
        ProtocolErrorMessage,
    ],
    Field(discriminator="type"),
]


AGENT_TO_BRAIN_ADAPTER = TypeAdapter(AgentToBrainMessage)
BRAIN_TO_AGENT_ADAPTER = TypeAdapter(BrainToAgentMessage)
BRIDGE_TO_BRAIN_ADAPTER = TypeAdapter(BridgeToBrainMessage)
BRAIN_TO_BRIDGE_ADAPTER = TypeAdapter(BrainToBridgeMessage)
