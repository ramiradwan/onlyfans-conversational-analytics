"""The independent verifier reads existing encrypted stores and fails closed."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from app.persistence.database import LocalSQLite
from app.analytics.sqlite_projection_store import ProjectionValidationError
from app.security.local_data_key import MASTER_KEY_FILENAME
from tests.continuous_analytics_fixture import ACCOUNT, advance, cleanup, make_fixture
from tools import analytics_qualification_package_oracle as oracle


pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]


@pytest.fixture
def packaged(tmp_path):
    directory = tmp_path / "data"
    fixture = make_fixture(directory, conversations=2, messages=2)
    fixture.clock.now = datetime.now(timezone.utc)
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET sent_at=?", (
            (fixture.clock.now - timedelta(days=2)).isoformat(),))
    reference = fixture.pipeline.publish_candidate(fixture.pipeline.build_candidate(ACCOUNT)).reference
    (directory / MASTER_KEY_FILENAME).write_bytes(b"synthetic-test-key-marker")
    (directory / "runtime.env").write_text(
        f'CANONICAL_DATABASE_PATH="{(directory / "canonical.sqlite3").as_posix()}"\n'
        f'ANALYTICS_PROJECTION_DATABASE_PATH="{(directory / "analytics.sqlite3").as_posix()}"\n',
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    inputs = {"data_directory": str(directory), "runtime_directory": str(runtime),
              "synthetic_account_id": ACCOUNT}
    yield fixture, reference, inputs, tmp_path / "verification.json"
    cleanup(fixture)


def test_oracle_replays_canonical_and_revalidates_persisted_content(packaged, monkeypatch):
    fixture, reference, inputs, output = packaged
    canonical = fixture.repositories.database.path
    before = canonical.read_bytes()
    monkeypatch.setattr(LocalSQLite, "connect", lambda self: pytest.fail("writable open"))
    result = oracle.verify_package(inputs, reference.generation_id, reference.source_revision, output)
    assert result["expected"] == result["actual"]
    assert set(result["actual"]) == set(oracle._DIGESTS)
    assert all(result[field] is True for field in (
        "canonical_unchanged", "independent_rebuild_equal", "persisted_content_revalidated",
        "no_unpermitted_orphans"))
    assert result["stale_reference_rejected"] is None
    assert result["persisted_verifier_binding"]["generation_id"] == reference.generation_id
    assert result["persisted_verifier_binding"]["canonical_revision"] == reference.source_revision
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert canonical.read_bytes() == before
    assert "synthetic-test-key-marker" not in output.read_text(encoding="utf-8")


def test_oracle_checks_retired_reference_with_current_publication(packaged):
    fixture, previous, inputs, output = packaged
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='Synthetic changed text' "
                   "WHERE creator_account_id=? AND message_id='m-0-0'", (ACCOUNT,))
        advance(db)
    current = fixture.pipeline.publish_candidate(fixture.pipeline.build_candidate(ACCOUNT)).reference
    result = oracle.verify_package(inputs, current.generation_id, current.source_revision,
                                   output, previous.generation_id)
    assert result["stale_reference_rejected"] is True


@pytest.mark.parametrize("failure", ["generation", "revision", "account", "same_previous"])
def test_oracle_refuses_mismatched_bindings(packaged, failure):
    _fixture, reference, inputs, output = packaged
    generation, revision, previous = reference.generation_id, reference.source_revision, None
    if failure == "generation":
        generation = "absent-generation"
    elif failure == "revision":
        revision += 1
    elif failure == "account":
        inputs["synthetic_account_id"] = "synthetic-other-owner"
    else:
        previous = generation
    with pytest.raises(ValueError):
        oracle.verify_package(inputs, generation, revision, output, previous)
    assert not output.exists()


def test_oracle_requires_existing_key_before_opening_data(packaged, monkeypatch):
    _fixture, reference, inputs, output = packaged
    key = oracle.Path(inputs["data_directory"]) / MASTER_KEY_FILENAME
    key.unlink()
    monkeypatch.setattr(LocalSQLite, "__init__", lambda *a, **k: pytest.fail("database opened"))
    with pytest.raises(ValueError, match="oracle_existing_key_required"):
        oracle.verify_package(inputs, reference.generation_id, reference.source_revision, output)
    assert not key.exists()


def test_oracle_refuses_stale_canonical_content_at_same_revision(packaged):
    fixture, reference, inputs, output = packaged
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='Synthetic changed text' "
                   "WHERE creator_account_id=? AND message_id='m-0-0'", (ACCOUNT,))
    with pytest.raises(ValueError, match="oracle_independent_rebuild_mismatch"):
        oracle.verify_package(inputs, reference.generation_id, reference.source_revision, output)
    assert not output.exists()


def test_oracle_detects_canonical_commit_during_replay(packaged, monkeypatch):
    fixture, reference, inputs, output = packaged
    rebuild = oracle._cold_projection

    def changing(source, generation, now):
        result = rebuild(source, generation, now)
        with fixture.repositories.database.transaction() as db:
            advance(db)
        return result

    monkeypatch.setattr(oracle, "_cold_projection", changing)
    with pytest.raises(ValueError, match="oracle_source_changed_during_verification"):
        oracle.verify_package(inputs, reference.generation_id, reference.source_revision, output)
    assert not output.exists()


def test_oracle_does_not_trust_stored_projection_digest(packaged):
    fixture, reference, inputs, output = packaged
    store = getattr(fixture.stores.projections, "_store", fixture.stores.projections)
    with store.database.transaction() as db:
        triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                              "AND tbl_name='analytics_projections' AND sql LIKE '%BEFORE UPDATE%'").fetchall()
        for row in triggers:
            db.execute('DROP TRIGGER "' + row[0].replace('"', '""') + '"')
        db.execute("UPDATE analytics_projections SET document_json=json_set(document_json,"
                   "'$.creator_metrics.message_count',999) WHERE generation_id=?",
                   (reference.generation_id,))
        for row in triggers:
            db.execute(row[1])
    with pytest.raises(ProjectionValidationError, match="conversation enrichment coverage differs"):
        oracle.verify_package(inputs, reference.generation_id, reference.source_revision, output)
    assert not output.exists()


def test_oracle_output_cannot_mutate_runtime_data(packaged):
    _fixture, reference, inputs, _output = packaged
    with pytest.raises(ValueError, match="oracle_output_inside_data_directory"):
        oracle.verify_package(inputs, reference.generation_id, reference.source_revision,
                              oracle.Path(inputs["data_directory"]) / "verification.json")


def _expected_messages(fixture):
    at = (fixture.clock.now - timedelta(days=2)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return [[f"m-{chat}-{message}", f"chat-{chat}", "synthetic-fan", "Thanks pricing", at,
             "inbound" if message % 2 == 0 else "outbound"]
            for chat in range(2) for message in range(2)]


def test_oracle_compares_offered_fixture_content_without_writing_it(packaged):
    fixture, reference, inputs, output = packaged
    expected = _expected_messages(fixture)
    result = oracle.verify_package(inputs, reference.generation_id, reference.source_revision,
                                   output, expected_messages=expected)
    assert result["fixture_content_equal"] is True
    assert result["expected_message_count"] == 4
    assert result["expected_messages_sha256"] == result["actual_messages_sha256"]
    assert len(result["expected_messages_sha256"]) == 64
    assert "Thanks pricing" not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("field", range(6))
def test_oracle_refuses_equal_count_fixture_field_loss(packaged, field):
    fixture, reference, inputs, output = packaged
    expected = _expected_messages(fixture)
    expected[0][field] = "2000-01-01T00:00:00.000Z" if field == 4 else "altered"
    expected.sort(key=lambda row: row[0])
    with pytest.raises(ValueError, match="oracle_fixture_content_mismatch"):
        oracle.verify_package(inputs, reference.generation_id, reference.source_revision,
                              output, expected_messages=expected)
    assert not output.exists()


def test_oracle_refuses_missing_expected_message(packaged):
    fixture, reference, inputs, output = packaged
    with pytest.raises(ValueError, match="oracle_fixture_content_mismatch"):
        oracle.verify_package(inputs, reference.generation_id, reference.source_revision,
                              output, expected_messages=_expected_messages(fixture)[1:])
    assert not output.exists()
