"""Bounded immutable pre-dispatch acknowledgements in canonical storage."""

from contextlib import contextmanager
from datetime import timezone
import hashlib
import json
from uuid import uuid4

from app.models.text_analysis_client import (
    ClientIntentPut,
    ClientIntentRecord,
    ClientIntentCursor,
    ClientIntentPage,
)
from app.nlp.profiles import canonical_bytes
from app.security.runtime_policy import authorized_account


@contextmanager
def _atomic(db):
    name = 'client_' + uuid4().hex
    db.execute('SAVEPOINT ' + name)
    try:
        yield
    except BaseException:
        db.execute('ROLLBACK TO ' + name)
        raise
    finally:
        db.execute('RELEASE ' + name)


class TextAnalysisClientStore:
    def __init__(self, repository, *, clock):
        self.repository, self.clock = repository, clock

    @staticmethod
    def _scope(policy):
        account = authorized_account(policy, policy.identity.creator_account_id)
        authority = policy.identity_authority
        if authority is None:
            raise ValueError('text_analysis_identity_unavailable')
        return (
            authority.installation_id,
            authority.organization_id,
            account,
            policy.identity.principal_id,
        )

    @staticmethod
    def _record(row):
        from app.models.text_analysis_enrollment import EnrollmentCommand
        from app.models.text_analysis_preview_execution import PreviewPrepareRequest
        from app.models.text_analysis_client import PreviewConfirmIntent

        model = {
            'enrollment_command': EnrollmentCommand,
            'preview_prepare': PreviewPrepareRequest,
            'preview_confirm': PreviewConfirmIntent,
        }.get(row['kind'])
        raw = bytes(row['body'])
        if (
            model is None
            or not 0 < len(raw) <= 8192
            or hashlib.sha256(raw).hexdigest() != row['body_sha256']
        ):
            raise ValueError('text_analysis_client_corrupt')
        body = model.model_validate_json(raw)
        if canonical_bytes(body) != raw:
            raise ValueError('text_analysis_client_corrupt')
        return ClientIntentRecord.model_validate_json(
            json.dumps(
                dict(
                    schema_version='text-analysis-client-intent-record.v1',
                    kind=row['kind'],
                    key=row['intent_key'],
                    body=body.model_dump(mode='json'),
                    body_sha256=row['body_sha256'],
                    retained_at=row['retained_at'],
                )
            )
        )

    def read_in(self, db, kind, key, *, policy):
        self.repository._owned_connection(db)
        cursor = ClientIntentCursor(kind=kind, key=key)
        scope = self._scope(policy)
        row = db.execute(
            'SELECT * FROM text_analysis_client_intents WHERE installation_id=? AND kind=? AND intent_key=?',
            (scope[0], cursor.kind, cursor.key),
        ).fetchone()
        if row is None:
            return None
        if (
            tuple(
                row[name]
                for name in (
                    'installation_id',
                    'organization_id',
                    'creator_account_id',
                    'principal_id',
                )
            )
            != scope
        ):
            raise ValueError('text_analysis_client_actor_conflict')
        return self._record(row)

    def list_in(self, db, *, policy, after=None, limit=64):
        self.repository._owned_connection(db)
        if (
            type(limit) is not int
            or not 1 <= limit <= 64
            or (after is not None and type(after) is not ClientIntentCursor)
        ):
            raise ValueError('text_analysis_client_page_invalid')
        scope = self._scope(policy)
        kind, key = ('', '') if after is None else (after.kind, after.key)
        rows = db.execute(
            'SELECT * FROM text_analysis_client_intents WHERE installation_id=? AND organization_id=? '
            'AND creator_account_id=? AND principal_id=? AND (kind,intent_key)>(?,?) '
            'ORDER BY kind,intent_key LIMIT ?',
            (*scope, kind, key, limit + 1),
        ).fetchall()
        items = tuple(self._record(row) for row in rows[:limit])
        next_after = (
            ClientIntentCursor(kind=items[-1].kind, key=items[-1].key)
            if len(rows) > limit
            else None
        )
        return ClientIntentPage(items=items, next_after=next_after)

    def retain_in(self, db, intent, *, policy):
        self.repository._transaction(db)
        if type(intent) is not ClientIntentPut:
            raise ValueError('text_analysis_client_invalid')
        raw = canonical_bytes(intent.body)
        if not 0 < len(raw) <= 8192:
            raise ValueError('text_analysis_client_body_limit')
        old = self.read_in(db, intent.kind, intent.key, policy=policy)
        if old is not None:
            if canonical_bytes(old.body) != raw:
                raise ValueError('text_analysis_client_conflict')
            return old
        scope = self._scope(policy)
        if (
            db.execute(
                'SELECT count(*) FROM text_analysis_client_intents WHERE installation_id=?',
                (scope[0],),
            ).fetchone()[0]
            >= 4096
        ):
            raise ValueError('text_analysis_client_capacity')
        with _atomic(db):
            retained = (
                self.clock().astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
            )
            db.execute(
                'INSERT INTO text_analysis_client_intents VALUES(?,?,?,?,?,?,?,?,?)',
                (
                    *scope,
                    intent.kind,
                    intent.key,
                    raw,
                    hashlib.sha256(raw).hexdigest(),
                    retained,
                ),
            )
            if self.repository._storage(db) > self.repository.max_storage_bytes:
                raise ValueError('knowledge_storage_full')
            return self.read_in(db, intent.kind, intent.key, policy=policy)
