"""Brain-owned immutable Agent configuration authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import UUID

from app.protocol import AgentConfigDocumentResponse


BOOTSTRAP_ACCOUNT_ID = "dev-creator-account"
BOOTSTRAP_CONFIG_REVISION = "config-7"
BOOTSTRAP_ISSUED_AT = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
BOOTSTRAP_CAPTURE_POLICY = {
    "observation_interval_seconds": 30,
    "rules": [
        {
            "resource": "messages",
            "url_pattern": "/api2/v2/chats/*/messages",
            "enabled": True,
        }
    ],
}
BOOTSTRAP_COMMAND_POLICY = {
    "allowed_actions": ["message.send"],
    "max_text_length": 1000,
    "require_idempotency": True,
}


def _digest_content(document: AgentConfigDocumentResponse | dict[str, Any]) -> dict[str, Any]:
    if isinstance(document, AgentConfigDocumentResponse):
        content = document.model_dump(mode="json")
    else:
        content = dict(document)
    content.pop("digest", None)
    content.pop("etag", None)
    return content


def config_document_digest(document: AgentConfigDocumentResponse | dict[str, Any]) -> str:
    """Return the digest of the normalized immutable document content."""
    encoded = json.dumps(
        _digest_content(document),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def build_config_document(
    *,
    creator_account_id: str,
    config_revision: str,
    issued_at: datetime,
    capture_policy: dict[str, Any],
    command_policy: dict[str, Any],
    config_schema_version: str = "1",
) -> AgentConfigDocumentResponse:
    """Normalize a document once, then seal it with its content digest."""
    draft = {
        "operation": "agent.config.document",
        "protocol_version": "1",
        "creator_account_id": creator_account_id,
        "config_revision": config_revision,
        "config_schema_version": config_schema_version,
        "digest": "sha256:" + ("0" * 64),
        "etag": config_revision,
        "issued_at": issued_at,
        "capture_policy": capture_policy,
        "command_policy": command_policy,
    }
    normalized = AgentConfigDocumentResponse.model_validate(draft)
    draft["digest"] = config_document_digest(normalized)
    return AgentConfigDocumentResponse.model_validate(draft)


@dataclass(frozen=True, slots=True)
class ConfigInstallationRecord:
    creator_account_id: str
    agent_installation_id: UUID
    required_config_revision: str
    applied_config_revision: str | None = None
    last_failure: str | None = None


class AgentConfigRepository(ABC):
    """DB-swappable persistence contract for documents and installation state."""

    @abstractmethod
    def add_document(self, document: AgentConfigDocumentResponse) -> None:
        raise NotImplementedError

    @abstractmethod
    def document(
        self, creator_account_id: str, revision: str
    ) -> AgentConfigDocumentResponse | None:
        raise NotImplementedError

    @abstractmethod
    def required_document(self, creator_account_id: str) -> AgentConfigDocumentResponse:
        raise NotImplementedError

    @abstractmethod
    def next_revision(self, creator_account_id: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def set_required(self, creator_account_id: str, revision: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def installation(
        self, creator_account_id: str, agent_installation_id: UUID
    ) -> ConfigInstallationRecord | None:
        raise NotImplementedError

    @abstractmethod
    def save_installation(self, record: ConfigInstallationRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def require_for_account(self, creator_account_id: str, revision: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def reset(self) -> None:
        raise NotImplementedError


class InMemoryAgentConfigRepository(AgentConfigRepository):
    """Immutable JSON-backed repository used until shared DB wiring is selected."""

    def __init__(self) -> None:
        self._documents: dict[tuple[str, str], str] = {}
        self._required: dict[str, str] = {}
        self._last_sequence: dict[str, int] = {}
        self._installations: dict[tuple[str, UUID], ConfigInstallationRecord] = {}

    @staticmethod
    def _sequence(revision: str) -> int:
        prefix, separator, value = revision.rpartition("-")
        if not separator or prefix != "config" or not value.isdigit():
            raise ValueError("Config revisions must use the monotonic config-N form")
        return int(value)

    def add_document(self, document: AgentConfigDocumentResponse) -> None:
        key = (document.creator_account_id, document.config_revision)
        if key in self._documents:
            raise ValueError(f"Configuration revision {document.config_revision} is immutable")
        if document.digest != config_document_digest(document):
            raise ValueError("Configuration digest does not match its immutable content")
        sequence = self._sequence(document.config_revision)
        if sequence <= self._last_sequence.get(document.creator_account_id, -1):
            raise ValueError("Configuration revision must increase monotonically")
        self._documents[key] = document.model_dump_json()
        self._last_sequence[document.creator_account_id] = sequence

    def document(
        self, creator_account_id: str, revision: str
    ) -> AgentConfigDocumentResponse | None:
        encoded = self._documents.get((creator_account_id, revision))
        return AgentConfigDocumentResponse.model_validate_json(encoded) if encoded else None

    def required_document(self, creator_account_id: str) -> AgentConfigDocumentResponse:
        revision = self._required.get(creator_account_id)
        if revision is None:
            raise LookupError(f"No required Agent configuration for {creator_account_id}")
        document = self.document(creator_account_id, revision)
        if document is None:
            raise RuntimeError("Required configuration points at a missing immutable document")
        return document

    def next_revision(self, creator_account_id: str) -> str:
        return f"config-{self._last_sequence.get(creator_account_id, 0) + 1}"

    def set_required(self, creator_account_id: str, revision: str) -> None:
        if self.document(creator_account_id, revision) is None:
            raise LookupError(f"Unknown configuration revision {revision}")
        self._required[creator_account_id] = revision

    def installation(
        self, creator_account_id: str, agent_installation_id: UUID
    ) -> ConfigInstallationRecord | None:
        record = self._installations.get((creator_account_id, agent_installation_id))
        return replace(record) if record is not None else None

    def save_installation(self, record: ConfigInstallationRecord) -> None:
        self._installations[(record.creator_account_id, record.agent_installation_id)] = replace(
            record
        )

    def require_for_account(self, creator_account_id: str, revision: str) -> None:
        self.set_required(creator_account_id, revision)
        for key, record in list(self._installations.items()):
            if record.creator_account_id == creator_account_id:
                self._installations[key] = replace(
                    record, required_config_revision=revision
                )

    def reset(self) -> None:
        self._documents.clear()
        self._required.clear()
        self._last_sequence.clear()
        self._installations.clear()


class AgentConfigurationAuthority:
    """Own immutable documents and required-versus-applied installation state."""

    def __init__(self, repository: AgentConfigRepository) -> None:
        self.repository = repository
        self._publish_lock = asyncio.Lock()
        self.bootstrap()

    def bootstrap(self) -> None:
        document = build_config_document(
            creator_account_id=BOOTSTRAP_ACCOUNT_ID,
            config_revision=BOOTSTRAP_CONFIG_REVISION,
            issued_at=BOOTSTRAP_ISSUED_AT,
            capture_policy=BOOTSTRAP_CAPTURE_POLICY,
            command_policy=BOOTSTRAP_COMMAND_POLICY,
        )
        existing = self.repository.document(
            BOOTSTRAP_ACCOUNT_ID, BOOTSTRAP_CONFIG_REVISION
        )
        if existing is None:
            publish_document = getattr(self.repository, "publish_document", None)
            if callable(publish_document):
                publish_document(document)
            else:
                self.repository.add_document(document)
                self.repository.set_required(
                    BOOTSTRAP_ACCOUNT_ID, BOOTSTRAP_CONFIG_REVISION
                )
            return
        if existing.digest != document.digest:
            raise RuntimeError("Persisted bootstrap configuration content has changed")
        try:
            self.repository.required_document(BOOTSTRAP_ACCOUNT_ID)
        except LookupError:
            self.repository.set_required(
                BOOTSTRAP_ACCOUNT_ID, BOOTSTRAP_CONFIG_REVISION
            )

    def reset(self) -> None:
        self.repository.reset()
        self.bootstrap()

    def required_document(self, creator_account_id: str) -> AgentConfigDocumentResponse:
        return self.repository.required_document(creator_account_id)

    async def publish(
        self,
        creator_account_id: str,
        *,
        capture_policy: dict[str, Any],
        command_policy: dict[str, Any],
        issued_at: datetime | None = None,
    ) -> AgentConfigDocumentResponse:
        async with self._publish_lock:
            document = build_config_document(
                creator_account_id=creator_account_id,
                config_revision=self.repository.next_revision(creator_account_id),
                issued_at=issued_at or datetime.now(timezone.utc),
                capture_policy=capture_policy,
                command_policy=command_policy,
            )
            publish_document = getattr(self.repository, "publish_document", None)
            if callable(publish_document):
                publish_document(document)
            else:
                self.repository.add_document(document)
                self.repository.require_for_account(
                    creator_account_id, document.config_revision
                )
            return document

    def bind_installation(
        self,
        creator_account_id: str,
        agent_installation_id: UUID,
        applied_config_revision: str | None,
    ) -> ConfigInstallationRecord:
        required = self.required_document(creator_account_id).config_revision
        record = self.repository.installation(
            creator_account_id, agent_installation_id
        )
        if record is None:
            record = ConfigInstallationRecord(
                creator_account_id=creator_account_id,
                agent_installation_id=agent_installation_id,
                required_config_revision=required,
            )
        record = replace(record, required_config_revision=required)
        record = self._record_echo(record, applied_config_revision)
        self.repository.save_installation(record)
        return record

    def record_echo(
        self,
        creator_account_id: str,
        agent_installation_id: UUID,
        applied_config_revision: str | None,
    ) -> ConfigInstallationRecord:
        return self.bind_installation(
            creator_account_id, agent_installation_id, applied_config_revision
        )

    def _record_echo(
        self, record: ConfigInstallationRecord, applied_config_revision: str | None
    ) -> ConfigInstallationRecord:
        if applied_config_revision is None:
            return replace(record, applied_config_revision=None)
        known = self.repository.document(
            record.creator_account_id, applied_config_revision
        )
        if known is None:
            return replace(
                record,
                last_failure=(
                    f"Agent reported unknown configuration {applied_config_revision}"
                ),
            )
        return replace(
            record,
            applied_config_revision=applied_config_revision,
            last_failure=None,
        )

    def record_report(
        self,
        creator_account_id: str,
        agent_installation_id: UUID,
        *,
        config_revision: str,
        digest: str,
        outcome: str,
        capability_details: Iterable[str | None],
    ) -> ConfigInstallationRecord:
        record = self.repository.installation(
            creator_account_id, agent_installation_id
        )
        if record is None:
            record = self.bind_installation(
                creator_account_id, agent_installation_id, None
            )
        document = self.repository.document(creator_account_id, config_revision)
        if document is None or document.digest != digest:
            record = replace(
                record, last_failure="Agent reported an unknown revision or digest"
            )
        elif outcome == "applied":
            record = replace(
                record,
                applied_config_revision=config_revision,
                last_failure=None,
            )
        else:
            details = [detail for detail in capability_details if detail]
            record = replace(
                record,
                last_failure=(
                    "; ".join(details)
                    if details
                    else f"Configuration activation outcome was {outcome}"
                ),
            )
        self.repository.save_installation(record)
        return record

    def installation(
        self, creator_account_id: str, agent_installation_id: UUID
    ) -> ConfigInstallationRecord:
        record = self.repository.installation(
            creator_account_id, agent_installation_id
        )
        if record is None:
            return self.bind_installation(
                creator_account_id, agent_installation_id, None
            )
        return record

