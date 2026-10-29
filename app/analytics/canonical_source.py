"""Immutable analytics snapshots read from signer-v2 canonical history."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
from app.persistence import sqlite_api as sqlite3
from datetime import datetime
from threading import Lock, local

from app.analytics.evidence_contracts import (
    EvidenceLocation, EvidenceMessage, MAX_EVIDENCE_TEXT_CHARS,
)
from app.analytics.query_execution import QuestionBudget
from app.canonical.read_models import AccountReadModel
from app.persistence.history import HistoryRepository


_LIVE_EVIDENCE = """
    AND m.is_deleted=0 AND c.is_deleted=0
    AND NOT EXISTS (
        SELECT 1 FROM entity_tombstones AS t
        WHERE t.creator_account_id=m.creator_account_id
          AND ((t.entity_kind='message' AND t.entity_id=m.message_id)
            OR (t.entity_kind='chat' AND t.entity_id=m.chat_id)))
    AND NOT EXISTS (
        SELECT 1 FROM deletion_barriers AS b
        WHERE b.creator_account_id=m.creator_account_id
          AND (m.lifecycle_origin!='creator_import' OR m.lifecycle_started_at IS NULL OR b.deleted_at>=m.lifecycle_started_at)
          AND ((b.scope_kind='account' AND b.scope_key='*')
            OR (b.scope_kind='conversation' AND b.scope_key=m.chat_id)
            OR (b.scope_kind='message' AND b.scope_key=m.message_id)
            OR (b.scope_kind='participant' AND b.scope_key=c.platform_user_id)))
    AND NOT EXISTS (
        SELECT 1 FROM participant_deletion_chat_scopes AS p
        WHERE p.creator_account_id=m.creator_account_id AND p.chat_id=m.chat_id
          AND (m.lifecycle_origin!='creator_import' OR m.lifecycle_started_at IS NULL OR p.deleted_at>=m.lifecycle_started_at))
"""


class HistoryAnalyticsSource:
    """Expose signer canonical history through the analytics read-source contract."""

    def __init__(
        self,
        history: HistoryRepository,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.history = history
        self.connection = connection
        from app.analytics.source_tokens import SourceIdentityCache
        self._identity_cache = SourceIdentityCache()
        from app.analytics.question_work import QuestionWork
        self._question_work = QuestionWork()
        self._question_preparation_lock = Lock()
        self._question_scope_local = local()
        from app.analytics.catalog_cache import SourceCatalogCache
        self._catalog_cache = SourceCatalogCache(self._identity_cache)

    def _read(self):
        if self.connection is not None:
            from contextlib import nullcontext

            return nullcontext(self.connection)
        return self.history.database.read()

    def analytics_snapshot(self, account_id: str, *, cancellation_check=None):
        from app.analytics.cancellation import check_cancelled
        from app.analytics.source_snapshot import SourceCatalog, scan_identity, cancellable_source_read
        from app.analytics.errors import CanonicalAccountNotFound

        check = lambda: check_cancelled(cancellation_check)
        with self._read() as connection, cancellable_source_read(connection, cancellation_check):
            own_transaction = self.connection is None and not connection.in_transaction
            if own_transaction:
                connection.execute("BEGIN")
            try:
                row = connection.execute("SELECT canonical_revision FROM account_heads WHERE creator_account_id=?", (account_id,)).fetchone()
                if row is None:
                    raise CanonicalAccountNotFound()
                token = self._identity_cache.token(connection, account_id) if self.connection is None else None
                cached = self._catalog_cache.get(account_id, token) if self.connection is None else None
                if cached is None:
                    identity, digests, count = scan_identity(connection, account_id, int(row[0]), check=check)
                    check()
                    self._identity_cache.put(account_id, token, identity)
                    self._catalog_cache.put(account_id, token, identity, digests)
                else:
                    identity, digests = cached
                    count = 0
                    check()
                from app.analytics.source_tokens import source_identity_proof
                proof = source_identity_proof(account_id, token, identity)
            finally:
                if own_transaction:
                    connection.rollback()
        return SourceCatalog(
            identity, digests,
            lambda chat: self.conversation_read_model(
                account_id, chat, cancellation_check=cancellation_check
            ),
            count,
            identity_proof=proof,
        )

    def verify_identity_proof(self, account_id: str, identity, proof):
        """Rebind a scanned identity to the current token without rescanning content."""

        if self.connection is not None:
            return None
        from app.analytics.source_tokens import verify_source_identity_proof

        with self._read() as connection:
            current = self._identity_cache.token(connection, account_id)
        matched = verify_source_identity_proof(account_id, identity, proof, current)
        if matched:
            self._identity_cache.put(account_id, current, identity)
        return matched

    def read_identity(self, account_id: str):
        from app.analytics.errors import CanonicalAccountNotFound

        try:
            if self.connection is None:
                with self._read() as connection:
                    cached = self._identity_cache.get(account_id, self._identity_cache.token(connection, account_id))
                if cached is not None:
                    return cached
            return self.analytics_snapshot(account_id).identity
        except CanonicalAccountNotFound:
            return None

    def refresh_identity_cache(self, account_id: str) -> None:
        """Ensure an unexpired identity without extending an existing cache lifetime."""

        if self.connection is None:
            self.read_identity(account_id)

    def prepare_question_identity(self, account_id: str, *, cancellation_check=None,
                                  budget=None, return_proof=False):
        """Independently prepare expiring identities on the runtime's owned worker."""
        from app.analytics.cancellation import check_cancelled

        if self.connection is not None:
            raise ValueError("question_live_read_required")
        if budget is not None:
            original_check = cancellation_check
            def cancellation_check():
                budget.check()
                return original_check is not None and original_check()
        check_cancelled(cancellation_check)
        while not self._question_preparation_lock.acquire(timeout=0.05):
            check_cancelled(cancellation_check)
        try:
            return self._prepare_question_identity(account_id, cancellation_check,
                                                  budget=budget, return_proof=return_proof)
        finally:
            self._question_preparation_lock.release()

    def _prepare_question_identity(self, account_id, cancellation_check, *, budget=None,
                                  return_proof=False):
        from app.analytics.cancellation import check_cancelled
        from app.analytics.errors import CanonicalRevisionChanged, ProjectionUnavailable
        from app.analytics.source_snapshot import scan_identity, cancellable_source_read
        from app.analytics.source_tokens import source_identity_proof
        from app.analytics.query_sql import bounded_sql
        from contextlib import nullcontext

        check = lambda: check_cancelled(cancellation_check)
        # Background scope preparation shares its owned connection and SQL
        # budget. Standalone preparation retains its existing cancellation path.
        scoped = (getattr(self._question_scope_local, 'connection', None)
                  if budget is not None and getattr(self._question_scope_local, 'lease', None) is None else None)
        with nullcontext(scoped) if scoped is not None else self._read() as db, \
                (nullcontext() if scoped is not None else bounded_sql(db, budget)
                 if budget is not None else cancellable_source_read(db, cancellation_check)):
            db.execute("BEGIN")
            try:
                row = db.execute("SELECT canonical_revision FROM account_heads WHERE creator_account_id=?",
                                 (account_id,)).fetchone()
                if row is None:
                    return None
                token = self._identity_cache.token(db, account_id)
                if token is None:
                    raise ProjectionUnavailable(availability="error",
                        reason_code="analytics_question_source_tracking_unavailable")
                cached = self._identity_cache.get(account_id, token)
                if cached is not None and not self._identity_cache.preparation_due(account_id, token):
                    check()
                    return source_identity_proof(account_id, token, cached) if return_proof else cached
                identity, digests, _ = scan_identity(db, account_id, int(row[0]), check=check,
                    consume=self._question_work.background(cancellation_check=cancellation_check))
                check()
            finally:
                db.rollback()
            # Recheck outside the scan snapshot before retaining its identity.
            if self._identity_cache.token(db, account_id) != token:
                raise CanonicalRevisionChanged()
            check()
            self._identity_cache.put(account_id, token, identity)
            self._catalog_cache.put(account_id, token, identity, digests)
            return source_identity_proof(account_id, token, identity) if return_proof else identity

    def conversation_read_model(self, account_id: str, conversation_id: str, *, cancellation_check=None):
        from app.analytics.cancellation import check_cancelled
        from app.analytics.source_snapshot import read_conversation, cancellable_source_read

        with self._read() as connection, cancellable_source_read(connection, cancellation_check):
            return read_conversation(connection, account_id, conversation_id,
                check=lambda: check_cancelled(cancellation_check))

    @contextmanager
    def use_question_connection(self, lease):
        """Bind a runtime-owned read lease only to this thread's question scope."""
        if (lease.database is not self.history.database
                or getattr(self._question_scope_local, 'lease', None) is not None):
            raise ValueError('question_connection_binding_invalid')
        self._question_scope_local.lease = lease
        try:
            yield
        finally:
            del self._question_scope_local.lease

    @contextmanager
    def open_question_scope(self, account_id, budget, *, verify_source_token=False,
                            prepare_identity=False):
        """Pin live canonical identity and bound every source read."""

        from app.analytics.query_canonical import CanonicalQuestionScope
        from app.analytics.query_sql import bounded_sql

        if self.connection is not None:
            raise ValueError("question_live_read_required")
        interrupted = []
        lease = getattr(self._question_scope_local, 'lease', None)
        with lease.read() if lease is not None else self.history.database.read() as connection, bounded_sql(connection, budget, interrupted=interrupted):
            if getattr(self._question_scope_local, "connection", None) is not None:
                raise RuntimeError("nested_question_scope")
            self._question_scope_local.connection = connection
            self._question_scope_local.interrupted = interrupted
            try:
                budget.consume(2)
                prepared = (self.prepare_question_identity(account_id, budget=budget, return_proof=True)
                            if prepare_identity else None)
                scope = CanonicalQuestionScope(connection, account_id, budget)
                token = self._identity_cache.token(connection, account_id)
                if prepare_identity:
                    from app.analytics.source_tokens import SourceIdentityProof, verify_source_identity_proof
                    if (not isinstance(prepared, SourceIdentityProof)
                            or verify_source_identity_proof(account_id, prepared.identity, prepared, token) is not True):
                        from app.analytics.errors import ProjectionUnavailable
                        raise ProjectionUnavailable(availability="building")
                    scope.identity = prepared.identity
                else:
                    scope.identity = self._identity_cache.get(account_id, token)
                if scope.identity is None:
                    from app.analytics.errors import ProjectionUnavailable
                    if token is None:
                        raise ProjectionUnavailable(availability="error",
                            reason_code="analytics_question_source_tracking_unavailable")
                    raise ProjectionUnavailable(availability="building",
                        reason_code="analytics_question_identity_preparing")
                scope.check(budget)
                yield scope
                if lease is not None:
                    lease.close_cursors()
                    lease.observe()
                scope.check(budget)
                if verify_source_token:
                    budget.consume(2)
                    # This scope does not open a transaction: recheck the live
                    # account token after every admitted source has been read.
                    current = self._identity_cache.token(connection, account_id)
                    if current != token or current.revision != scope.identity.revision:
                        from app.analytics.errors import ProjectionUnavailable
                        raise ProjectionUnavailable(availability="building")
                    scope.check(budget)
            finally:
                del self._question_scope_local.connection
                del self._question_scope_local.interrupted

    def read_evidence_message(
        self, account_id: str, location: EvidenceLocation, budget: QuestionBudget,
    ) -> EvidenceMessage | None:
        """Read one live, undeleted source by its indexed canonical identity."""

        if self.connection is not None:
            raise ValueError("evidence_live_read_required")
        location = EvidenceLocation.model_validate(location)
        budget.check()

        def read_row(connection):
            from app.analytics.evidence import EvidenceUnavailable

            budget.consume(2)
            token = self._identity_cache.token(connection, account_id)
            if token is None:
                raise EvidenceUnavailable()
            row = _read_evidence_row(connection, account_id, location)
            return row, f"{token.schema}:{token.value}:{token.revision}"

        scoped = getattr(self._question_scope_local, "connection", None)
        if scoped is not None:
            try:
                row, source_state_token = read_row(scoped)
            except sqlite3.OperationalError:
                interrupted = self._question_scope_local.interrupted
                if interrupted:
                    raise interrupted[0] from None
                budget.check()
                raise
        else:
            with self.history.database.read() as connection:
                budget.check()
                timeout_ms = max(1, int(budget.remaining_seconds() * 1000))
                connection.execute(f"PRAGMA busy_timeout={timeout_ms}")
                connection.execute("PRAGMA query_only=ON")
                interrupted = []

                def progress() -> int:
                    try:
                        budget.check()
                    except Exception as error:
                        interrupted.append(error)
                        return 1
                    return 0

                connection.set_progress_handler(progress, 100)
                try:
                    row, source_state_token = read_row(connection)
                except sqlite3.OperationalError:
                    if interrupted:
                        raise interrupted[0] from None
                    budget.check()
                    raise
                finally:
                    connection.set_progress_handler(None, 0)
        budget.check()
        if row is None:
            return None
        return EvidenceMessage(
            account_id=account_id, location=location, source_revision=row[0],
            text=row[1], sent_at=row[2], direction=row[3], sender_id=row[4],
            upstream_updated_at=row[5], content_hash=row[6],
            stream_epoch=row[7], source_sequence=row[8],
            source_state_token=source_state_token,
        )

    def evidence_context_locations(self, account_id, location, before, after, budget, *, cutoff, now):
        """Seek UTC time buckets, then order bounded candidates at full precision."""
        from app.analytics.evidence import EvidenceUnavailable
        from app.analytics.query_contracts import utc_instant

        if any(type(n) is not int or not 0 <= n <= 5 for n in (before, after)):
            raise ValueError('evidence_context_limit_invalid')
        db = getattr(self._question_scope_local, 'connection', None)
        if db is None or self.connection is not None:
            raise EvidenceUnavailable()
        location = EvidenceLocation.model_validate(location)
        budget.consume(2)
        anchor = db.execute("""SELECT julianday(sent_at),sent_at,winning_stream_epoch,
            winning_source_seq,message_id FROM account_messages
            WHERE creator_account_id=? AND chat_id=? AND message_id=? AND is_deleted=0""",
            (account_id, location.conversation_id, location.message_id)).fetchone()
        if anchor is None or anchor[0] is None:
            raise EvidenceUnavailable()
        limits = db.execute('SELECT julianday(?),julianday(?)',
            (cutoff.isoformat(), now.isoformat())).fetchone()
        budget.consume()
        if db.execute("""SELECT 1 FROM account_messages INDEXED BY account_messages_context
                WHERE creator_account_id=? AND chat_id=? AND is_deleted=0
                AND julianday(sent_at) IS NULL LIMIT 1""",
                (account_id, location.conversation_id)).fetchone() is not None:
            raise EvidenceUnavailable()

        def exact_key(row):
            return (utc_instant(row[0]), int(row[1]), int(row[2]), str(row[3]))

        buckets = {}
        def load_bucket(at):
            if at in buckets:
                return buckets[at]
            budget.consume()
            remaining = (budget.limits.max_records - budget.records_examined) // 2
            rows = db.execute("""SELECT sent_at,winning_stream_epoch,winning_source_seq,message_id
                FROM account_messages INDEXED BY account_messages_context
                WHERE creator_account_id=? AND chat_id=? AND is_deleted=0
                  AND julianday(sent_at)=?
                ORDER BY winning_stream_epoch,winning_source_seq,message_id LIMIT ?""",
                (account_id, location.conversation_id, at, max(1, remaining + 1))).fetchall()
            budget.consume(len(rows) * 2)
            found = []
            for row in rows:
                budget.check()
                live = db.execute(f"""SELECT 1 FROM account_messages AS m JOIN account_chats AS c
                    ON c.creator_account_id=m.creator_account_id AND c.chat_id=m.chat_id
                    WHERE m.creator_account_id=? AND m.chat_id=? AND m.message_id=?
                    {_LIVE_EVIDENCE} LIMIT 1""",
                    (account_id, location.conversation_id, row[3])).fetchone()
                if live is not None:
                    key = exact_key(row)
                    if cutoff < key[0] <= now:
                        found.append(key)
            buckets[at] = found
            return found

        anchor_key = exact_key(anchor[1:])
        initial = load_bucket(anchor[0])
        if anchor_key not in initial:
            raise EvidenceUnavailable()

        def side(count, older):
            found = [key for key in initial if (key < anchor_key if older else key > anchor_key)]
            at = anchor[0]
            relation, order = ('<', 'DESC') if older else ('>', 'ASC')
            while len(found) <= count:
                budget.consume()
                next_row = db.execute(f"""SELECT julianday(sent_at) FROM account_messages
                    INDEXED BY account_messages_context
                    WHERE creator_account_id=? AND chat_id=? AND is_deleted=0
                      AND julianday(sent_at)>=? AND julianday(sent_at)<=?
                      AND julianday(sent_at) {relation} ?
                    ORDER BY julianday(sent_at) {order} LIMIT 1""",
                    (account_id, location.conversation_id, limits[0], limits[1], at)).fetchone()
                if next_row is None:
                    break
                at = next_row[0]
                found.extend(load_bucket(at))
            found.sort(reverse=older)
            return found[:count], len(found) > count

        earlier, has_earlier = side(before, True)
        later, has_later = side(after, False)
        keys = list(reversed(earlier)) + [anchor_key] + later
        budget.check()
        return tuple(EvidenceLocation(conversation_id=location.conversation_id, message_id=key[3])
                     for key in keys), len(earlier), has_earlier, has_later

    def nlp_location_page(self, account_id, after=None, *, limit=64):
        """Page locators only; live evidence admission remains a separate read."""
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError('nlp_page_limit')
        after = after or ('','')
        if len(after) != 2 or any(type(v) is not str for v in after):
            raise ValueError('nlp_cursor_invalid')
        from contextlib import nullcontext
        scoped = getattr(self._question_scope_local, 'connection', None)
        with nullcontext(scoped) if scoped is not None else self._read() as db:
            rows = db.execute('SELECT chat_id,message_id FROM account_messages '
                'WHERE creator_account_id=? AND message_id>? '
                'ORDER BY message_id LIMIT ?', (account_id,after[1],limit)).fetchall()
        return tuple(EvidenceLocation(conversation_id=r[0],message_id=r[1]) for r in rows)

    def erasure_source_identity(self, account_id):
        """Capture guarded source tracking without materializing an account."""
        with self._read() as db:
            token = self._identity_cache.token(db, account_id)
            if token is None:
                raise ValueError("nlp_erasure_source_tracking_unavailable")
            return f"{token.schema}:{token.value}:{token.revision}"

    @contextmanager
    def dependency_selection_guard(self, account_id, expected_token):
        """Serialize a short dependent selection with canonical lifecycle writers."""
        if self.connection is not None:
            raise ValueError('nlp_selection_live_source_required')
        # The existing canonical writer transaction is only a fence here. No
        # source fields are copied or changed; dependent storage commits first.
        with self.history.database.transaction() as db:
            token = self._identity_cache.token(db, account_id)
            exists = db.execute('SELECT 1 FROM account_heads WHERE creator_account_id=?', (account_id,)).fetchone()
            if (exists is None or token is None or
                    f'{token.schema}:{token.value}:{token.revision}' != expected_token):
                raise ValueError('nlp_selection_source_changed')
            yield

    def account_revision(self, account_id: str) -> int | None:
        with self._read() as connection:
            row = connection.execute("SELECT canonical_revision FROM account_heads WHERE creator_account_id=?", (account_id,)).fetchone()
        return None if row is None else int(row[0])

    def account_exists(self, creator_account_id: str) -> bool:
        with self._read() as connection:
            return connection.execute(
                "SELECT 1 FROM account_heads WHERE creator_account_id=?",
                (creator_account_id,),
            ).fetchone() is not None

    def account_revisions(self) -> list[tuple[str, int]]:
        with self._read() as connection:
            return [
                (str(row[0]), int(row[1]))
                for row in connection.execute(
                    """SELECT creator_account_id,canonical_revision FROM account_heads
                       ORDER BY creator_account_id"""
                )
            ]

    def account_revision_page(self, *, after: str | None = None, limit: int = 64) -> list[tuple[str, int]]:
        """Read one ordered page from the authoritative account registry."""
        if (type(limit) is not int or not 1 <= limit <= 64
                or (after is not None and (type(after) is not str or not after or len(after) > 1024))):
            raise ValueError('analytics_account_page_invalid')
        with self._read() as connection:
            if after is None:
                return [(str(row[0]), int(row[1])) for row in connection.execute(
                    'SELECT creator_account_id,canonical_revision FROM account_heads '
                    'ORDER BY creator_account_id LIMIT ?', (limit,))]
            return [(str(row[0]), int(row[1])) for row in connection.execute(
                'SELECT creator_account_id,canonical_revision FROM account_heads '
                'WHERE creator_account_id>? ORDER BY creator_account_id LIMIT ?', (after, limit))]

    def account_read_model(self, creator_account_id: str) -> AccountReadModel:
        from app.analytics.source_coverage import AcquisitionCoverage
        from app.analytics.source_snapshot import message_kind_column

        with self._read() as connection:
            head = connection.execute(
                """SELECT canonical_revision FROM account_heads
                   WHERE creator_account_id=?""",
                (creator_account_id,),
            ).fetchone()
            if head is None:
                return AccountReadModel()

            account = AccountReadModel(view_revision=int(head[0]))
            coverage = AcquisitionCoverage(connection, creator_account_id)
            chat_rows = connection.execute(
                """SELECT chat_id,platform_user_id,display_name,upstream_updated_at
                     FROM account_chats
                    WHERE creator_account_id=? AND is_deleted=0 ORDER BY chat_id""",
                (creator_account_id,),
            ).fetchall()
            for row in chat_rows:
                account.conversations[str(row[0])] = {
                    "acquisition_coverage": coverage.conversation(str(row[0])),
                    "conversation_id": str(row[0]),
                    "platform_user_id": row[1] or f"placeholder:{row[0]}",
                    "display_name": row[2],
                    "unread_count": 0,
                    "last_message_at": None,
                    "messages": [],
                }

            kind_column = message_kind_column(connection)
            messages = connection.execute(
                f"""SELECT chat_id,message_id,text,sent_at,direction,
                          winning_stream_epoch,winning_source_seq,{kind_column}
                     FROM account_messages
                    WHERE creator_account_id=? AND is_deleted=0
                    ORDER BY chat_id,sent_at,winning_stream_epoch,
                             winning_source_seq,message_id""",
                (creator_account_id,),
            ).fetchall()
            ordinals: dict[str, int] = {}
            for row in messages:
                conversation_id = str(row[0])
                conversation = account.conversations.get(conversation_id)
                if conversation is None:
                    continue
                ordinal = ordinals.get(conversation_id, 0)
                ordinals[conversation_id] = ordinal + 1
                message = {
                    "message_id": str(row[1]),
                    "source_ordinal": ordinal,
                    "text": str(row[2]),
                    "sent_at": self._iso(str(row[3])),
                    "direction": str(row[4]),
                    "sentiment": None,
                    **({"event_kind": json.loads(row[7])} if row[7] is not None else {}),
                }
                conversation["messages"].append(message)
                conversation["last_message_at"] = message["sent_at"]
            return account

    def canonical_content_digest(self, creator_account_id: str) -> str | None:
        if not self.account_exists(creator_account_id):
            return None
        account = self.account_read_model(creator_account_id)
        encoded = json.dumps(
            {
                "canonical_revision": account.view_revision,
                "conversations": account.conversations,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(
            b"ofca:canonical-account:v2\0" + encoded
        ).hexdigest()

    @staticmethod
    def _iso(value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("canonical message timestamp must include a timezone")
        return parsed.isoformat()


def read_transaction_evidence(connection, account_id, location, budget):
    """Resolve live evidence on the caller's existing canonical transaction."""
    location = EvidenceLocation.model_validate(location)
    budget.consume(2)
    row = _read_evidence_row(connection, account_id, location)
    budget.check()
    if row is None:
        return None
    return EvidenceMessage(account_id=account_id, location=location, source_revision=row[0],
        text=row[1], sent_at=row[2], direction=row[3], sender_id=row[4],
        upstream_updated_at=row[5], content_hash=row[6], stream_epoch=row[7], source_sequence=row[8])


def _read_evidence_row(connection, account_id, location):
    return connection.execute(
        f"""SELECT h.canonical_revision,m.text,m.sent_at,m.direction,
                  m.sender_platform_user_id,m.upstream_updated_at,
                  m.content_hash,m.winning_stream_epoch,m.winning_source_seq
             FROM account_messages AS m
             JOIN account_heads AS h
               ON h.creator_account_id=m.creator_account_id
             JOIN account_chats AS c
               ON c.creator_account_id=m.creator_account_id AND c.chat_id=m.chat_id
            WHERE m.creator_account_id=? AND m.message_id=? AND m.chat_id=?
               AND length(m.text)<=?
               AND length(CAST(m.text AS BLOB))<=?
               {_LIVE_EVIDENCE}
            LIMIT 1""",
        (
            account_id,
            location.message_id,
            location.conversation_id,
            MAX_EVIDENCE_TEXT_CHARS,
            MAX_EVIDENCE_TEXT_CHARS * 4,
        ),
    ).fetchone()
