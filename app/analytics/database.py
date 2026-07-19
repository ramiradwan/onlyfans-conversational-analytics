"""Initialization and metadata access for the disposable projections file."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from app.persistence.database import ProjectionsSQLite
from app.persistence.migrations import MigrationRunner
from app.analytics.opaque_refs import normalize_account_ref


GenerationStatus = Literal[
    "building", "validated", "activation_pending", "active", "retired"
]


@dataclass(frozen=True, slots=True)
class ProjectionGeneration:
    generation_id: str
    creator_account_id: str
    status: GenerationStatus
    schema_version: int
    build_version: str
    canonical_revision: int
    canonical_content_digest: str
    canonical_high_water_json: str
    pipeline_revision: str
    pipeline_config_digest: str
    pipeline_identity_digest: str
    projection_digest: str | None
    graph_digest: str | None
    node_count: int
    edge_count: int
    activation_intent_id: str | None
    witness_sequence: int | None
    expected_active_generation_id: str | None
    expected_active_revision: int | None
    publication_epoch: str | None
    owner_id: str
    owner_pid: int | None
    owner_process_started_at: str | None
    owner_instance_nonce: str | None
    owner_capability_digest: str
    lease_expires_at: datetime
    started_at: datetime
    validated_at: datetime | None
    activated_at: datetime | None
    retired_at: datetime | None

    @property
    def view_revision(self) -> int | None:
        return self.witness_sequence

    @property
    def account_ref(self) -> str:
        return self.creator_account_id


class ProjectionsDatabase(ProjectionsSQLite):
    """The production projections SQLite file and its migration catalog."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        migrations_dir: str | Path | None = None,
    ) -> None:
        super().__init__(path, busy_timeout_ms=busy_timeout_ms)
        self.migrations_dir = Path(
            migrations_dir or Path(__file__).with_name("sql")
        )
        self.migration_runner = MigrationRunner(
            self,
            migrations_dir=self.migrations_dir,
        )
        self.migration_runner.run()

    def active_generation(self, creator_account_id: str) -> ProjectionGeneration | None:
        partition_ref = normalize_account_ref(creator_account_id)
        with self.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM projection_generations
                WHERE creator_account_id = ? AND status = 'active'
                """,
                (partition_ref,),
            ).fetchone()
            return None if row is None else generation_from_row(row)

    def generation(self, generation_id: str) -> ProjectionGeneration | None:
        with self.read() as connection:
            row = connection.execute(
                "SELECT * FROM projection_generations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            return None if row is None else generation_from_row(row)

    def generations(self, creator_account_id: str) -> list[ProjectionGeneration]:
        partition_ref = normalize_account_ref(creator_account_id)
        with self.read() as connection:
            return [
                generation_from_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM projection_generations
                    WHERE creator_account_id = ?
                    ORDER BY started_at, generation_id
                    """,
                    (partition_ref,),
                )
            ]

    def store_identity(self) -> tuple[int, str, str, str | None]:
        """Return cheap file/schema/store/active-witness identity metadata."""

        with self.read() as connection:
            row = connection.execute(
                "SELECT store_id, schema_identity FROM projection_store_identity WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise RuntimeError("projection_store_identity_missing")
            active = connection.execute(
                """
                SELECT generation_id, witness_sequence FROM projection_generations
                WHERE status='active' ORDER BY creator_account_id
                """
            ).fetchall()
            witness = "|".join(
                f"{item['generation_id']}:{item['witness_sequence']}" for item in active
            ) or None
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            return version, row["schema_identity"], row["store_id"], witness


def generation_from_row(row) -> ProjectionGeneration:
    return ProjectionGeneration(
        generation_id=row["generation_id"],
        creator_account_id=row["creator_account_id"],
        status=row["status"],
        schema_version=int(row["schema_version"]),
        build_version=row["build_version"],
        canonical_revision=int(row["canonical_revision"]),
        canonical_content_digest=row["canonical_content_digest"],
        canonical_high_water_json=row["canonical_high_water_json"],
        pipeline_revision=row["pipeline_revision"],
        pipeline_config_digest=row["pipeline_config_digest"],
        pipeline_identity_digest=row["pipeline_identity_digest"],
        projection_digest=row["projection_digest"],
        graph_digest=row["graph_digest"],
        node_count=int(row["node_count"]),
        edge_count=int(row["edge_count"]),
        activation_intent_id=row["activation_intent_id"],
        witness_sequence=(
            int(row["witness_sequence"])
            if row["witness_sequence"] is not None
            else None
        ),
        expected_active_generation_id=row["expected_active_generation_id"],
        expected_active_revision=(
            int(row["expected_active_revision"])
            if row["expected_active_revision"] is not None
            else None
        ),
        publication_epoch=row["publication_epoch"],
        owner_id=row["owner_id"],
        owner_pid=(None if row["owner_pid"] is None else int(row["owner_pid"])),
        owner_process_started_at=row["owner_process_started_at"],
        owner_instance_nonce=row["owner_instance_nonce"],
        owner_capability_digest=row["owner_capability_digest"],
        lease_expires_at=datetime.fromisoformat(row["lease_expires_at"]),
        started_at=datetime.fromisoformat(row["started_at"]),
        validated_at=(
            datetime.fromisoformat(row["validated_at"])
            if row["validated_at"] is not None
            else None
        ),
        activated_at=(
            datetime.fromisoformat(row["activated_at"])
            if row["activated_at"] is not None
            else None
        ),
        retired_at=(
            datetime.fromisoformat(row["retired_at"])
            if row["retired_at"] is not None
            else None
        ),
    )
