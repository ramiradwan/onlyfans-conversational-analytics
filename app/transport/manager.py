"""Bound WebSocket sessions and replaceable in-memory transport state.

The state containers in this module deliberately sit behind one manager so a
shared durable implementation can replace them without changing endpoint or
router behavior in a later phase.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
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
from app.persistence.history import IngestResult, InvariantViolation, StreamKey
from app.utils.logger import logger


DEV_AGENT_AUTH_TICKET = "onlyfans-agent-local-ticket-v2"
DEV_BRIDGE_AUTH_TICKET = "onlyfans-bridge-local-ticket-v2"
DEV_PRINCIPAL_ID = "dev-principal"
DEV_ACCOUNT_ID = "dev-creator-account"
REQUIRED_CONFIG_REVISION = BOOTSTRAP_CONFIG_REVISION
HEARTBEAT_INTERVAL_SECONDS = settings.agent_heartbeat_interval_seconds
LEASE_TIMEOUT_SECONDS = settings.agent_lease_timeout_seconds
LEASE_EXPIRED_CLOSE_CODE = 4001
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
    config_auth_ticket: str
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


@dataclass(frozen=True, slots=True)
class AgentPairingGrant:
    principal_id: str
    creator_account_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AgentConfigGrant:
    creator_account_id: str
    agent_installation_id: UUID
    expires_at: datetime


class AuthenticationError(ValueError):
    """Raised when the development ticket cannot authorize a hello."""


class AuthorizationError(ValueError):
    """Raised when an authenticated development principal cannot access an account."""


class InMemoryTransportManager:
    """Account-partitioned sessions, leases, presence, and ingestion cursors."""

    def __init__(self, repositories: CanonicalRepositories | None = None) -> None:
        repositories = repositories or create_canonical_repositories("memory")
        # Retain the repository aggregate so its disposable TemporaryDirectory
        # remains alive for the full manager lifetime in isolated tests.
        self._repositories = repositories
        self.active_agents: dict[str, AgentLease] = {}
        self.agent_connections: dict[UUID, AgentLease] = {}
        self.bridges: dict[UUID, BridgeBinding] = {}
        self.presence: dict[str, PresenceRecord] = {}
        self._agent_pairing_grants: dict[str, AgentPairingGrant] = {}
        self._agent_config_grants: dict[str, AgentConfigGrant] = {}
        self.history = repositories.history
        self.projection = repositories.projection
        self.ingestion = repositories.ingestion
        self.projection_activation = repositories.projection_activation
        self.config_authority = AgentConfigurationAuthority(repositories.configuration)
        self.commands = CommandService(repositories.commands)
        self.canonical_database = repositories.database
        self.projection_database = repositories.projection_database
        self._agent_command_lock = asyncio.Lock()
        self._sweeper_task: asyncio.Task[None] | None = None
        self._state_delta_queues: dict[str, list[dict[str, Any]]] = {}
        self._state_delta_tasks: dict[str, asyncio.Task[None]] = {}
        self._projection_tasks: dict[
            str, asyncio.Task[dict[str, Any] | None]
        ] = {}
        self._projection_pending_accounts: set[str] = set()

    def _development_stub_allowed(self) -> bool:
        auth_mode = settings.websocket_auth_mode
        bind_host = getattr(settings, "websocket_bind_host", "127.0.0.1")
        environment = settings.environment.lower()
        local_hosts = {"127.0.0.1", "localhost", "::1"}
        return (
            auth_mode == "development_stub"
            and environment in {"development", "dev", "local", "test"}
            and bind_host in local_hosts
        )

    def _local_runtime_allowed(self) -> bool:
        return getattr(settings, "websocket_bind_host", "127.0.0.1") in {
            "127.0.0.1",
            "localhost",
            "::1",
        }

    def validate_auth_configuration(self) -> None:
        if not self._local_runtime_allowed():
            raise RuntimeError("Brain runtime authentication requires a loopback bind host")
        if (
            settings.websocket_auth_mode == "development_stub"
            and not self._development_stub_allowed()
        ):
            raise RuntimeError(
                "Development stub authentication requires a local development exposure"

            )

    def consume_launcher_bootstrap(
        self,
        ticket: str,
        *,
        principal_id: str,
        creator_account_id: str,
    ) -> bool:
        """Atomically consume a launcher credential by hash in durable local state."""
        ticket_hash = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
        with self.canonical_database.transaction() as connection:
            inserted = connection.execute(
                """INSERT OR IGNORE INTO launcher_bootstrap_consumptions(
                       ticket_hash,principal_id,creator_account_id,consumed_at
                   ) VALUES (?,?,?,?)""",
                (
                    ticket_hash,
                    principal_id,
                    creator_account_id,
                    utc_now().isoformat(),
                ),
            )
            return inserted.rowcount == 1

    @staticmethod
    def _ticket_key(ticket: str) -> str:
        return hashlib.sha256(ticket.encode("utf-8")).hexdigest()

    def _expire_auth_grants(self, now: datetime) -> None:
        self._agent_pairing_grants = {
            key: grant
            for key, grant in self._agent_pairing_grants.items()
            if grant.expires_at > now
        }
        self._agent_config_grants = {
            key: grant
            for key, grant in self._agent_config_grants.items()
            if grant.expires_at > now
        }

    @staticmethod
    def _runtime_ticket_secret() -> bytes:
        return settings.security_signing_secret.get_secret_value().encode("utf-8")

    @staticmethod
    def _base64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    def _issue_runtime_ticket(
        self,
        *,
        purpose: Literal["agent-reconnect", "agent-config", "bridge-websocket"],
        principal_id: str,
        creator_account_id: str,
        ttl_seconds: int,
        agent_installation_id: UUID | None = None,
        bridge_session_id: UUID | None = None,
        role: Literal["creator", "operator", "agent"] = "agent",
        now: datetime | None = None,
    ) -> str:
        issued_at = now or utc_now()
        issued_timestamp = int(issued_at.timestamp())
        document = {
            "account": creator_account_id,
            "bridge_session": None if bridge_session_id is None else str(bridge_session_id),
            "exp": issued_timestamp + ttl_seconds,
            "iat": issued_timestamp,
            "installation": (
                None if agent_installation_id is None else str(agent_installation_id)
            ),
            "nonce": secrets.token_urlsafe(18),
            "principal": principal_id,
            "purpose": purpose,
            "role": role,
            "v": 2,
        }
        payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        signature = hmac.new(
            self._runtime_ticket_secret(), payload, hashlib.sha256
        ).digest()
        return f"local-v2.{self._base64(payload)}.{self._base64(signature)}"

    def _verify_runtime_ticket(
        self,
        ticket: str,
        *,
        purpose: Literal["agent-reconnect", "agent-config", "bridge-websocket"],
        creator_account_id: str,
        agent_installation_id: UUID | None = None,
        bridge_session_id: UUID | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        try:
            prefix, encoded_payload, encoded_signature = ticket.split(".", 2)
            if prefix != "local-v2":
                raise ValueError("prefix")
            payload = base64.urlsafe_b64decode(
                (encoded_payload + "=" * (-len(encoded_payload) % 4)).encode("ascii")
            )
            signature = base64.urlsafe_b64decode(
                (encoded_signature + "=" * (-len(encoded_signature) % 4)).encode("ascii")
            )
            expected = hmac.new(
                self._runtime_ticket_secret(), payload, hashlib.sha256
            ).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            document = json.loads(payload)
            expires_at = int(document["exp"])
            issued_at = int(document["iat"])
        except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as error:
            raise AuthenticationError("Runtime ticket is invalid") from error
        expected_installation = (
            None if agent_installation_id is None else str(agent_installation_id)
        )
        expected_bridge_session = (
            None if bridge_session_id is None else str(bridge_session_id)
        )
        if document != {
            "account": creator_account_id,
            "bridge_session": expected_bridge_session,
            "exp": expires_at,
            "iat": issued_at,
            "installation": expected_installation,
            "nonce": document.get("nonce"),
            "principal": document.get("principal"),
            "purpose": purpose,
            "role": document.get("role"),
            "v": 2,
        }:
            raise AuthenticationError("Runtime ticket binding is invalid")
        authenticated_at = int((now or utc_now()).timestamp())
        if issued_at > authenticated_at or expires_at <= authenticated_at:
            raise AuthenticationError("Runtime ticket has expired")
        if not isinstance(document["principal"], str) or not document["principal"]:
            raise AuthenticationError("Runtime ticket principal is invalid")
        if not isinstance(document.get("nonce"), str) or not document["nonce"]:
            raise AuthenticationError("Runtime ticket nonce is invalid")
        maximum_lifetime = {
            "agent-reconnect": settings.agent_reconnect_ticket_ttl_seconds,
            "agent-config": settings.agent_config_ticket_ttl_seconds,
            "bridge-websocket": settings.bridge_ticket_ttl_seconds,
        }[purpose]
        if expires_at - issued_at <= 0 or expires_at - issued_at > maximum_lifetime:
            raise AuthenticationError("Runtime ticket lifetime is invalid")
        if purpose.startswith("agent-") and document["role"] != "agent":
            raise AuthenticationError("Runtime ticket role is invalid")
        if purpose == "bridge-websocket" and document["role"] not in {
            "creator",
            "operator",
        }:
            raise AuthenticationError("Runtime ticket role is invalid")
        return document

    def issue_bridge_ticket(
        self,
        *,
        principal_id: str,
        creator_account_id: str,
        role: Literal["creator", "operator"],
        bridge_session_id: UUID | None = None,
        ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> str:
        return self._issue_runtime_ticket(
            purpose="bridge-websocket",
            principal_id=principal_id,
            creator_account_id=creator_account_id,
            bridge_session_id=bridge_session_id,
            role=role,
            ttl_seconds=ttl_seconds or settings.bridge_ticket_ttl_seconds,
            now=now,
        )

    def issue_agent_pairing_ticket(
        self,
        *,
        principal_id: str,
        creator_account_id: str,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        if not self._local_runtime_allowed():
            raise AuthenticationError("Local Agent pairing is disabled")
        issued_at = now or utc_now()
        self._expire_auth_grants(issued_at)
        ticket = f"pair-{secrets.token_urlsafe(32)}"
        expires_at = issued_at + timedelta(
            seconds=settings.agent_pairing_ticket_ttl_seconds
        )
        self._agent_pairing_grants[self._ticket_key(ticket)] = AgentPairingGrant(
            principal_id=principal_id,
            creator_account_id=creator_account_id,
            expires_at=expires_at,
        )
        return ticket, expires_at

    def authenticate_agent_handshake(
        self,
        auth_ticket: str,
        requested_account: str,
        agent_installation_id: UUID,
        *,
        now: datetime | None = None,
    ) -> tuple[str, str, str, str]:
        """Consume bootstrap once or verify a bounded stateless reconnect ticket.

        Renewal intentionally overlaps the prior reconnect credential until
        its signed expiry so loss of the `agent.session` response cannot lock
        out an installation. Purpose/account/installation/lifetime remain
        immutable, and pairing tickets themselves are still single-use.
        """
        authenticated_at = now or utc_now()
        self._expire_auth_grants(authenticated_at)
        if (
            self._development_stub_allowed()
            and auth_ticket == DEV_AGENT_AUTH_TICKET
            and requested_account == DEV_ACCOUNT_ID
        ):
            principal_id = DEV_PRINCIPAL_ID
            account_id = DEV_ACCOUNT_ID
        elif auth_ticket.startswith("local-v2."):
            document = self._verify_runtime_ticket(
                auth_ticket,
                purpose="agent-reconnect",
                creator_account_id=requested_account,
                agent_installation_id=agent_installation_id,
                now=authenticated_at,
            )
            principal_id = str(document["principal"])
            account_id = requested_account
        else:
            grant = self._agent_pairing_grants.pop(self._ticket_key(auth_ticket), None)
            if grant is None or grant.expires_at <= authenticated_at:
                raise AuthenticationError(
                    "Agent pairing ticket is invalid, expired, or already used"
                )
            if grant.creator_account_id != requested_account:
                raise AuthenticationError("Agent pairing ticket is bound to another account")
            principal_id = grant.principal_id
            account_id = grant.creator_account_id
        reconnect_ticket = self._issue_runtime_ticket(
            purpose="agent-reconnect",
            principal_id=principal_id,
            creator_account_id=account_id,
            agent_installation_id=agent_installation_id,
            ttl_seconds=settings.agent_reconnect_ticket_ttl_seconds,
            now=authenticated_at,
        )
        config_ticket = self._issue_runtime_ticket(
            purpose="agent-config",
            principal_id=principal_id,
            creator_account_id=account_id,
            agent_installation_id=agent_installation_id,
            ttl_seconds=settings.agent_config_ticket_ttl_seconds,
            now=authenticated_at,
        )
        return principal_id, account_id, reconnect_ticket, config_ticket
    def authenticate(
        self,
        auth_ticket: str,
        requested_account: str,
        *,
        role: Literal["agent", "bridge"],
        bridge_session_id: UUID | None = None,
        now: datetime | None = None,
    ) -> tuple[str, str]:
        expected_ticket = (
            DEV_AGENT_AUTH_TICKET if role == "agent" else DEV_BRIDGE_AUTH_TICKET
        )
        if (
            self._development_stub_allowed()
            and auth_ticket == expected_ticket
            and requested_account == DEV_ACCOUNT_ID
        ):
            return DEV_PRINCIPAL_ID, DEV_ACCOUNT_ID
        if role == "bridge":
            try:
                document = self._verify_runtime_ticket(
                    auth_ticket,
                    purpose="bridge-websocket",
                    creator_account_id=requested_account,
                    bridge_session_id=bridge_session_id,
                    now=now,
                )
            except AuthenticationError:
                # A page-bootstrap ticket predates the client-generated page
                # session id. Explicitly session-bound tickets never match
                # this unbound shape and therefore cannot be downgraded.
                document = self._verify_runtime_ticket(
                    auth_ticket,
                    purpose="bridge-websocket",
                    creator_account_id=requested_account,
                    bridge_session_id=None,
                    now=now,
                )
            return str(document["principal"]), requested_account
        raise AuthenticationError("Invalid authentication ticket or unauthorized account")

    def authenticate_agent_config(
        self,
        auth_ticket: str,
        requested_account: str,
        agent_installation_id: UUID,
        *,
        now: datetime | None = None,
    ) -> tuple[str, str]:
        if (
            self._development_stub_allowed()
            and auth_ticket == DEV_AGENT_AUTH_TICKET
            and requested_account == DEV_ACCOUNT_ID
        ):
            return DEV_PRINCIPAL_ID, DEV_ACCOUNT_ID
        document = self._verify_runtime_ticket(
            auth_ticket,
            purpose="agent-config",
            creator_account_id=requested_account,
            agent_installation_id=agent_installation_id,
            now=now,
        )
        return str(document["principal"]), requested_account

    async def start(self) -> None:
        self.validate_auth_configuration()
        if self._sweeper_task is None or self._sweeper_task.done():
            self._sweeper_task = asyncio.create_task(self._sweep(), name="phase2-transport-expiry")
        pending_accounts = await asyncio.to_thread(self.projection.pending_accounts)
        for account_id in pending_accounts:
            self.schedule_projection(account_id)

    async def stop(self) -> None:
        task = self._sweeper_task
        self._sweeper_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        projection_tasks = list(self._projection_tasks.values())
        if projection_tasks:
            await asyncio.gather(*projection_tasks, return_exceptions=True)

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
        self._agent_pairing_grants.clear()
        self._agent_config_grants.clear()
        self.projection.reset()
        self.history.reset()
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
        for task in self._projection_tasks.values():
            if not task.done():
                task.cancel()
        self._projection_tasks.clear()
        self._projection_pending_accounts.clear()

    async def bind_agent(
        self,
        websocket: WebSocket,
        *,
        principal_id: str,
        creator_account_id: str,
        agent_installation_id: UUID,
        agent_stream_id: UUID,
        config_auth_ticket: str = DEV_AGENT_AUTH_TICKET,
        applied_config_revision: str | None,
        now: datetime | None = None,
    ) -> AgentLease:
        config_record = self.config_authority.bind_installation(
            creator_account_id,
            agent_installation_id,
            applied_config_revision,
        )
        history_settings = self.history.history_settings(creator_account_id)
        if history_settings["required_config_revision"] is None:
            self.history.bind_history_config(
                creator_account_id,
                settings_revision=int(history_settings["settings_revision"]),
                config_revision=config_record.required_config_revision,
            )
        if (
            config_record.applied_config_revision
            == config_record.required_config_revision
        ):
            self.history.mark_history_config_applied(
                creator_account_id, config_record.required_config_revision
            )
        lease = AgentLease(
            websocket=websocket,
            principal_id=principal_id,
            creator_account_id=creator_account_id,
            connection_id=uuid4(),
            fencing_token=f"fence-{secrets.token_urlsafe(24)}",
            agent_installation_id=agent_installation_id,
            agent_stream_id=agent_stream_id,
            config_auth_ticket=config_auth_ticket,
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
        return self.history.checkpoint(self.stream_key(lease))

    def pending_snapshot_for(self, lease: AgentLease) -> tuple[UUID, int] | None:
        return self.history.pending_snapshot(self.stream_key(lease))

    def ingest_snapshot(self, lease: AgentLease, payload: Any) -> IngestResult:
        key = self.stream_key(lease)
        if payload.frame_kind == "begin":
            return self.history.begin_snapshot(key, payload)
        if payload.frame_kind == "chunk":
            return self.history.add_snapshot_chunk(key, payload)
        if payload.frame_kind == "commit":
            return self.history.commit_snapshot(key, payload)
        raise InvariantViolation(f"unsupported snapshot frame {payload.frame_kind!r}")

    def ingest_delta(self, lease: AgentLease, payload: Any) -> IngestResult:
        return self.history.commit_delta(self.stream_key(lease), payload)

    async def _run_projection_worker(self, account_id: str) -> dict[str, Any] | None:
        latest: dict[str, Any] | None = None
        while True:
            self._projection_pending_accounts.discard(account_id)
            snapshot = await asyncio.to_thread(self.projection.catch_up, account_id)
            if snapshot is None:
                if account_id in self._projection_pending_accounts:
                    continue
                return latest
            latest = snapshot
            await self._broadcast_bridge(account_id, "state.snapshot", snapshot)
            # The newly activated generation makes readiness recover; system.state
            # is otherwise a bind-only one-shot, so without this the Bridge keeps
            # rendering the freshly delivered data under a stale "unavailable" /
            # "degraded" readiness that never corrects itself.
            await self.broadcast_system_state(account_id)

    def schedule_projection(
        self, account_id: str
    ) -> asyncio.Task[dict[str, Any] | None]:
        """Serialize one account's durable projection work outside the event loop."""
        self._projection_pending_accounts.add(account_id)
        existing = self._projection_tasks.get(account_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(
            self._run_projection_worker(account_id),
            name=f"projection:{account_id}",
        )
        self._projection_tasks[account_id] = task

        def completed(done: asyncio.Task[dict[str, Any] | None]) -> None:
            if self._projection_tasks.get(account_id) is done:
                self._projection_tasks.pop(account_id, None)
                self._projection_pending_accounts.discard(account_id)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[PROJECTION] Account projection worker failed")

        task.add_done_callback(completed)
        return task

    async def project_committed_state(self, account_id: str) -> dict[str, Any] | None:
        """Compatibility awaitable for callers that explicitly need activation completion."""
        return await asyncio.shield(self.schedule_projection(account_id))

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
        if record.applied_config_revision is not None:
            self.history.mark_history_config_applied(
                lease.creator_account_id, record.applied_config_revision
            )
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
        expired_leases: list[tuple[str, AgentLease]] = []
        for account_id, lease in list(self.active_agents.items()):
            age = (now - lease.last_heartbeat_at).total_seconds()
            if age >= lease.lease_timeout_seconds * 2:
                hard_expired = False
                async with self._agent_command_lock:
                    if self.active_agents.get(account_id) is not lease:
                        continue
                    age = (now - lease.last_heartbeat_at).total_seconds()
                    if age >= lease.lease_timeout_seconds * 2:
                        self.active_agents.pop(account_id, None)
                        self.agent_connections.pop(lease.connection_id, None)
                        lease.status = "disconnected"
                        expired_leases.append((account_id, lease))
                        hard_expired = True
                if hard_expired:
                    continue

            # A transport-level disconnect is authoritative until the lease is
            # hard-retired. Only a fresh handshake can make that Agent live again.
            if lease.status == "disconnected":
                continue

            desired: Literal["connected", "stale"]
            if age >= lease.lease_timeout_seconds:
                desired = "stale"
            else:
                desired = "connected"
            if desired != lease.status:
                lease.status = desired
                await self.broadcast_agent_state(account_id)

        for account_id, lease in expired_leases:
            try:
                await lease.websocket.close(
                    code=LEASE_EXPIRED_CLOSE_CODE,
                    reason="Agent heartbeat lease expired",
                )
            except Exception:
                # The transport may already have disappeared; lease retirement is authoritative.
                pass
            await self.broadcast_agent_state(account_id)

        for account_id, record in list(self.presence.items()):
            if record.freshness == "current" and now >= record.expires_at:
                record.freshness = "unknown"
                record.online_platform_user_ids = []
                await self.broadcast_presence_state(account_id)

    def agent_state_payload(self, account_id: str) -> dict[str, Any]:
        lease = self.active_agents.get(account_id)
        history_settings = self.history.history_settings(account_id)
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
                "required_history_settings_revision": int(history_settings["settings_revision"]),
                "applied_history_settings_revision": history_settings["effective_settings_revision"],
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
        history_drift = (
            history_settings["effective_settings_revision"]
            != history_settings["settings_revision"]
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
        elif history_drift:
            reason = "Historical acquisition settings are waiting for Agent confirmation"
        elif config_record.last_failure:
            reason = f"Agent configuration is degraded: {config_record.last_failure}"
        return {
            "creator_account_id": account_id,
            "status": lease.status,
            "agent_installation_id": str(lease.agent_installation_id) if connected else None,
            "connection_id": str(lease.connection_id) if connected else None,
            "required_config_revision": config_record.required_config_revision,
            "applied_config_revision": config_record.applied_config_revision,
            "required_history_settings_revision": int(history_settings["settings_revision"]),
            "applied_history_settings_revision": history_settings["effective_settings_revision"],
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
        return self.projection.snapshot(account_id)

    def system_state_payload(self, account_id: str) -> dict[str, Any]:
        coverage = self.history.coverage(account_id)
        projection = self.projection.state(account_id)
        freshness = self.history.live_freshness(account_id)
        history_settings = self.history.history_settings(account_id)
        configuration_aligned = (
            history_settings["desired_state"] == history_settings["effective_state"]
            and history_settings["required_config_revision"] is not None
            and history_settings["required_config_revision"]
            == history_settings["effective_config_revision"]
            and history_settings["effective_settings_revision"] is not None
            and int(history_settings["settings_revision"])
            == int(history_settings["effective_settings_revision"])
        )
        if self.history.account_has_pending_snapshot(account_id):
            processing_mode = "processing_snapshot"
        elif projection["status"] == "pending":
            processing_mode = "resyncing"
        else:
            processing_mode = "realtime"
        up_to_date = (
            coverage["status"] == "complete"
            and projection["status"] == "current"
            and projection["projected_revision"] >= projection["canonical_revision"]
            and freshness["status"] == "current"
            and configuration_aligned
        )
        if up_to_date:
            readiness = "ready"
            detail = "Coverage, projection, live freshness, and settings are aligned"
        elif projection["status"] in {"unavailable", "degraded"}:
            readiness = "unavailable"
            detail = projection["reason"] or "No valid projection is available"
        else:
            readiness = "degraded"
            reasons = [
                f"coverage={coverage['status']}",
                f"projection={projection['status']}",
                f"live={freshness['status']}",
                f"configuration={'aligned' if configuration_aligned else 'pending'}",
            ]
            detail = ", ".join(reasons)
        return {
            "creator_account_id": account_id,
            "processing_mode": processing_mode,
            "readiness": readiness,
            "updated_at": utc_now().isoformat(),
            "detail": detail,
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
            "protocol_version": "2",
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

    async def broadcast_system_state(self, account_id: str) -> None:
        """Re-emit the replaceable readiness signal after any input to it changes."""
        await self._broadcast_bridge(account_id, "system.state", self.system_state_payload(account_id))

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
        if (
            payload.outcome == "applied"
            and record.applied_config_revision == payload.config_revision
        ):
            self.history.mark_history_config_applied(
                lease.creator_account_id, payload.config_revision
            )
        await self.broadcast_agent_state(lease.creator_account_id)
        # Applying config can flip configuration_aligned; refresh readiness so the
        # dashboard leaves "configuration=pending" without waiting for a reconnect.
        await self.broadcast_system_state(lease.creator_account_id)

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
        history_acquisition: dict[str, Any] | None = None,
        issued_at: datetime | None = None,
        signal_available: bool = True,
    ):
        document = await self.config_authority.publish(
            account_id,
            capture_policy=capture_policy,
            command_policy=command_policy,
            history_acquisition=history_acquisition,
            issued_at=issued_at,
        )
        await self.broadcast_agent_state(account_id)
        if signal_available:
            await self.signal_config_available(account_id)
        return document

    async def publish_history_settings(
        self, account_id: str, history_settings: dict[str, Any]
    ):
        """Publish one immutable Agent document and bind it to the desired settings revision."""
        current = self.required_config_document(account_id)
        desired_state = str(history_settings["desired_state"])
        document = await self.publish_config(
            account_id,
            capture_policy=current.capture_policy.model_dump(mode="json"),
            command_policy=current.command_policy.model_dump(mode="json"),
            history_acquisition={
                "enabled": desired_state == "running",
                "consent_revision": history_settings["consent_revision"],
                "authorized_platform_creator_id": history_settings[
                    "authorized_platform_creator_id"
                ],
                "recent_window_days": int(history_settings["recent_window_days"]),
                "page_size": int(history_settings["page_size"]),
                "pages_per_wake": int(history_settings["pages_per_wake"]),
                "request_interval_ms": int(history_settings["request_interval_ms"]),
                "retry_limit": int(history_settings["retry_limit"]),
            },
            signal_available=False,
        )
        self.history.bind_history_config(
            account_id,
            settings_revision=int(history_settings["settings_revision"]),
            config_revision=document.config_revision,
        )
        lease = self.active_agents.get(account_id)
        if (
            lease is not None
            and lease.applied_config_revision == document.config_revision
        ):
            self.history.mark_history_config_applied(
                account_id, document.config_revision
            )
        await self.signal_config_available(account_id)
        await self.broadcast_agent_state(account_id)
        return document


transport_manager = InMemoryTransportManager(
    create_canonical_repositories(
        settings.canonical_persistence_backend,
        canonical_path=(
            settings.canonical_database_path
            if settings.canonical_persistence_backend == "sqlite"
            else None
        ),
        projection_path=(
            settings.projection_database_path
            if settings.canonical_persistence_backend == "sqlite"
            else None
        ),
    )
)
