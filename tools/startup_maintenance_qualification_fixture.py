"""Synthetic retained work for opt-in frozen startup recovery qualification.

This tool is never packaged. It seeds the supported canonical v11 lineage,
not an invented migration, and never constructs a migrating auth store.
Only the native test's private temporary roots are admitted.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
from types import SimpleNamespace

from app.core.first_run import VerifiedGrantBindings, initialize_production_configuration
from app.persistence.database import AuthSQLite, CanonicalSQLite, open_encrypted_sqlite
from app.persistence.migrations import InstallationMigrationLock, MigrationRunner
from app.persistence.signing_parameters import SigningParameterPersistence
from app.security.local_data_key import database_key
from app.security.signing_parameter_publications import (
    canonical, checkpoint, sha256, verify_catalog, verify_keyset,
)

REPOSITORY = Path(__file__).resolve().parents[1]


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
                result["commands"] = sha256(canonical([list(row) for row in connection.execute("SELECT * FROM commands")]))
        with path.open("rb") as handle:
            assert not handle.read(16).startswith(b"SQLite format 3")
    for filename in ("runtime.env", ".ofca-master-key.dpapi"):
        result[filename] = sha256((root / filename).read_bytes())
    return result


def seed(root: Path, rows: int) -> None:
    assert 100 <= rows <= 500_000
    auth = AuthSQLite(root / "auth.sqlite3", encryption_key=database_key(root / "auth.sqlite3", "auth", create_if_missing=False))
    now = datetime.now(timezone.utc).isoformat()
    with auth.transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
        connection.execute("""INSERT INTO onboarding_progress_outbox
            (milestone,event_id,occurred_at,onboarding_transaction_id,organization_id,installation_id,
             correlation_id,state,attempts,next_attempt_at)
            VALUES ('enrolled','synthetic-event',?,'synthetic-transaction','synthetic-org',
                    'synthetic-installation','synthetic-correlation','pending',2,'2099-01-01T00:00:00Z')""", (now,))
        body = canonical({"synthetic": "pending-activation"}).decode()
        connection.execute("""INSERT INTO capability_license_pending_deliveries
            (operation_type,authority_reference_id,endpoint,idempotency_key,request_body,request_digest,created_at)
            VALUES ('activate','synthetic-reference','https://qualification.example.test/activate',
                    'synthetic-idempotency',?,?,?)""", (body, sha256(body.encode()), now))
    initialize_production_configuration(bindings=VerifiedGrantBindings("synthetic-installation", "creator"),
                                        extension_id="a" * 32, data_directory=root)
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
        connection.execute("""INSERT INTO commands
            (command_id,creator_account_id,action_json,deadline,idempotency_policy,issued_at,state,
             delivery_attempts,result_apply_count)
            VALUES ('synthetic-command','synthetic-account','{}','2099-01-01T00:00:00Z',
                    'synthetic-policy',?,'issued',2,0)""", (now,))


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
    root = Path(supplied).resolve(strict=True)
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
