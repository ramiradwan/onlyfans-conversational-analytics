"""Bound WebSocket sessions and replaceable in-memory transport state.

The state containers in this module deliberately sit behind one manager so a
shared durable implementation can replace them without changing endpoint or
router behavior in a later phase.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import WebSocket

from app.core.config import settings
from app.protocol import BRAIN_TO_AGENT_ADAPTER, BRAIN_TO_BRIDGE_ADAPTER
from app.services.agent_configuration import (
    BOOTSTRAP_CONFIG_REVISION,
    AgentConfigurationAuthority,
)
from app.services.command_execution import (
    CommandDeliveryTarget,
    CommandRecord,
    CommandService,
)
from app.persistence.factory import CanonicalRepositories, create_canonical_repositories
from app.transport.ingestion import IngestionService, StreamKey


DEV_AUTH_TICKET = "bridge-clean-dev-ticket-v1"
DEV_PRINCIPAL_ID = "dev-principal"
DEV_ACCOUNT_ID = "dev-creator-account"
REQUIRED_CONFIG_REVISION = BOOTSTRAP_CONFIG_REVISION
HEARTBEAT_INTERVAL_SECONDS = 20
LEASE_TIMEOUT_SECONDS = 60
PRESENCE_TTL_SECONDS = 120
STATE_DELTA_FLUSH_SECONDS = 0.1


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class AgentLease:
    websocket: WebSocket
    principal_id: str
    creator_account_id: str
    connection_id: UUID
    fencing_token: str
    agent_installation_id: UUID
    agent_stream_id: UUID
    applied_config_revision: str | None
    last_heartbeat_at: datetime
    lease_timeout_seconds: int = LEASE_TIMEOUT_SECONDS
    status: Literal["connected", "stale", "disconnected"] = "connected"


@dataclass(slots=True)
class BridgeBinding:
    websocket: WebSocket
    principal_id: str
    creator_account_id: str
    connection_id: UUID
    bridge_session_id: UUID


@dataclass(slots=True)
class PresenceRecord:
    creator_account_id: str
    observation_id: int
    observed_at: datetime
    server_received_at: datetime
    expires_at: datetime
    online_platform_user_ids: list[str]
    freshness: Literal["current", "unknown"] = "current"


class AuthenticationError(ValueError):
    """Raised when the development ticket cannot authorize a hello."""


class AuthorizationError(ValueError):
    """Raised when an authenticated development principal cannot access an account."""


class InMemoryTransportManager:
    """Account-partitioned sessions, leases, presence, and ingestion cursors."""

    def __init__(self, repositories: CanonicalRepositories | None = None) -> None:
        repositories = repositories or create_canonical_repositories("memory")
        self.active_agents: dict[str, AgentLease] = {}
        self.agent_connections: dict[UUID, AgentLease] = {}
        self.bridges: dict[UUID, BridgeBinding] = {}
        self.presence: dict[str, PresenceRecord] = {}
        self.ingestion = IngestionService(repositories.ingestion)
        self.config_authority = AgentConfigurationAuthority(repositories.configuration)
        self.commands = CommandService(repositories.commands)
        self.canonical_database = repositories.database
        self._agent_command_lock = asyncio.Lock()
        self._sweeper_task: asyncio.Task[None] | None = None
        self._state_delta_queues: dict[str, list[dict[str, Any]]] = {}
        self._state_delta_tasks: dict[str, asyncio.Task[None]] = {}

    def _development_stub_allowed(self) -> bool:
        auth_mode = getattr(settings, "websocket_auth_mode", "development_stub")
        bind_host = getattr(settings, "websocket_bind_host", "127.0.0.1")
        environment = settings.environment.lower()
        local_hosts = {"127.0.0.1", "localhost", "::1"}
        return (
            auth_mode == "development_stub"
            and environment in {"development", "dev", "local", "test"}
            and bind_host in local_hosts
        )

    def validate_auth_configuration(self) -> None:
        if (
            getattr(settings, "websocket_auth_mode", "development_stub")
            == "development_stub"
            and not self._development_stub_allowed()
        ):
            raise RuntimeError(
                "Development stub authentication requires a local development exposure"

            )
    def authenticate(self, auth_ticket: str, requested_account: str) -> tuple[str, str]:
        if not self._development_stub_allowed():
            raise AuthenticationError("Development authentication is disabled")
        if auth_ticket != DEV_AUTH_TICKET or requested_account != DEV_ACCOUNT_ID:
            raise AuthenticationError("Invalid development ticket or unauthorized account")
        return DEV_PRINCIPAL_ID, DEV_ACCOUNT_ID

    def authenticate_agent_config(
        self, auth_ticket: str, requested_account: str
    ) -> tuple[str, str]:
        if not self._development_stub_allowed() or auth_ticket != DEV_AUTH_TICKET:
            raise AuthenticationError("Invalid development authentication ticket")
        if requested_account != DEV_ACCOUNT_ID:
            raise AuthorizationError(
                "The development principal cannot access that account"
            )
        return DEV_PRINCIPAL_ID, DEV_ACCOUNT_ID

    async def start(self) -> None:
        self.validate_auth_configuration()
        if self._sweeper_task is None or self._sweeper_task.done():
            self._sweeper_task = asyncio.create_task(self._sweep(), name="phase2-transport-expiry")

    async def stop(self) -> None:
        task = self._sweeper_task
        self._sweeper_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self.expire(utc_now())

    def reset(self) -> None:
        """Clear replaceable state between isolated application/test runs."""
        self.active_agents.clear()
        self.agent_connections.clear()
        self.bridges.clear()
        self.presence.clear()
        self.ingestion.reset()
        self.commands.reset()
        self.config_authority.reset()
        for task in self._state_delta_tasks.values():
            if not task.done():
                if task.get_loop().is_closed():
                    task._log_destroy_pending = False
                    task.get_coro().close()
                else:
                    task.cancel()
        self._state_delta_tasks.clear()
        self._state_delta_queues.clear()

    async def bind_agent(
        self,
        websocket: WebSocket,
        *,
        principal_id: str,
        creator_account_id: str,
        agent_installation_id: UUID,
        agent_stream_id: UUID,
        applied_config_revision: str | None,
        now: datetime | None = None,
    ) -> AgentLease:
        config_record = self.config_authority.bind_installation(
            creator_account_id,
            agent_installation_id,
            applied_config_revision,
        )
        lease = AgentLease(
            websocket=websocket,
            principal_id=principal_id,
            creator_account_id=creator_account_id,
            connection_id=uuid4(),
            fencing_token=f"fence-{secrets.token_urlsafe(24)}",
            agent_installation_id=agent_installation_id,
            agent_stream_id=agent_stream_id,
            applied_config_revision=config_record.applied_config_revision,
            last_heartbeat_at=now or utc_now(),
        )
        async with self._agent_command_lock:
            previous = self.active_agents.get(creator_account_id)
            if previous is not None:
                previous.status = "disconnected"
            self.active_agents[creator_account_id] = lease
            self.agent_connections[lease.connection_id] = lease
        await self.broadcast_agent_state(creator_account_id)
        return lease

    async def bind_bridge(
        self,
        websocket: WebSocket,
        *,
        principal_id: str,
        creator_account_id: str,
        bridge_session_id: UUID,
    ) -> BridgeBinding:
        binding = BridgeBinding(
            websocket=websocket,
            principal_id=principal_id,
            creator_account_id=creator_account_id,
            connection_id=uuid4(),
            bridge_session_id=bridge_session_id,
        )
        self.bridges[binding.connection_id] = binding
        return binding

    async def disconnect_agent(self, connection_id: UUID) -> None:
        should_broadcast = False
        async with self._agent_command_lock:
            lease = self.agent_connections.pop(connection_id, None)
            if lease is None:
                return
            active = self.active_agents.get(lease.creator_account_id)
            if active is lease:
                lease.status = "disconnected"
                should_broadcast = True
        if should_broadcast:
            await self.broadcast_agent_state(lease.creator_account_id)

    async def disconnect_bridge(self, connection_id: UUID) -> None:
        binding = self.bridges.pop(connection_id, None)
        if binding is None:
            return
        has_account_bridge = any(
            candidate.creator_account_id == binding.creator_account_id
            for candidate in self.bridges.values()
        )
        if not has_account_bridge:
            self._state_delta_queues.pop(binding.creator_account_id, None)
            task = self._state_delta_tasks.pop(binding.creator_account_id, None)
            if task is not None and not task.done():
                if task.get_loop().is_closed():
                    task._log_destroy_pending = False
                    task.get_coro().close()
                else:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    def is_current_fence(self, lease: AgentLease) -> bool:
        return self.active_agents.get(lease.creator_account_id) is lease and lease.status != "disconnected"

    @staticmethod
    def stream_key(lease: AgentLease) -> StreamKey:
        return StreamKey(
            lease.creator_account_id, lease.agent_installation_id, lease.agent_stream_id
        )

    def checkpoint_for(self, lease: AgentLease) -> int | None:
        return self.ingestion.checkpoint(self.stream_key(lease))

    def _command_delivery(
        self, creator_account_id: str
    ) -> tuple[AgentLease | None, CommandDeliveryTarget | None, str]:
        lease = self.active_agents.get(creator_account_id)
        if lease is None:
            return None, None, "agent_disconnected"
        if self.active_agents.get(creator_account_id) is not lease:
            return None, None, "agent_superseded"
        if lease.status != "connected":
            return None, None, f"agent_{lease.status}"
        return (
            lease,
            CommandDeliveryTarget(
                connection_id=lease.connection_id,
                fencing_token=lease.fencing_token,
                creator_account_id=lease.creator_account_id,
            ),
            "",
        )

    async def issue_command(
        self,
        creator_account_id: str,
        *,
        action: dict[str, Any],
        deadline: datetime,
        command_id: UUID | None = None,
        now: datetime | None = None,
    ) -> CommandRecord:
        """Issue only to the currently fenced, non-stale Agent writer."""

        async with self._agent_command_lock:
            lease, target, unavailable_reason = self._command_delivery(
                creator_account_id
            )

            async def sender(payload: dict[str, Any]) -> None:
                if lease is None:  # pragma: no cover - target/sender invariant
                    raise RuntimeError("Command delivery target disappeared")
                await self.send_agent(lease.websocket, "command.execute", payload)

            return await self.commands.issue(
                creator_account_id=creator_account_id,
                action=action,
                deadline=deadline,
                target=target,
                sender=sender if target is not None else None,
                unavailable_reason=unavailable_reason,
                command_id=command_id,
                now=now,
            )

    async def reissue_command(
        self,
        command_id: UUID,
        *,
        now: datetime | None = None,
    ) -> CommandRecord:
        """Redeliver one existing command with its original dedup identity."""

        record = self.commands.get(command_id)
        if record is None:
            raise LookupError(f"Unknown command {command_id}")
        async with self._agent_command_lock:
            lease, target, unavailable_reason = self._command_delivery(
                record.creator_account_id
            )

            async def sender(payload: dict[str, Any]) -> None:
                if lease is None:  # pragma: no cover - target/sender invariant
                    raise RuntimeError("Command delivery target disappeared")
                await self.send_agent(lease.websocket, "command.execute", payload)

            return await self.commands.reissue(
                command_id,
                target=target,
                sender=sender if target is not None else None,
                unavailable_reason=unavailable_reason,
                now=now,
            )

    def command_record(self, command_id: UUID) -> CommandRecord | None:
        return self.commands.get(command_id)

    def command_log(
        self, creator_account_id: str | None = None
    ) -> list[CommandRecord]:
        return self.commands.list(creator_account_id)

    async def heartbeat(
        self,
        lease: AgentLease,
        applied_revision: str | None,
        now: datetime | None = None,
    ) -> None:
        lease.last_heartbeat_at = now or utc_now()
        previous_record = self.config_authority.installation(
            lease.creator_account_id, lease.agent_installation_id
        )
        record = self.config_authority.record_echo(
            lease.creator_account_id,
            lease.agent_installation_id,
            applied_revision,
        )
        changed = (
            lease.status != "connected"
            or lease.applied_config_revision != record.applied_config_revision
            or previous_record.last_failure != record.last_failure
        )
        lease.applied_config_revision = record.applied_config_revision
        lease.status = "connected"
        if changed:
            await self.broadcast_agent_state(lease.creator_account_id)

    def required_config_document(self, account_id: str):
        return self.config_authority.required_document(account_id)

    async def observe_presence(
        self,
        lease: AgentLease,
        *,
        observation_id: int,
        observed_at: datetime,
        online_platform_user_ids: list[str],
        now: datetime | None = None,
    ) -> bool:
        received_at = now or utc_now()
        existing = self.presence.get(lease.creator_account_id)
        if existing is not None and (
            observation_id <= existing.observation_id or observed_at <= existing.observed_at
        ):
            return False
        if observed_at + timedelta(seconds=PRESENCE_TTL_SECONDS) <= received_at:
            return False
        record = PresenceRecord(
            creator_account_id=lease.creator_account_id,
            observation_id=observation_id,
            observed_at=observed_at,
            server_received_at=received_at,
            expires_at=received_at + timedelta(seconds=PRESENCE_TTL_SECONDS),
            online_platform_user_ids=list(online_platform_user_ids),
        )
        self.presence[lease.creator_account_id] = record
        await self.broadcast_presence_state(lease.creator_account_id)
        return True

    async def expire(self, now: datetime) -> None:
        self.commands.expire(now)
        for account_id, lease in list(self.active_agents.items()):
            age = (now - lease.last_heartbeat_at).total_seconds()
            desired: Literal["connected", "stale", "disconnected"]
            if age >= lease.lease_timeout_seconds * 2:
                desired = "disconnected"
            elif age >= lease.lease_timeout_seconds:
                desired = "stale"
            else:
                desired = "connected"
            if desired != lease.status:
                lease.status = desired
                await self.broadcast_agent_state(account_id)

        for account_id, record in list(self.presence.items()):
            if record.freshness == "current" and now >= record.expires_at:
                record.freshness = "unknown"
                record.online_platform_user_ids = []
                await self.broadcast_presence_state(account_id)

    def agent_state_payload(self, account_id: str) -> dict[str, Any]:
        lease = self.active_agents.get(account_id)
        if lease is None:
            return {
                "creator_account_id": account_id,
                "status": "disconnected",
                "agent_installation_id": None,
                "connection_id": None,
                "required_config_revision": self.required_config_document(
                    account_id
                ).config_revision,
                "applied_config_revision": None,
                "last_heartbeat_at": None,
                "degraded_reason": "No active Agent lease",
            }
        config_record = self.config_authority.installation(
            account_id, lease.agent_installation_id
        )
        connected = lease.status != "disconnected"
        config_drift = (
            config_record.applied_config_revision
            != config_record.required_config_revision
        )
        reason = None
        if lease.status == "stale":
            reason = "Agent heartbeat lease is stale"
        elif lease.status == "disconnected":
            reason = "Agent heartbeat lease expired"
        elif config_drift:
            reason = "Agent configuration revision is out of date"
            if config_record.last_failure:
                reason = f"{reason}: {config_record.last_failure}"
        elif config_record.last_failure:
            reason = f"Agent configuration is degraded: {config_record.last_failure}"
        return {
            "creator_account_id": account_id,
            "status": lease.status,
            "agent_installation_id": str(lease.agent_installation_id) if connected else None,
            "connection_id": str(lease.connection_id) if connected else None,
            "required_config_revision": config_record.required_config_revision,
            "applied_config_revision": config_record.applied_config_revision,
            "last_heartbeat_at": lease.last_heartbeat_at.isoformat(),
            "degraded_reason": reason,
        }

    def presence_state_payload(self, account_id: str) -> dict[str, Any]:
        record = self.presence.get(account_id)
        if record is None:
            return {
                "creator_account_id": account_id,
                "freshness": "unknown",
                "online_platform_user_ids": [],
                "server_received_at": None,
                "expires_at": None,
                "last_observation": None,
            }
        return {
            "creator_account_id": account_id,
            "freshness": record.freshness,
            "online_platform_user_ids": list(record.online_platform_user_ids),
            "server_received_at": record.server_received_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
            "last_observation": {
                "observation_id": record.observation_id,
                "observed_at": record.observed_at.isoformat(),
            },
        }

    def state_snapshot_payload(self, account_id: str) -> dict[str, Any]:
        return self.ingestion.state_snapshot_payload(account_id)

    def system_state_payload(self, account_id: str) -> dict[str, Any]:
        return {
            "creator_account_id": account_id,
            "processing_mode": "realtime",
            "readiness": "ready",
            "updated_at": utc_now().isoformat(),
            "detail": "Phase 2 in-memory transport is active",
        }

    async def send_agent(
        self,
        websocket: WebSocket,
        message_type: str,
        payload: dict[str, Any],
        *,
        correlation_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        return await self._send(
            websocket, BRAIN_TO_AGENT_ADAPTER, message_type, payload, correlation_id=correlation_id
        )

    async def send_bridge(
        self,
        websocket: WebSocket,
        message_type: str,
        payload: dict[str, Any],
        *,
        correlation_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        return await self._send(
            websocket, BRAIN_TO_BRIDGE_ADAPTER, message_type, payload, correlation_id=correlation_id
        )

    async def _send(
        self,
        websocket: WebSocket,
        adapter: Any,
        message_type: str,
        payload: dict[str, Any],
        *,
        correlation_id: UUID | str | None,
    ) -> dict[str, Any]:
        document: dict[str, Any] = {
            "type": message_type,
            "protocol_version": "1",
            "message_id": str(uuid4()),
            "payload": payload,
        }
        if correlation_id is not None:
            document["correlation_id"] = str(correlation_id)
        model = adapter.validate_json(json.dumps(document))
        await websocket.send_text(model.model_dump_json())
        return json.loads(model.model_dump_json())

    async def broadcast_agent_state(self, account_id: str) -> None:
        await self._broadcast_bridge(account_id, "agent.state", self.agent_state_payload(account_id))

    async def broadcast_presence_state(self, account_id: str) -> None:
        await self._broadcast_bridge(account_id, "presence.state", self.presence_state_payload(account_id))

    async def broadcast_state_delta(self, account_id: str, payload: dict[str, Any]) -> None:
        """Queue ordered deltas without blocking independent presence delivery."""
        if not any(
            binding.creator_account_id == account_id for binding in self.bridges.values()
        ):
            return
        self._state_delta_queues.setdefault(account_id, []).append(payload)
        task = self._state_delta_tasks.get(account_id)
        if task is None or task.done():
            self._state_delta_tasks[account_id] = asyncio.create_task(
                self._flush_state_deltas(account_id),
                name=f"state-delta-{account_id}",
            )

    async def _flush_state_deltas(self, account_id: str) -> None:
        try:
            await asyncio.sleep(STATE_DELTA_FLUSH_SECONDS)
            queue = self._state_delta_queues.setdefault(account_id, [])
            while queue:
                await self._broadcast_bridge(account_id, "state.delta", queue.pop(0))
        finally:
            self._state_delta_tasks.pop(account_id, None)
            if self._state_delta_queues.get(account_id):
                self._state_delta_tasks[account_id] = asyncio.create_task(
                    self._flush_state_deltas(account_id),
                    name=f"state-delta-{account_id}",
                )

    async def _broadcast_bridge(
        self,
        account_id: str,
        message_type: str,
        payload: dict[str, Any],
    ) -> None:
        stale: list[UUID] = []
        for connection_id, binding in list(self.bridges.items()):
            if binding.creator_account_id != account_id:
                continue
            try:
                await self.send_bridge(binding.websocket, message_type, payload)
            except Exception:
                stale.append(connection_id)
        for connection_id in stale:
            self.bridges.pop(connection_id, None)

    async def record_config_applied(self, lease: AgentLease, payload: Any) -> None:
        record = self.config_authority.record_report(
            lease.creator_account_id,
            lease.agent_installation_id,
            config_revision=payload.config_revision,
            digest=payload.digest,
            outcome=payload.outcome,
            capability_details=(
                capability.detail for capability in payload.capabilities
            ),
        )
        lease.applied_config_revision = record.applied_config_revision
        await self.broadcast_agent_state(lease.creator_account_id)

    async def signal_config_available(self, account_id: str) -> bool:
        lease = self.active_agents.get(account_id)
        if lease is None or lease.status == "disconnected":
            return False
        document = self.required_config_document(account_id)
        await self.send_agent(
            lease.websocket,
            "config.available",
            {
                "connection_id": str(lease.connection_id),
                "creator_account_id": lease.creator_account_id,
                "required_config_revision": document.config_revision,
                "digest": document.digest,
            },
        )
        return True

    async def publish_config(
        self,
        account_id: str,
        *,
        capture_policy: dict[str, Any],
        command_policy: dict[str, Any],
        issued_at: datetime | None = None,
    ):
        document = await self.config_authority.publish(
            account_id,
            capture_policy=capture_policy,
            command_policy=command_policy,
            issued_at=issued_at,
        )
        await self.broadcast_agent_state(account_id)
        await self.signal_config_available(account_id)
        return document


transport_manager = InMemoryTransportManager(
    create_canonical_repositories(
        settings.canonical_persistence_backend,
        canonical_path=(
            settings.canonical_database_path
            if settings.canonical_persistence_backend == "sqlite"
            else None
        ),
    )
)
REQUIRED_CONFIG_DIGEST = transport_manager.required_config_document(
    DEV_ACCOUNT_ID
).digest
