"""Restartable erasure of source-bound findings without executing analysis."""

from contextlib import contextmanager
from datetime import timedelta, timezone
import hashlib
import json
import time

from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.nlp_findings_store import (
    encoded,
    source_binding,
    inventory_digest,
    check_generation,
    check_deadline,
)
from app.analytics.nlp_reclamation import reclaim_orphans
from app.analytics.opaque_refs import account_ref
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from app.analytics.validation_receipt import content_stamp, generation_binding
from app.nlp import v2

PAGE_SIZE = 64


class FindingsErasure:
    def __init__(self, source, projections, *, clock):
        self.source, self.projections, self.database, self.clock = (
            source,
            projections,
            projections.database,
            clock,
        )

        def utc_clock():
            value = clock()
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("nlp_erasure_clock_invalid")
            return value.astimezone(timezone.utc)

        self.clock = utc_clock

    def _state(self, ref):
        with self.database.read() as db:
            row = db.execute(
                "SELECT * FROM nlp_erasure_state WHERE account_ref=?", (ref,)
            ).fetchone()
            return dict(row) if row else None

    @contextmanager
    def _write(self, state):
        with self.database.transaction() as db:
            row = db.execute(
                "SELECT * FROM nlp_erasure_state WHERE account_ref=?",
                (state["account_ref"],),
            ).fetchone()
            if row is None or dict(row) != state:
                raise ValueError("nlp_erasure_retry")
            yield db

    def step(
        self,
        account_id,
        target_revision,
        *,
        account_wide,
        deadline=None,
        acknowledge_absence=None
    ):
        deadline = deadline if deadline is not None else time.monotonic() + 5.0
        check_deadline(deadline)
        ref = account_ref(account_id)
        state = self._state(ref)
        if state and state["target_revision"] > target_revision:
            raise ValueError("nlp_erasure_target_changed")
        if (
            state
            and account_wide
            and int(account_wide) <= state["covered_account_revision"]
        ):
            account_wide = 0
        with self.database.read() as db:
            if content_stamp(db) is None:
                raise ValueError("nlp_erasure_guards_invalid")
            payload = any(
                db.execute(
                    "SELECT 1 FROM " + table + " WHERE account_ref=? LIMIT 1", (ref,)
                ).fetchone()
                for table in ("nlp_accounts", "nlp_records", "nlp_snapshots")
            )
            enhanced = db.execute(
                "SELECT 1 FROM projection_generations WHERE creator_account_id=? AND pipeline_revision LIKE '%+nlp.snapshot.v1%' LIMIT 1",
                (ref,),
            ).fetchone()
            if (
                not payload
                and not enhanced
                and (state is None or state["phase"] == "complete")
            ):
                empty = True
            else:
                empty = False
        if empty:
            # Serialize absence with pending writers; a WAL reader alone can
            # otherwise acknowledge data whose old transaction has not committed.
            with self.database.transaction() as db:
                if content_stamp(db) is None:
                    raise ValueError("nlp_erasure_guards_invalid")
                if (
                    any(
                        db.execute(
                            "SELECT 1 FROM " + table + " WHERE account_ref=? LIMIT 1",
                            (ref,),
                        ).fetchone()
                        for table in ("nlp_accounts", "nlp_records", "nlp_snapshots")
                    )
                    or db.execute(
                        "SELECT 1 FROM projection_generations WHERE creator_account_id=? AND pipeline_revision LIKE '%+nlp.snapshot.v1%' LIMIT 1",
                        (ref,),
                    ).fetchone()
                ):
                    return None
                store = db.execute(
                    "SELECT store_id FROM projection_store_identity WHERE singleton=1"
                ).fetchone()[0]
                receipt = {
                    "account_ref": ref,
                    "target_revision": target_revision,
                    "store_identity": store,
                    "account_revision": max(
                        int(account_wide),
                        state["covered_account_revision"] if state else 0,
                    ),
                }
                current = db.execute(
                    "SELECT * FROM nlp_erasure_state WHERE account_ref=?", (ref,)
                ).fetchone()
                if (dict(current) if current else None) != state:
                    raise ValueError("nlp_erasure_retry")
                db.execute(
                    "INSERT INTO nlp_erasure_state(account_ref,target_revision,account_wide,phase,receipt,covered_account_revision) "
                    "VALUES(?,?,?,'complete',?,?) ON CONFLICT(account_ref) DO UPDATE SET "
                    "target_revision=excluded.target_revision,account_wide=excluded.account_wide,phase='complete',"
                    "receipt=excluded.receipt,covered_account_revision=excluded.covered_account_revision",
                    (
                        ref,
                        target_revision,
                        int(account_wide),
                        encoded(receipt).decode(),
                        receipt["account_revision"],
                    ),
                )
                return receipt
        if state is None or state["target_revision"] < target_revision:
            with self.database.transaction() as db:
                current = db.execute(
                    "SELECT * FROM nlp_erasure_state WHERE account_ref=?", (ref,)
                ).fetchone()
                if (
                    current is not None
                    and current["target_revision"] >= target_revision
                ):
                    return None
                db.execute(
                    "INSERT INTO nlp_erasure_state(account_ref,target_revision,account_wide,phase) VALUES(?,?,?,'index') "
                    "ON CONFLICT(account_ref) DO UPDATE SET target_revision=excluded.target_revision,account_wide=excluded.account_wide,"
                    "phase='index',identity='',cursor='[]',record_cursor='',due_at=NULL,receipt=NULL",
                    (ref, target_revision, int(account_wide)),
                )
                db.execute(
                    "UPDATE nlp_accounts SET admission_epoch=admission_epoch+1,availability_revision=availability_revision+1,"
                    "notification_revision=notification_revision+1 WHERE account_ref=?",
                    (ref,),
                )
            return None
        if state["target_revision"] != target_revision:
            raise ValueError("nlp_erasure_target_changed")
        if state["phase"] == "complete":
            return json.loads(state["receipt"])
        if state["account_wide"] != int(account_wide):
            raise ValueError("nlp_erasure_target_changed")
        if state["phase"] == "index":
            self._index(state, deadline)
            return None
        with self.database.read() as db:
            invalid = (
                state["indexed_schema"]
                != db.execute("PRAGMA schema_version").fetchone()[0]
                or db.execute(
                    "SELECT 1 FROM nlp_records r LEFT JOIN nlp_record_bindings b USING(account_ref,record_digest) "
                    "WHERE r.account_ref=? AND b.record_digest IS NULL LIMIT 1",
                    (ref,),
                ).fetchone()
            )
        if invalid:
            with self._write(state) as db:
                db.execute(
                    "UPDATE nlp_erasure_state SET phase='index',record_cursor='',identity='' WHERE account_ref=?",
                    (ref,),
                )
            return None
        if state["phase"] == "scan":
            self._scan(account_id, state, deadline)
            return None
        return self._erase(account_id, state, deadline)

    def _index(self, state, deadline):
        ref = state["account_ref"]
        with self.database.read() as db:
            stamp = content_stamp(db)
            if stamp is None:
                raise ValueError("nlp_erasure_guards_invalid")
            rows = db.execute(
                "SELECT r.record_digest,r.document FROM nlp_records r WHERE r.account_ref=? "
                "AND r.record_digest>? ORDER BY r.record_digest LIMIT ?",
                (ref, state["record_cursor"], PAGE_SIZE),
            ).fetchall()
            bindings = []
            for row in rows:
                check_deadline(deadline)
                raw = bytes(row["document"])
                if hashlib.sha256(raw).hexdigest() != row["record_digest"]:
                    raise ValueError("nlp_record_invalid")
                source = v2.read_record(raw).source
                if source.account_scope != ref:
                    raise ValueError("nlp_record_invalid")
                value = (
                    ref,
                    row["record_digest"],
                    source.source_ref,
                    source.source_version,
                    source.speaker_ref,
                )
                old = db.execute(
                    "SELECT account_ref,record_digest,source_ref,source_version,speaker_ref FROM nlp_record_bindings "
                    "WHERE account_ref=? AND record_digest=?",
                    value[:2],
                ).fetchone()
                if old is not None and tuple(old) != value:
                    raise ValueError("nlp_record_binding_invalid")
                bindings.append(value)
        check_deadline(deadline)
        with self._write(state) as db:
            if content_stamp(db) != stamp:
                raise ValueError("nlp_erasure_retry")
            db.executemany(
                "INSERT OR IGNORE INTO nlp_record_bindings(account_ref,record_digest,source_ref,source_version,speaker_ref) VALUES(?,?,?,?,?)",
                bindings,
            )
            if rows:
                db.execute(
                    "UPDATE nlp_erasure_state SET record_cursor=? WHERE account_ref=?",
                    (rows[-1]["record_digest"], ref),
                )
            else:
                db.execute(
                    "UPDATE nlp_erasure_state SET phase=?,indexed_schema=? WHERE account_ref=?",
                    (
                        "erase" if state["account_wide"] else "scan",
                        db.execute("PRAGMA schema_version").fetchone()[0],
                        ref,
                    ),
                )

    def _scan(self, account_id, state, deadline):
        identity = self.source.erasure_source_identity(account_id)
        ref = state["account_ref"]
        if identity != state["identity"] or (
            state["due_at"] and state["due_at"] <= self.clock().isoformat()
        ):
            check_deadline(deadline)
            with self._write(state) as db:
                db.execute(
                    "UPDATE nlp_erasure_state SET identity=?,cursor='[]',scan_epoch=scan_epoch+1,due_at=NULL WHERE account_ref=?",
                    (identity, ref),
                )
            return
        cursor = json.loads(state["cursor"]) or None
        page = self.source.nlp_location_page(account_id, cursor, limit=PAGE_SIZE)
        budget = QuestionBudget(
            QuestionLimits(max_records=PAGE_SIZE * 3, wall_clock_ms=5000)
        )
        live = []
        due = state["due_at"]
        for location in page:
            message = self.source.read_evidence_message(account_id, location, budget)
            if message is None:
                continue
            expires = (
                message.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
            ).astimezone(timezone.utc)
            if expires <= self.clock():
                continue
            binding = source_binding(message)
            live.append(
                (binding.source_ref, binding.source_version, binding.speaker_ref)
            )
            due = min(due, expires.isoformat()) if due else expires.isoformat()
        if self.source.erasure_source_identity(account_id) != identity or (
            due and due <= self.clock().isoformat()
        ):
            raise ValueError("nlp_erasure_source_changed")
        check_deadline(deadline)
        with self._write(state) as db:
            for source in live:
                db.execute(
                    "UPDATE nlp_record_bindings SET seen_epoch=? WHERE account_ref=? AND source_ref=? AND source_version=? AND speaker_ref=?",
                    (state["scan_epoch"], ref, *source),
                )
                db.execute(
                    "UPDATE nlp_sources SET erasure_seen_epoch=? WHERE account_ref=? AND source_ref=? AND source_version=? AND speaker_ref=?",
                    (state["scan_epoch"], ref, *source),
                )
            next_cursor = (
                encoded([page[-1].conversation_id, page[-1].message_id]).decode()
                if page
                else state["cursor"]
            )
            db.execute(
                "UPDATE nlp_erasure_state SET cursor=?,due_at=?,phase=? WHERE account_ref=?",
                (next_cursor, due, "scan" if page else "erase", ref),
            )

    def _source_page_step(self, account_id, state, deadline):
        """Check one authenticated historical page under the current erase epoch."""
        from app.analytics.evidence_contracts import EvidenceLocation
        from app.analytics.nlp_findings_store import FrozenFindings, checked_snapshot
        from app.analytics.nlp_read_projection import authenticate_source_page, validate_source_header
        from app.analytics.opaque_refs import message_ref

        ref = state["account_ref"]
        chosen = None
        with self.database.read() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] < 30:
                return False
            stamp = content_stamp(db)
            if stamp is None:
                raise ValueError("nlp_erasure_guards_invalid")
            for snapshot in db.execute(
                "SELECT snapshot_id FROM nlp_snapshots WHERE account_ref=? ORDER BY snapshot_id", (ref,)
            ):
                check_deadline(deadline)
                key = snapshot[0]
                value = checked_snapshot(db, FrozenFindings(ref, key), verify_records=False, deadline=deadline)
                header = value.get("read_inventory")
                counts = db.execute(
                    "SELECT count(*),min(ordinal),max(ordinal),COALESCE(sum(length(document)),0) "
                    "FROM nlp_snapshot_source_pages WHERE account_ref=? AND snapshot_id=?", (ref, key)
                ).fetchone()
                if header is None:
                    if counts[0]:
                        raise ValueError("nlp_read_page_invalid")
                    continue
                validate_source_header(header)
                count = header["source_page_count"]
                if (counts[0] != count or counts[3] != header["source_bytes"]
                        or (count and (counts[1] != 0 or counts[2] != count - 1))):
                    raise ValueError("nlp_read_page_invalid")
                page = db.execute(
                    "SELECT ordinal,document,erasure_seen_epoch,erasure_invalid FROM nlp_snapshot_source_pages "
                    "WHERE account_ref=? AND snapshot_id=? AND erasure_seen_epoch!=? ORDER BY ordinal LIMIT 1",
                    (ref, key, state["scan_epoch"]),
                ).fetchone()
                if page is not None:
                    chosen = (key, dict(page), authenticate_source_page(header, page["ordinal"], bytes(page["document"])))
                    break
        if chosen is None:
            return False
        key, old, rows = chosen
        budget = QuestionBudget(QuestionLimits(max_records=64 * 3, wall_clock_ms=5000))
        invalid = False
        due = state["due_at"]
        for row in rows:
            check_deadline(deadline)
            if type(row) is not list or len(row) != 8:
                raise ValueError("nlp_read_source_invalid")
            chat, message_id, sent_at, direction, version, speaker, record_index, status = row
            location = EvidenceLocation(conversation_id=chat, message_id=message_id)
            message = self.source.read_evidence_message(account_id, location, budget)
            if message is None:
                invalid = True
                continue
            binding = source_binding(message)
            expires = (message.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)).astimezone(timezone.utc)
            if (binding.source_ref != message_ref(account_id, chat, message_id)
                    or binding.source_version != version or binding.speaker_ref != speaker
                    or message.sent_at.isoformat().replace("+00:00", "Z") != sent_at
                    or message.direction != direction or expires <= self.clock()):
                invalid = True
            else:
                due = min(due, expires.isoformat()) if due else expires.isoformat()
        if (self.source.erasure_source_identity(account_id) != state["identity"]
                or (due and due <= self.clock().isoformat())):
            raise ValueError("nlp_erasure_source_changed")
        check_deadline(deadline)
        with self._write(state) as db:
            if content_stamp(db) != stamp:
                raise ValueError("nlp_erasure_retry")
            changed = db.execute(
                "UPDATE nlp_snapshot_source_pages SET erasure_seen_epoch=?,erasure_invalid=? "
                "WHERE account_ref=? AND snapshot_id=? AND ordinal=? AND erasure_seen_epoch=? AND erasure_invalid=?",
                (state["scan_epoch"], int(invalid), ref, key, old["ordinal"], old["erasure_seen_epoch"], old["erasure_invalid"]),
            ).rowcount
            if changed != 1:
                raise ValueError("nlp_erasure_retry")
            db.execute("UPDATE nlp_erasure_state SET due_at=? WHERE account_ref=?", (due, ref))
        return True

    def _erase(self, account_id, state, deadline):
        ref = state["account_ref"]
        if not state["account_wide"]:
            identity = self.source.erasure_source_identity(account_id)
            if identity != state["identity"] or (
                state["due_at"] and state["due_at"] <= self.clock().isoformat()
            ):
                with self._write(state) as db:
                    db.execute(
                        "UPDATE nlp_erasure_state SET phase='scan',identity='' WHERE account_ref=?",
                        (ref,),
                    )
                return None
        # Strict authentication/backfill must succeed before retirement can
        # remove ownership. Optional GC's diagnostic boundary is not used here.
        with self.database.read() as db:
            invalid = db.execute(
                "SELECT 1 FROM projection_generations g LEFT JOIN nlp_generation_refs r USING(generation_id) "
                "WHERE (g.creator_account_id=? OR r.account_ref=?) AND ("
                "(g.pipeline_revision LIKE '%+nlp.snapshot.v1%' AND r.generation_id IS NULL) "
                "OR (r.generation_id IS NOT NULL AND (r.account_ref!=g.creator_account_id OR g.pipeline_revision NOT LIKE '%+nlp.snapshot.v1%'))) LIMIT 1",
                (ref, ref),
            ).fetchone()
            if invalid:
                raise ValueError("nlp_generation_binding_invalid")
        reclaim_orphans(self.database, ref, deadline=deadline)
        if self._state(ref) != state:
            raise ValueError("nlp_erasure_retry")
        if not state["account_wide"] and self._source_page_step(account_id, state, deadline):
            return None
        with self.database.read() as db:
            if content_stamp(db) is None:
                raise ValueError("nlp_erasure_guards_invalid")
            if db.execute(
                "SELECT 1 FROM nlp_snapshots s LEFT JOIN nlp_snapshot_inventory i USING(account_ref,snapshot_id) "
                "WHERE s.account_ref=? AND COALESCE(i.complete,0)=0 LIMIT 1",
                (ref,),
            ).fetchone():
                return None
            missing = db.execute(
                "SELECT 1 FROM projection_generations g LEFT JOIN nlp_generation_refs r USING(generation_id) "
                "WHERE g.creator_account_id=? AND g.pipeline_revision LIKE '%+nlp.snapshot.v1%' AND r.generation_id IS NULL LIMIT 1",
                (ref,),
            ).fetchone()
            if missing:
                raise ValueError("nlp_generation_binding_missing")
            generations = db.execute(
                "SELECT g.* FROM projection_generations g JOIN nlp_generation_refs r USING(generation_id) "
                "WHERE r.account_ref=? AND (? OR EXISTS(SELECT 1 FROM nlp_snapshot_members m JOIN nlp_record_bindings b "
                "USING(account_ref,record_digest) WHERE m.account_ref=r.account_ref AND m.snapshot_id=r.snapshot_id "
                "AND b.seen_epoch!=?) OR EXISTS(SELECT 1 FROM nlp_snapshot_source_pages p "
                "WHERE p.account_ref=r.account_ref AND p.snapshot_id=r.snapshot_id "
                "AND p.erasure_seen_epoch=? AND p.erasure_invalid=1)) ORDER BY g.generation_id LIMIT 1",
                (ref, state["account_wide"], state["scan_epoch"], state["scan_epoch"]),
            ).fetchall()
            generation = generations[0] if generations else None
            if generation is not None:
                check_generation(db, generation, deadline=deadline)
                stamp = content_stamp(db)
        if generation is not None:
            check_deadline(deadline)
            self.projections.retire_findings_for_erasure(state, generation, stamp)
            return None
        with self.database.read() as db:
            sources = [
                row[0]
                for row in db.execute(
                    "SELECT source_ref FROM nlp_sources WHERE account_ref=? "
                    "AND (? OR erasure_seen_epoch!=?) ORDER BY source_ref LIMIT ?",
                    (ref, state["account_wide"], state["scan_epoch"], PAGE_SIZE),
                )
            ]
            jobs = [
                row[0]
                for row in db.execute(
                    "SELECT j.source_ref FROM nlp_jobs j WHERE j.account_ref=? AND ("
                    "? OR EXISTS(SELECT 1 FROM nlp_record_bindings b WHERE b.account_ref=j.account_ref AND b.record_digest=j.record_digest AND b.seen_epoch!=?)) "
                    "ORDER BY source_ref LIMIT ?",
                    (ref, state["account_wide"], state["scan_epoch"], PAGE_SIZE),
                )
            ]
        if sources or jobs:
            check_deadline(deadline)
            with self._write(state) as db:
                for key in set(sources + jobs):
                    db.execute(
                        "DELETE FROM nlp_jobs WHERE account_ref=? AND source_ref=?",
                        (ref, key),
                    )
                for key in sources:
                    db.execute(
                        "DELETE FROM nlp_sources WHERE account_ref=? AND source_ref=?",
                        (ref, key),
                    )
            return None
        with self.database.read() as db:
            if db.execute(
                "SELECT 1 FROM nlp_record_bindings WHERE account_ref=? AND (? OR seen_epoch!=?) LIMIT 1",
                (ref, state["account_wide"], state["scan_epoch"]),
            ).fetchone():
                return None
            if state["account_wide"] and any(
                db.execute(
                    "SELECT 1 FROM " + table + " WHERE account_ref=? LIMIT 1", (ref,)
                ).fetchone()
                for table in ("nlp_snapshots", "nlp_records", "nlp_jobs", "nlp_sources")
            ):
                return None
            inventory = inventory_digest(db, ref, deadline)
            store = db.execute(
                "SELECT store_id FROM projection_store_identity WHERE singleton=1"
            ).fetchone()[0]
        receipt = {
            "account_ref": ref,
            "target_revision": state["target_revision"],
            "store_identity": store,
            "account_revision": max(
                state["covered_account_revision"], state["account_wide"]
            ),
        }
        check_deadline(deadline)
        with self._write(state) as db:
            if state["account_wide"]:
                db.execute(
                    "INSERT INTO nlp_epoch_ledger SELECT account_ref,selection_epoch FROM nlp_accounts WHERE account_ref=? "
                    "ON CONFLICT(account_ref) DO UPDATE SET selection_epoch=max(selection_epoch,excluded.selection_epoch)",
                    (ref,),
                )
                db.execute("DELETE FROM nlp_accounts WHERE account_ref=?", (ref,))
                db.execute(
                    "DELETE FROM nlp_reclamation_cursor WHERE account_ref=?", (ref,)
                )
            else:
                db.execute(
                    "UPDATE nlp_jobs SET state='pending',owner=NULL,expires_at=NULL WHERE account_ref=? AND state='running'",
                    (ref,),
                )
                db.execute(
                    "UPDATE nlp_accounts SET inventory_digest=?,scan_identity='',scan_complete=0 WHERE account_ref=?",
                    (inventory, ref),
                )
            db.execute(
                "UPDATE nlp_erasure_state SET phase='complete',receipt=?,covered_account_revision=max(covered_account_revision,account_wide) WHERE account_ref=?",
                (encoded(receipt).decode(), ref),
            )
        return receipt
