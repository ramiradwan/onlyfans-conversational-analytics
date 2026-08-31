from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import MigrationRunner
from app.persistence.retention import CreatorVaultRetention

MESSAGES = 5_000
CONVERSATIONS = 100
BODY_BYTES = 256
QUALIFICATION_KEY = bytes.fromhex("0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0")
BOUNDS_SECONDS = {
    "policy_read": 5.0,
    "finite_policy_shortening": 120.0,
    "enforce": 30.0,
    "delete_conversation": 30.0,
    "delete_all": 60.0,
}


def timed(operation):
    started = time.perf_counter()
    value = operation()
    return value, time.perf_counter() - started


def database_bytes(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"))
        if candidate.exists()
    )


def seed(db: CanonicalSQLite, now: datetime) -> None:
    body = "x" * BODY_BYTES
    with db.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO account_heads(creator_account_id,updated_at) VALUES ('scale',?)",
            (now.isoformat(),),
        )
        connection.executemany(
            """INSERT INTO account_chats(
                   creator_account_id,chat_id,record_kind,platform_user_id,display_name,
                   upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,
                   winning_event_id,is_deleted,updated_at,lifecycle_origin,lifecycle_started_at)
               VALUES ('scale',?,'full',?,?,?, ?,1,1,?,0,?,'ordinary',?)""",
            [
                (
                    f"chat-{index}", f"participant-{index}", f"Participant {index}",
                    now.isoformat(), f"chat-hash-{index}", f"chat-event-{index}",
                    now.isoformat(), now.isoformat(),
                )
                for index in range(CONVERSATIONS)
            ],
        )
        rows = []
        for index in range(MESSAGES):
            sent_at = now - timedelta(days=120 if index % 2 == 0 else 10)
            chat = index % CONVERSATIONS
            rows.append((
                f"message-{index}", f"chat-{chat}", f"participant-{chat}", body,
                sent_at.isoformat(), f"message-hash-{index}", index + 2,
                f"message-event-{index}", now.isoformat(), now.isoformat(),
            ))
        connection.executemany(
            """INSERT INTO account_messages(
                   creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,
                   direction,upstream_updated_at,content_hash,winning_stream_epoch,
                   winning_source_seq,winning_event_id,is_deleted,updated_at,
                   lifecycle_origin,lifecycle_started_at)
               VALUES ('scale',?,?,?,?,?,'inbound',NULL,?,1,?,?,0,?,'ordinary',?)""",
            rows,
        )


def active_messages(db: CanonicalSQLite) -> int:
    with db.read() as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM account_messages WHERE creator_account_id='scale' AND is_deleted=0"
        ).fetchone()[0])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-commit", default=os.getenv("PRODUCT_SHA", "local"))
    args = parser.parse_args()
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)

    with tempfile.TemporaryDirectory(prefix="vault-scale-") as directory:
        path = Path(directory) / "canonical.sqlite3"
        db = CanonicalSQLite(path, encryption_key=QUALIFICATION_KEY)
        MigrationRunner(db).run()
        seed(db, now)
        retention = CreatorVaultRetention(db, clock=lambda: now)
        seeded_bytes = database_bytes(path)

        retention.set_policy("scale", "finite", finite_horizon_days=365, creator_action_ref="scale-365")
        _, policy_read = timed(lambda: retention.policy("scale"))
        _, shortening = timed(lambda: retention.set_policy(
            "scale", "finite", finite_horizon_days=30, creator_action_ref="scale-30"
        ))
        after_shortening = active_messages(db)
        _, enforce = timed(lambda: retention.enforce("scale", now=now))
        _, delete_conversation = timed(lambda: retention.delete_conversation("scale", "chat-1"))
        after_conversation_delete = active_messages(db)
        _, delete_all = timed(lambda: retention.delete_all("scale"))
        after_delete_all = active_messages(db)

        with db.read() as connection:
            plans = {
                "membership_scan": [list(row) for row in connection.execute(
                    """EXPLAIN QUERY PLAN SELECT m.message_id,a.source_event_at
                       FROM archive_membership a JOIN account_messages m
                       ON m.creator_account_id=a.creator_account_id AND m.message_id=a.message_id
                       WHERE a.creator_account_id='scale' AND m.is_deleted=0"""
                )],
                "conversation_delete_lookup": [list(row) for row in connection.execute(
                    """EXPLAIN QUERY PLAN SELECT message_id FROM account_messages
                       WHERE creator_account_id='scale' AND chat_id='chat-1'"""
                )],
            }

        timings = {
            "policy_read": policy_read,
            "finite_policy_shortening": shortening,
            "enforce": enforce,
            "delete_conversation": delete_conversation,
            "delete_all": delete_all,
        }
        checks = {
            "shortening_retained_expected_half": after_shortening == MESSAGES // 2,
            "conversation_delete_reduced_count": after_conversation_delete < after_shortening,
            "delete_all_empty": after_delete_all == 0,
            "timings_within_bounds": all(
                timings[name] <= BOUNDS_SECONDS[name] for name in timings
            ),
        }
        payload = {
            "schema_version": 1,
            "source_commit": args.source_commit,
            "runtime": {
                "platform": platform.system(),
                "python": platform.python_version(),
            },
            "qualification_envelope": {
                "messages": MESSAGES,
                "conversations": CONVERSATIONS,
                "database_bytes_after_seed": seeded_bytes,
                "fixture_body_bytes": BODY_BYTES,
            },
            "pass_bounds_seconds": BOUNDS_SECONDS,
            "operations_seconds": timings,
            "observed_counts": {
                "after_shortening": after_shortening,
                "after_conversation_delete": after_conversation_delete,
                "after_delete_all": after_delete_all,
            },
            "query_plans": plans,
            "checks": checks,
            "status": "PASS" if all(checks.values()) else "FAIL",
        }

    text = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    if payload["status"] != "PASS":
        raise SystemExit("Creator Vault scale qualification failed")


if __name__ == "__main__":
    main()
