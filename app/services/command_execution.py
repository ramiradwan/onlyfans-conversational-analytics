"""Auditable Brain-owned command orchestration and in-memory persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal
from uuid import UUID, uuid4

from app.protocol.common import MessageSendAction


CommandState = Literal["issued", "accepted", "succeeded", "failed", "unknown"]
IdempotencyPolicy = Literal["deduplicate"]
CommandSender = Callable[[dict[str, Any]], Awaitable[None]]
ALLOWED_COMMAND_ACTIONS = frozenset({"message.send"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class CommandDeliveryTarget:
    """Immutable Agent socket identity captured for one delivery attempt."""

    connection_id: UUID
    fencing_token: str
    creator_account_id: str


@dataclass(frozen=True, slots=True)
class CommandTransition:
    state: CommandState
    occurred_at: datetime
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class CommandResultRecord:
    result_id: UUID
    status: Literal["accepted", "succeeded", "failed"]
    completed_at: datetime
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class CommandResultReceipt:
    result_id: UUID
    received_at: datetime
    duplicate: bool
    late: bool


@dataclass(slots=True)
class CommandRecord:
    command_id: UUID
    creator_account_id: str
    action: dict[str, Any]
    deadline: datetime
    idempotency_policy: IdempotencyPolicy
    issued_at: datetime
    state: CommandState = "issued"
    connection_id: UUID | None = None
    fencing_token: str | None = None
    delivery_attempts: int = 0
    failure_reason: str | None = None
    result: CommandResultRecord | None = None
    result_apply_count: int = 0
    transitions: list[CommandTransition] = field(default_factory=list)
    receipts: list[CommandResultReceipt] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RecordedResult:
    command_id: UUID
    result_id: UUID
    recorded_at: datetime
    duplicate: bool
    late: bool


class CommandAlreadyExistsError(ValueError):
    """Raised when callers attempt to mint an existing command identifier."""


class CommandReissueError(ValueError):
    """Raised when an explicit delivery retry would not be safe."""


class CommandRepository(ABC):
    """DB-swappable repository contract for the Brain command audit log."""

    @abstractmethod
    def get(self, command_id: UUID) -> CommandRecord | None:
        raise NotImplementedError

    @abstractmethod
    def save(self, record: CommandRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def list(self, creator_account_id: str | None = None) -> list[CommandRecord]:
        raise NotImplementedError

    @abstractmethod
    def reset(self) -> None:
        raise NotImplementedError


class InMemoryCommandRepository(CommandRepository):
    """Copy-isolated command repository used until shared storage is selected."""

    def __init__(self) -> None:
        self._records: dict[UUID, CommandRecord] = {}

    def get(self, command_id: UUID) -> CommandRecord | None:
        record = self._records.get(command_id)
        return deepcopy(record) if record is not None else None

    def save(self, record: CommandRecord) -> None:
        self._records[record.command_id] = deepcopy(record)

    def list(self, creator_account_id: str | None = None) -> list[CommandRecord]:
        records = self._records.values()
        if creator_account_id is not None:
            records = (
                record
                for record in records
                if record.creator_account_id == creator_account_id
            )
        return [deepcopy(record) for record in records]

    def reset(self) -> None:
        self._records.clear()


class CommandService:
    """Issue, fence, expire, and deduplicate commands without transport coupling."""

    def __init__(self, repository: CommandRepository) -> None:
        self.repository = repository

    async def issue(
        self,
        *,
        creator_account_id: str,
        action: dict[str, Any],
        deadline: datetime,
        target: CommandDeliveryTarget | None,
        sender: CommandSender | None,
        unavailable_reason: str = "agent_unavailable",
        command_id: UUID | None = None,
        idempotency_policy: IdempotencyPolicy = "deduplicate",
        now: datetime | None = None,
    ) -> CommandRecord:
        issued_at = now or utc_now()
        normalized_action = self._validate_action(action)
        self._validate_deadline(deadline)
        if idempotency_policy != "deduplicate":
            raise ValueError(
                "Commands require command_id deduplication"
            )
        identifier = command_id or uuid4()
        if self.repository.get(identifier) is not None:
            raise CommandAlreadyExistsError(f"Command {identifier} already exists")

        record = CommandRecord(
            command_id=identifier,
            creator_account_id=creator_account_id,
            action=normalized_action,
            deadline=deadline,
            idempotency_policy=idempotency_policy,
            issued_at=issued_at,
            transitions=[CommandTransition("issued", issued_at)],
        )
        self.repository.save(record)

        if deadline <= issued_at:
            self._transition(record, "unknown", issued_at, "deadline_expired")
            return self._save_and_get(record)
        if target is None or sender is None:
            self._transition(record, "failed", issued_at, unavailable_reason)
            return self._save_and_get(record)
        if target.creator_account_id != creator_account_id:
            self._transition(record, "failed", issued_at, "delivery_identity_conflict")
            return self._save_and_get(record)

        record.connection_id = target.connection_id
        record.fencing_token = target.fencing_token
        record.delivery_attempts = 1
        self.repository.save(record)
        try:
            await sender(self._execute_payload(record, target))
        except Exception:
            self._transition(record, "failed", utc_now(), "delivery_failed")
        return self._save_and_get(record)

    async def reissue(
        self,
        command_id: UUID,
        *,
        target: CommandDeliveryTarget | None,
        sender: CommandSender | None,
        unavailable_reason: str = "agent_unavailable",
        now: datetime | None = None,
    ) -> CommandRecord:
        """Explicitly redeliver only the same deduplicated command identity."""

        record = self._required(command_id)
        current_time = now or utc_now()
        if record.idempotency_policy != "deduplicate":
            raise CommandReissueError(
                "Non-idempotent commands cannot be reissued without deduplication"
            )
        if record.state not in {"issued", "accepted"}:
            raise CommandReissueError(
                f"Command in {record.state} state cannot be reissued"
            )
        if record.deadline <= current_time:
            self._transition(record, "unknown", current_time, "deadline_expired")
            return self._save_and_get(record)
        if target is None or sender is None:
            self._transition(record, "failed", current_time, unavailable_reason)
            return self._save_and_get(record)
        if target.creator_account_id != record.creator_account_id:
            self._transition(
                record, "failed", current_time, "delivery_identity_conflict"
            )
            return self._save_and_get(record)

        record.connection_id = target.connection_id
        record.fencing_token = target.fencing_token
        record.delivery_attempts += 1
        self.repository.save(record)
        try:
            await sender(self._execute_payload(record, target))
        except Exception:
            self._transition(record, "failed", utc_now(), "delivery_failed")
        return self._save_and_get(record)

    def record_result(
        self, payload: Any, *, received_at: datetime | None = None
    ) -> RecordedResult:
        received = received_at or utc_now()
        value = (
            payload.model_dump(mode="python")
            if hasattr(payload, "model_dump")
            else dict(payload)
        )
        command_id = UUID(str(value["command_id"]))
        result_id = UUID(str(value["result_id"]))
        record = self.repository.get(command_id)
        if record is None:
            record = CommandRecord(
                command_id=command_id,
                creator_account_id=str(value["creator_account_id"]),
                action={},
                deadline=value["completed_at"],
                idempotency_policy="deduplicate",
                issued_at=received,
                state="unknown",
                failure_reason="result_without_issued_record",
                transitions=[
                    CommandTransition(
                        "unknown", received, "result_without_issued_record"
                    )
                ],
            )
        elif record.creator_account_id != str(value["creator_account_id"]):
            raise ValueError(
                "Command result account conflicts with the command audit record"
            )
        duplicate = record.result is not None
        late = record.state in {"unknown", "failed"}

        if not duplicate:
            status = value["status"]
            result = CommandResultRecord(
                result_id=result_id,
                status=status,
                completed_at=value["completed_at"],
                output=deepcopy(value.get("output")),
                error=deepcopy(value.get("error")),
                recorded_at=received,
            )
            record.result = result
            record.result_apply_count += 1
            if not late:
                self._transition(record, status, received)

        record.receipts.append(
            CommandResultReceipt(
                result_id=result_id,
                received_at=received,
                duplicate=duplicate,
                late=late,
            )
        )
        self.repository.save(record)
        return RecordedResult(
            command_id=command_id,
            result_id=result_id,
            recorded_at=received,
            duplicate=duplicate,
            late=late,
        )

    def expire(self, now: datetime | None = None) -> list[CommandRecord]:
        current_time = now or utc_now()
        expired: list[CommandRecord] = []
        for record in self.repository.list():
            if record.state in {"issued", "accepted"} and record.deadline <= current_time:
                self._transition(record, "unknown", current_time, "deadline_expired")
                self.repository.save(record)
                expired.append(deepcopy(record))
        return expired

    def get(self, command_id: UUID) -> CommandRecord | None:
        return self.repository.get(command_id)

    def list(self, creator_account_id: str | None = None) -> list[CommandRecord]:
        return self.repository.list(creator_account_id)

    def reset(self) -> None:
        self.repository.reset()

    @staticmethod
    def _validate_action(action: dict[str, Any]) -> dict[str, Any]:
        action_type = action.get("type") if isinstance(action, dict) else None
        if action_type not in ALLOWED_COMMAND_ACTIONS:
            raise ValueError("Command action is not in the Brain allow-list")
        return MessageSendAction.model_validate(action).model_dump(mode="json")

    @staticmethod
    def _validate_deadline(deadline: datetime) -> None:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("Command deadline must include a timezone")

    @staticmethod
    def _transition(
        record: CommandRecord,
        state: CommandState,
        occurred_at: datetime,
        detail: str | None = None,
    ) -> None:
        record.state = state
        record.failure_reason = detail if state in {"failed", "unknown"} else None
        record.transitions.append(CommandTransition(state, occurred_at, detail))

    @staticmethod
    def _execute_payload(
        record: CommandRecord, target: CommandDeliveryTarget
    ) -> dict[str, Any]:
        return {
            "connection_id": str(target.connection_id),
            "fencing_token": target.fencing_token,
            "creator_account_id": target.creator_account_id,
            "command_id": str(record.command_id),
            "deadline": record.deadline.isoformat(),
            "idempotency_policy": record.idempotency_policy,
            "action": deepcopy(record.action),
        }

    def _required(self, command_id: UUID) -> CommandRecord:
        record = self.repository.get(command_id)
        if record is None:
            raise LookupError(f"Unknown command {command_id}")
        return record

    def _save_and_get(self, record: CommandRecord) -> CommandRecord:
        self.repository.save(record)
        saved = self.repository.get(record.command_id)
        if saved is None:  # pragma: no cover - repository contract invariant
            raise RuntimeError("Command repository lost a saved record")
        return saved
