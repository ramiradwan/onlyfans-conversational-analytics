from pathlib import Path
import json

import pytest

from tools import targeted_visibility_benchmark as target

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.serial]


def _databases(directory: Path, suffix: bytes = b"") -> None:
    directory.mkdir()
    for index, name in enumerate(target.DATABASES):
        (directory / name).write_bytes(f"db-{index}".encode() + suffix)


def test_copy_databases_is_byte_bound(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _databases(source)

    copied = target.copy_databases(source, destination)

    assert copied == target.database_hashes(source)
    assert copied == target.database_hashes(destination)
    with pytest.raises(FileExistsError):
        target.copy_databases(source, destination)


def test_seed_metadata_rejects_database_tamper(tmp_path):
    seed = tmp_path / "seed"
    _databases(seed)
    metadata = {
        "schema": target.SEED_SCHEMA,
        "complete": True,
        "database_sha256": target.database_hashes(seed),
    }
    (seed / "ready-seed.json").write_text(json.dumps(metadata), encoding="utf-8")

    class Qualification:
        @staticmethod
        def read_json(path):
            return json.loads(path.read_text(encoding="utf-8"))

    assert target.seed_metadata(Qualification, seed) == metadata
    (seed / target.DATABASES[0]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="ready_seed_digest_mismatch"):
        target.seed_metadata(Qualification, seed)


def test_atomic_status_replaces_non_evidence_sidecar(tmp_path, capsys):
    status = tmp_path / "status.json"

    target.atomic_status(status, "idle", state="started", seconds=61)
    first = json.loads(status.read_text(encoding="utf-8"))
    target.atomic_status(status, "probe", state="complete", seconds=9.75)
    second = json.loads(status.read_text(encoding="utf-8"))

    assert first["stage"] == "idle"
    assert second["stage"] == "probe"
    assert second["state"] == "complete"
    assert second["seconds"] == 9.75
    output = capsys.readouterr().out
    assert "[idle]" in output
    assert "[probe]" in output


def test_verified_seed_open_skips_only_redundant_full_file_startup_checks():
    from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
    from app.persistence.migrations import MigrationRunner

    reconcile = SQLiteAnalyticsProjectionStore.reconcile_startup
    validate = MigrationRunner._validate_database
    with target.verified_seed_storage_start():
        assert SQLiteAnalyticsProjectionStore.reconcile_startup(object()) == {"verified_seed": 1}
        assert MigrationRunner._validate_database(None) is None

    assert SQLiteAnalyticsProjectionStore.reconcile_startup is reconcile
    assert MigrationRunner._validate_database is validate


def test_inherited_seed_verification_requires_same_verified_generation():
    verification = {
        "independent_rebuild_equal": True,
        "persisted_content_revalidated": True,
    }
    base = {
        "verification": verification,
        "manifest_sha256": "manifest",
        "runtime_sha256": "runtime",
        "source_revision": "source-a",
        "reference": {"generation_id": "generation-a"},
    }
    current = {"generation_id": "generation-a"}

    assert target.inherited_seed_verification(
        base, current, "manifest", "runtime", "source-a"
    ) is verification
    assert target.inherited_seed_verification(
        base, {"generation_id": "generation-b"}, "manifest", "runtime", "source-a"
    ) is None
    assert target.inherited_seed_verification(
        base, current, "changed", "runtime", "source-a"
    ) is None
    assert target.inherited_seed_verification(
        base, current, "manifest", "runtime", "source-b"
    ) is None
    assert target.inherited_seed_verification(
        None, current, "manifest", "runtime", "source-a"
    ) is None


def test_phase_heartbeat_reports_running_and_complete(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    events = []

    def observe(path, stage, **values):
        events.append((stage, values["state"]))

    monkeypatch.setattr(target, "atomic_status", observe)
    with target.PhaseHeartbeat(status, "recovery", interval=0.01):
        __import__("time").sleep(0.025)

    assert events[0] == ("recovery", "started")
    assert ("recovery", "running") in events
    assert events[-1] == ("recovery", "complete")


@pytest.mark.asyncio
async def test_ready_seed_enters_normal_scheduler_maintenance():
    class State:
        availability = "available"

    class Scheduler:
        calls = []

        async def start(self, *, recover):
            self.calls.append(recover)

        def state(self, account, *, canonical_revision):
            assert account == "account"
            assert canonical_revision == 7
            return State()

    scheduler = Scheduler()
    state = await target.activate_normal_maintenance(
        scheduler, "account", 7, "available"
    )

    assert scheduler.calls == [True]
    assert state.availability == "available"


def test_parser_requires_explicit_source_binding():
    parser = target.parser()

    args = parser.parse_args([
        "--source-root", "/repo",
        "--expected-sha", "a" * 40,
        "--owner-lock", "/lock",
        "--status", "/status.json",
        "run",
        "--ready-seed", "/seed",
        "--result", "/result.json",
    ])

    assert args.expected_sha == "a" * 40
    assert args.case == "dominant"
    assert args.probe_timeout == 120
    assert args.profile_output is None
