from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import pytest

from app.analytics.identity import CanonicalIdentity, canonical_identity
from app.analytics.opaque_refs import account_ref
from app.analytics.ownership import capability_digest, current_build_owner
from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import MigrationRunner
from app.persistence.projection_activation import (
    InMemoryProjectionActivationRepository,
    ProjectionActivationConflict,
    ProjectionActivationRepository,
    SQLiteProjectionActivationRepository,
)
from app.transport.ingestion import AccountReadModel


TEST_OWNER = current_build_owner("activation-test-owner")
TEST_PUBLICATION_DIGEST = capability_digest("activation-test-publication-secret")


@dataclass
class ActivationCase:
    ledger: ProjectionActivationRepository
    identity: CanonicalIdentity
    advance: Callable[[], CanonicalIdentity]
    reopen: Callable[[], ProjectionActivationRepository]


@pytest.fixture(params=["memory", "sqlite"])
def activation_case(request, tmp_path: Path) -> ActivationCase:
    initial = canonical_identity(AccountReadModel(view_revision=0))
    advanced = canonical_identity(AccountReadModel(view_revision=1))
    if request.param == "memory":
        state = {"identity": initial}
        ledger = InMemoryProjectionActivationRepository(
            lambda account_id: state["identity"] if account_id == "account-a" else None
        )
        for suffix in ("a", "b", "c"):
            ledger.register_publication_epoch(
                f"epoch-{suffix}", "activation-test-scheduler", TEST_PUBLICATION_DIGEST
            )

        def advance() -> CanonicalIdentity:
            state["identity"] = advanced
            return advanced

        return ActivationCase(ledger, initial, advance, lambda: ledger)

    path = tmp_path / "canonical.sqlite3"
    database = CanonicalSQLite(path)
    MigrationRunner(database).run()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO account_read_models VALUES ('account-a', 0)"
        )

    def advance() -> CanonicalIdentity:
        with database.transaction() as connection:
            connection.execute(
                "UPDATE account_read_models SET view_revision=1 WHERE creator_account_id='account-a'"
            )
        return advanced

    def reopen() -> ProjectionActivationRepository:
        reopened = CanonicalSQLite(path)
        MigrationRunner(reopened).run()
        return SQLiteProjectionActivationRepository(reopened)

    ledger = SQLiteProjectionActivationRepository(database)
    for suffix in ("a", "b", "c"):
        ledger.register_publication_epoch(
            f"epoch-{suffix}", "activation-test-scheduler", TEST_PUBLICATION_DIGEST
        )
    return ActivationCase(ledger, initial, advance, reopen)


def identity_fields(suffix: str = "a") -> dict[str, str]:
    return {
        "projection_digest": "sha256:" + suffix * 64,
        "graph_digest": "sha256:" + chr(ord(suffix) + 1) * 64,
        "pipeline_revision": "pipeline.v1",
        "pipeline_config_digest": "sha256:" + chr(ord(suffix) + 2) * 64,
        "pipeline_identity_digest": "sha256:" + chr(ord(suffix) + 3) * 64,
    }


def reservation_fields(suffix: str = "a") -> dict[str, object]:
    return {
        **identity_fields(suffix),
        "account_ref": account_ref("account-a"),
        "expected_previous_generation_id": None,
        "expected_previous_revision": None,
        "publication_epoch": f"epoch-{suffix}",
        "writer_owner": TEST_OWNER,
        "publication_capability_digest": TEST_PUBLICATION_DIGEST,
    }


def test_activation_intents_bind_full_identity_and_are_idempotent(
    activation_case: ActivationCase,
) -> None:
    ledger = activation_case.ledger
    fields = reservation_fields()
    first = ledger.reserve(
        creator_account_id="account-a",
        generation_id="generation-a",
        canonical_identity=activation_case.identity,
        **fields,
    )
    assert ledger.reserve(
        creator_account_id="account-a",
        generation_id="generation-a",
        canonical_identity=activation_case.identity,
        **fields,
    ) == first
    with pytest.raises(ProjectionActivationConflict, match="identity differs"):
        ledger.reserve(
            creator_account_id="account-a",
            generation_id="generation-a",
            canonical_identity=activation_case.identity,
            **{**fields, "graph_digest": "sha256:" + "f" * 64},
        )
    assert ledger.pending() == [first]

    completed = ledger.complete(first)
    assert completed.state == "completed"
    assert ledger.complete(first) == completed
    assert ledger.pending() == []

    second = ledger.reserve(
        creator_account_id="account-a",
        generation_id="generation-b",
        canonical_identity=activation_case.identity,
        **reservation_fields("b"),
    )
    assert second.witness_sequence == first.witness_sequence + 1
    assert ledger.cancel(second.intent_id).state == "cancelled"


def test_reservation_and_completion_are_canonical_identity_cas(
    activation_case: ActivationCase,
) -> None:
    ledger = activation_case.ledger
    stale = CanonicalIdentity(
        activation_case.identity.revision + 1,
        activation_case.identity.content_digest,
    )
    with pytest.raises(ProjectionActivationConflict, match="canonical identity changed"):
        ledger.reserve(
            creator_account_id="account-a",
            generation_id="stale",
            canonical_identity=stale,
            **reservation_fields("c"),
        )

    reserved = ledger.reserve(
        creator_account_id="account-a",
        generation_id="generation-a",
        canonical_identity=activation_case.identity,
        **reservation_fields(),
    )
    with pytest.raises(ProjectionActivationConflict, match="already reserved"):
        ledger.reserve(
            creator_account_id="account-a",
            generation_id="generation-b",
            canonical_identity=activation_case.identity,
            **reservation_fields("b"),
        )

    activation_case.advance()
    reopened = activation_case.reopen()
    with pytest.raises(ProjectionActivationConflict, match="canonical identity changed"):
        reopened.complete(reserved)
    cancelled = reopened.get("generation-a")
    assert cancelled is not None and cancelled.state == "cancelled"


def test_revoked_publication_epoch_cancels_reserved_completion(
    activation_case: ActivationCase,
) -> None:
    ledger = activation_case.ledger
    reserved = ledger.reserve(
        creator_account_id="account-a",
        generation_id="generation-a",
        canonical_identity=activation_case.identity,
        **reservation_fields(),
    )

    ledger.revoke_publication_epoch(
        reserved.publication_epoch,
        "activation-test-scheduler",
        TEST_PUBLICATION_DIGEST,
    )

    with pytest.raises(ProjectionActivationConflict, match="publication epoch revoked"):
        ledger.complete(reserved)
    cancelled = ledger.get(reserved.generation_id)
    assert cancelled is not None and cancelled.state == "cancelled"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("creator_account_id", "account-b"),
        ("account_ref", account_ref("account-b")),
        ("generation_id", "generation-b"),
        ("canonical_revision", 99),
        ("canonical_content_digest", "sha256:" + "f" * 64),
        ("projection_digest", "sha256:" + "f" * 64),
        ("graph_digest", "sha256:" + "f" * 64),
        ("pipeline_revision", "pipeline.v2"),
        ("pipeline_config_digest", "sha256:" + "f" * 64),
        ("pipeline_identity_digest", "sha256:" + "f" * 64),
        ("expected_previous_generation_id", "previous"),
        ("expected_previous_revision", 7),
        ("publication_epoch", "epoch-other"),
        ("writer_owner_id", "copied-owner"),
        ("writer_owner_pid", 2_000_000_000),
        ("writer_process_started_at", "copied-start"),
        ("writer_instance_nonce", "copied-nonce"),
        ("writer_capability_digest", "sha256:" + "e" * 64),
        ("publication_capability_digest", "sha256:" + "d" * 64),
        ("witness_sequence", 99),
    ],
)
def test_completion_rejects_every_mutated_identity_field(
    activation_case: ActivationCase,
    field: str,
    value: object,
) -> None:
    reserved = activation_case.ledger.reserve(
        creator_account_id="account-a",
        generation_id="generation-a",
        canonical_identity=activation_case.identity,
        **reservation_fields(),
    )
    with pytest.raises(ProjectionActivationConflict, match="completion identity differs"):
        activation_case.ledger.complete(replace(reserved, **{field: value}))
    assert activation_case.ledger.get("generation-a") == reserved
