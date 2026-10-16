"""Synthetic retained work for opt-in frozen startup recovery qualification.

This tool is never packaged. It seeds the supported canonical v11 lineage,
not an invented migration, and never constructs a migrating auth store.
Only private temporary roots or an explicitly claimed synthetic package root
are admitted. No production data root can be selected.
"""
from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timezone
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import tempfile
from types import SimpleNamespace
from uuid import uuid4

from app.core.first_run import VerifiedGrantBindings, initialize_production_configuration
from app.persistence.database import AuthSQLite, CanonicalSQLite, open_encrypted_sqlite
from app.persistence.migrations import InstallationMigrationLock, MigrationRunner
from app.persistence.private_files import (reject_path_aliases, apply_private_directory_security,
                                          apply_private_file_security, private_file_identity, file_sha256)
from app.persistence.signing_parameters import SigningParameterPersistence
from app.protocol.common import MessageSendAction
from app.services.command_execution import CommandDeliveryTarget, CommandService
from app.security.local_data_key import database_key
from app.security.signing_parameter_publications import (
    canonical, checkpoint, sha256, verify_catalog, verify_keyset,
)

REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE_MARKER = ".qualification-upgrade-fixture.json"
FIXTURE_SCHEMA = "ofca-installed-maintenance-fixture/v1"


@contextmanager
def fixture_configuration(root: Path):
    """Load configured repository adapters only after this fixture has a config."""
    assert (root / "runtime.env").is_file()
    name = "LOCAL_ANALYTICS_DATA_DIR"
    previous = os.environ.get(name)
    os.environ[name] = str(root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def governed_root(root: Path) -> None:
    assert os.name == "nt"
    expected = Path(os.environ["LOCALAPPDATA"]) / "OnlyFans Conversational Analytics" / "qualification" / "OFCA.Qualification.Direct"
    assert reject_path_aliases(root) == reject_path_aliases(expected)


def claim(root: Path) -> dict:
    governed_root(root)
    assert not root.exists(), "a qualification fixture cannot adopt existing state"
    root.mkdir(parents=True)
    apply_private_directory_security(root)
    marker = {"schema": FIXTURE_SCHEMA, "nonce": uuid4().hex}
    path = root / FIXTURE_MARKER
    path.write_text(json.dumps(marker), encoding="utf-8")
    apply_private_file_security(path)
    return marker


def admit_governed_fixture(root: Path) -> None:
    governed_root(root)
    path = root / FIXTURE_MARKER
    private_file_identity(path, repair_permissions=False)
    assert path.stat().st_size < 1024
    marker = json.loads(path.read_bytes())
    assert set(marker) == {"schema", "nonce"} and marker["schema"] == FIXTURE_SCHEMA
    assert isinstance(marker["nonce"], str) and re.fullmatch(r"[0-9a-f]{32}", marker["nonce"])


def observe(root: Path) -> dict:
    result = {}
    for filename, scope in (("auth.sqlite3", "auth"), ("canonical.sqlite3", "canonical")):
        path = root / filename
        with closing(open_encrypted_sqlite(path, database_key(path, scope, create_if_missing=False),
                                          read_only=True, immutable=not Path(str(path) + "-wal").exists())) as connection:
            result[scope + "_version"] = connection.execute("PRAGMA user_version").fetchone()[0]
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
            if scope == "auth":
                for table in ("onboarding_progress_outbox", "capability_license_pending_deliveries"):
                    rows = [list(row) for row in connection.execute("SELECT * FROM " + table)]
                    result[table] = sha256(canonical(rows))
                    result[table + "_count"] = len(rows)
                result["publication"] = None
                if result["auth_version"] == 33:
                    row = connection.execute("SELECT state_json,state_sha256,initialized FROM signing_publication_authority").fetchone()
                    if row[0] is not None:
                        assert sha256(row[0].encode()) == row[1] and row[2] == 1
                        result["publication"] = row[1]
            else:
                digest = hashlib.sha256()
                count = 0
                for row in connection.execute("SELECT message_id,text FROM account_messages ORDER BY message_id"):
                    digest.update(canonical(list(row)))
                    count += 1
                result["messages"] = digest.hexdigest()
                result["message_count"] = count
                # Reconstruct through the actual read-only repository. A raw
                # table digest alone does not prove retained work is usable.
                with fixture_configuration(root):
                    from app.persistence.repositories import SQLiteCommandRepository
                    commands = SQLiteCommandRepository(SimpleNamespace(read=lambda: nullcontext(connection))).list()
                for command in commands:
                    MessageSendAction.model_validate(command.action)
                    assert command.idempotency_policy == "deduplicate"
                result["commands"] = sha256(canonical({
                    table: [list(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                    for table in ("commands", "command_transitions", "command_results", "command_result_receipts")
                }))
        with path.open("rb") as handle:
            assert not handle.read(16).startswith(b"SQLite format 3")
    for filename in ("runtime.env", ".ofca-master-key.dpapi"):
        result[filename] = sha256((root / filename).read_bytes())
    from app.persistence import upgrade_lifecycle as upgrade
    result["upgrade"] = None
    if (root / upgrade.JOURNAL_FILENAME).exists():
        metadata = upgrade._validate_metadata(upgrade._read_protected(root / upgrade.JOURNAL_FILENAME), checkpoint=False)
        effective = upgrade._effective(metadata)
        manifest = root / upgrade.CHECKPOINT_DIRECTORY / "checkpoint.dpapi"
        result["upgrade"] = {"schema": metadata["schema"], "phase": metadata["phase"],
            "original_version": metadata.get("release", {}).get("version"),
            "original_kind": metadata.get("release", {}).get("kind"),
            "original_target": metadata["target"], "resume_target": effective["target"],
            "resume_version": effective.get("release", {}).get("version"),
            "original_schemas": metadata["schemas"], "resume_schemas": effective["schemas"],
            "checkpoint_inventory": metadata["files"],
            "checkpoint_manifest_sha256": file_sha256(manifest) if manifest.exists() else None}
    return result


def seed(root: Path, rows: int) -> None:
    assert 100 <= rows <= 500_000
    auth = AuthSQLite(root / "auth.sqlite3", encryption_key=database_key(root / "auth.sqlite3", "auth", create_if_missing=False))
    now = datetime.now(timezone.utc).isoformat()
    with auth.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
    initialize_production_configuration(bindings=VerifiedGrantBindings("synthetic-installation", "creator"),
                                        extension_id="a" * 32, data_directory=root)
    with fixture_configuration(root):
        from app.provisioning.progress_reporting import _new_uuid7
    with auth.transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
        connection.execute("""INSERT INTO onboarding_progress_outbox
            (milestone,event_id,occurred_at,onboarding_transaction_id,organization_id,installation_id,
             correlation_id,state,attempts,next_attempt_at)
            VALUES ('enrolled',?,?,'synthetic-transaction','synthetic-org',
                    'synthetic-installation',?,'pending',2,'2099-01-01T00:00:00Z')""",
            (_new_uuid7(datetime.fromisoformat(now)), now, _new_uuid7(datetime.fromisoformat(now))))
        body = canonical({"synthetic": "pending-activation"}).decode()
        connection.execute("""INSERT INTO capability_license_pending_deliveries
            (operation_type,authority_reference_id,endpoint,idempotency_key,request_body,request_digest,created_at)
            VALUES ('activate','synthetic-reference','https://qualification.example.test/activate',
                    'synthetic-idempotency',?,?,?)""", (body, sha256(body.encode()), now))
    assert not (root / "canonical.sqlite3").exists()
    database = CanonicalSQLite(root / "canonical.sqlite3")
    with tempfile.TemporaryDirectory(prefix="ofca-canonical-v11-") as temporary:
        catalog = Path(temporary)
        for path in sorted((REPOSITORY / "app/persistence/sql").glob("[0-9]*.sql"))[:11]:
            shutil.copyfile(path, catalog / path.name)
        MigrationRunner(database, migrations_dir=catalog).run()
    with database.transaction() as connection:
        connection.execute("INSERT INTO account_heads(creator_account_id,updated_at) VALUES ('synthetic-account',?)", (now,))
        connection.execute("""INSERT INTO account_chats
            (creator_account_id,chat_id,record_kind,platform_user_id,display_name,upstream_updated_at,
             content_hash,winning_stream_epoch,winning_source_seq,winning_event_id,is_deleted,updated_at,
             lifecycle_origin,lifecycle_started_at)
            VALUES ('synthetic-account','synthetic-chat','full','synthetic-user','Synthetic',?,
                    'synthetic-chat-hash',1,1,'synthetic-chat-event',0,?,'ordinary',?)""", (now, now, now))
        connection.executemany("""INSERT INTO account_messages
            (creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,direction,
             content_hash,winning_stream_epoch,winning_source_seq,winning_event_id,is_deleted,updated_at,
             lifecycle_origin,lifecycle_started_at)
            VALUES ('synthetic-account',?,'synthetic-chat','synthetic-user',?,?,'inbound',
                    'synthetic-message-hash',1,1,'synthetic-message-event',0,?,'ordinary',?)""",
            ((f"synthetic-message-{index:08}", "Synthetic protected text " + "q" * 4096, now, now, now)
             for index in range(rows)))
    asyncio.run(seed_pending_command(database, now=datetime.fromisoformat(now)))


async def seed_pending_command(database: CanonicalSQLite, *, now: datetime) -> None:
    """Use the real command boundary with a local delivery sink, never a socket."""
    with fixture_configuration(database.path.parent):
        from app.persistence.repositories import SQLiteCommandRepository
    sent = []

    async def capture(payload):
        sent.append(payload)

    service = CommandService(SQLiteCommandRepository(database))
    record = await service.issue(
        creator_account_id="synthetic-account",
        action={"type": "message.send", "conversation_id": "synthetic-chat",
                "text": "Synthetic pending command", "media_url": None},
        deadline=datetime(2099, 1, 1, tzinfo=timezone.utc),
        target=CommandDeliveryTarget(uuid4(), "synthetic-fence", "synthetic-account"),
        sender=capture, now=now,
    )
    assert record.state == "issued" and record.delivery_attempts == 1
    assert len(sent) == 1 and sent[0]["command_id"] == str(record.command_id)
    assert service.expire(now) == []


def publish(root: Path) -> None:
    # A32 has no publication authority. Seed a genuine signed synthetic
    # checkpoint only AFTER auth33 commits, while canonical still needs v12.
    # It must survive resume even though the encrypted checkpoint predates it.
    assert observe(root)["auth_version"] == 33
    assert observe(root)["canonical_version"] == 11
    with tarfile.open(fileobj=io.BytesIO((REPOSITORY / "extension/vendor/local-authenticated-read-connector-0.6.0-rc.4.tgz").read_bytes()), mode="r:gz") as archive:
        vectors = json.load(archive.extractfile("package/publication/vectors.json"))
    fixture = vectors["fixtures"][0]
    keys = verify_keyset(fixture["keyset"], vectors["trust"], now=fixture["now"])
    catalog = verify_catalog(fixture["catalog"], keys, now=fixture["now"])
    state = {"schema": "signing-parameter-brain-state/v1", "trust_sha256": sha256(canonical(vectors["trust"])),
             "clock_high_water": fixture["now"], "keyset": checkpoint(keys),
             "catalog": {**checkpoint(catalog), "keyset_jws": keys.jws}}
    database = AuthSQLite(root / "auth.sqlite3", encryption_key=database_key(root / "auth.sqlite3", "auth", create_if_missing=False))
    persistence = SigningParameterPersistence(SimpleNamespace(database=database), root / "signing-parameter-cache")
    persistence.save(state)
    assert persistence.load() == state


def main() -> None:
    operation, supplied, *arguments = sys.argv[1:]
    if operation == "claim":
        assert not arguments
        print(json.dumps(claim(Path(supplied)), sort_keys=True))
        return
    root = Path(supplied).resolve(strict=True)
    if "--governed-qualification-root" in arguments:
        arguments.remove("--governed-qualification-root")
        admit_governed_fixture(root)
    else:
        assert root.parent == Path(tempfile.gettempdir()).resolve() and root.name.startswith("bridge-process-test-")
    with InstallationMigrationLock(root / ".ofca-runtime-writer.lock"):
        if operation == "seed":
            seed(root, int(arguments[0]))
        elif operation == "publish":
            publish(root)
        else:
            assert operation == "observe"
        print(json.dumps(observe(root), sort_keys=True))


if __name__ == "__main__":
    main()
