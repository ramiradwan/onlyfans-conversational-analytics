"""Bounded original-operation storage for native module licence replacement."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4


RESERVATION_BYTES = 128 * 1024
INSTALLATION_BUDGET_BYTES = 8 * 1024 * 1024
METADATA_MAX = 16384
BODY_LIMITS = {
    'prepare_request': 8192, 'read_request': 8192, 'recover_request': 8192,
    'finalize_request': 8192, 'prepare_response': 4096,
    'authorized_response': 24576, 'confirmation': 8192, 'result': 24576,
}
BODY_LEDGER_BYTES = sum(BODY_LIMITS.values()) + 16384
STATES = {'prepared', 'awaiting_authorization', 'authorized', 'adopted', 'expired'}
_METADATA_KEYS = {'schema', 'flow_id', 'scope_sha256', 'operation_id', 'binding',
    'provider_origin', 'provider_name', 'provider_key_name', 'public_key_jwk',
    'state', 'version', 'created_at', 'updated_at', 'operation_expires_at',
    'confirmation_expires_at', 'reference_id', 'digests'}
_BINDING_KEYS = {'organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt'}


class ReissueJournalError(ValueError):
    """A fixed local storage outcome without protected values."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code='reissue_journal_corrupt'):
    if not condition:
        raise ReissueJournalError(code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _time(clock):
    value = clock()
    _need(isinstance(value, datetime) and value.utcoffset() is not None)
    return value.astimezone(timezone.utc).isoformat()


def scope_digest(origin, binding):
    _need(type(origin) is str and len(origin.encode('utf-8')) <= 2048)
    parsed = urlsplit(origin)
    _need(parsed.scheme == 'https' and parsed.hostname and not parsed.username
          and not parsed.password and not parsed.query and not parsed.fragment
          and parsed.path in ('', '/'))
    _need(type(binding) is dict and set(binding) == _BINDING_KEYS)
    _need(all(type(x) is str and 0 < len(x.encode('utf-8')) <= 512 for x in binding.values()))
    return _sha(_encode({'origin': origin, 'binding': binding}))


@dataclass(frozen=True)
class ReissueRecord:
    metadata: dict = field(repr=False)
    bodies: dict = field(repr=False)

    @property
    def flow_id(self):
        return self.metadata['flow_id']

    @property
    def state(self):
        return self.metadata['state']

    @property
    def version(self):
        return self.metadata['version']


class TextAnalysisReissueStore:
    """Reserve complete logical capacity before any hosted preparation."""
    def __init__(self, authentication, *, clock=lambda: datetime.now(timezone.utc)):
        self.authentication = authentication
        self.database = authentication.database
        self.clock = clock

    @classmethod
    def _row(cls, db, row):
        try:
            return cls._checked_row(db, row)
        except ReissueJournalError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
            raise ReissueJournalError('reissue_journal_corrupt') from None

    @staticmethod
    def _checked_row(db, row):
        _need(row is not None, 'reissue_flow_unavailable')
        raw = bytes(row['metadata'])
        _need(len(raw) <= METADATA_MAX and _sha(raw) == row['metadata_sha256'])
        try:
            m = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ReissueJournalError('reissue_journal_corrupt') from None
        _need(type(m) is dict and set(m) == _METADATA_KEYS and _encode(m) == raw)
        _need(m['schema'] == 'text-analysis-reissue-operation/v1')
        for name in ('flow_id', 'scope_sha256', 'operation_id', 'state', 'version', 'reference_id'):
            _need(m[name] == row[name])
        _need(type(m['state']) is str and m['state'] in STATES and type(m['version']) is int and 1 <= m['version'] <= 2**53-1)
        _need(type(m['flow_id']) is str and type(m['operation_id']) is str)
        _need(re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', m['flow_id']))
        _need(re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', m['operation_id']))
        _need(scope_digest(m['provider_origin'], m['binding']) == m['scope_sha256'])
        for name, cap in [('provider_name', 256), ('provider_key_name', 256), ('public_key_jwk', 1024),
                          ('created_at', 40), ('updated_at', 40)]:
            _need(type(m[name]) is str and 0 < len(m[name].encode('utf-8')) <= cap)
        for name in ('operation_expires_at', 'confirmation_expires_at'):
            _need(m[name] is None or type(m[name]) is str and 0 < len(m[name]) <= 40)
        _need(type(m['digests']) is dict and set(m['digests']) == set(BODY_LIMITS))
        bodies = {}
        for name, cap in BODY_LIMITS.items():
            value = None if row[name] is None else bytes(row[name])
            _need(value is None or 0 < len(value) <= cap)
            _need(m['digests'][name] == (None if value is None else _sha(value)))
            bodies[name] = value
        _need(all(bodies[x] is not None for x in ('prepare_request', 'read_request', 'recover_request')))
        if m['state'] != 'prepared':
            _need(bodies['prepare_response'] is not None and m['operation_expires_at'] is not None)
        if m['state'] in ('authorized', 'adopted'):
            _need(bodies['confirmation'] is not None and bodies['authorized_response'] is not None)
        if m['state'] == 'adopted':
            _need(bodies['result'] is not None and bodies['finalize_request'] is not None and m['reference_id'])
            authority = db.execute('SELECT kind,compact_jws,compact_jws_sha256 FROM text_analysis_authority_references WHERE reference_id=?',
                                   (m['reference_id'],)).fetchone()
            _need(authority is not None and authority['kind'] == 'license')
            try:
                token = json.loads(bodies['result'])['signed_authority']
                token_bytes = token.encode('ascii')
            except (ValueError, UnicodeError, KeyError, TypeError, AttributeError):
                raise ReissueJournalError('reissue_journal_corrupt') from None
            _need(len(token_bytes) <= 16384 and authority['compact_jws'] == token
                  and authority['compact_jws_sha256'] == _sha(token_bytes))
        else:
            _need(bodies['result'] is None and m['reference_id'] is None)
        _need(sum(len(x) for x in bodies.values() if x is not None) + 16384 + len(raw)
              <= BODY_LEDGER_BYTES + METADATA_MAX)
        return ReissueRecord(m, bodies)

    def get(self, flow_id):
        with self.database.read() as db:
            return self._row(db, db.execute('SELECT * FROM text_analysis_reissue_operations WHERE flow_id=?',
                                            (flow_id,)).fetchone())

    def active(self, scope):
        with self.database.read() as db:
            row = db.execute("SELECT * FROM text_analysis_reissue_operations WHERE scope_sha256=? AND state IN ('prepared','awaiting_authorization','authorized')", (scope,)).fetchone()
            return None if row is None else self._row(db, row)

    def latest(self, scope):
        with self.database.read() as db:
            records = [self._row(db, row) for row in db.execute(
                'SELECT * FROM text_analysis_reissue_operations WHERE scope_sha256=? LIMIT 65', (scope,))]
            _need(len(records) <= INSTALLATION_BUDGET_BYTES // RESERVATION_BYTES)
            return max(records, key=lambda r: (r.state in ('prepared', 'awaiting_authorization', 'authorized'),
                       r.metadata['created_at'], r.flow_id)) if records else None

    def begin(self, *, origin, context, provider_name, provider_key_name, read_request, recover_request):
        binding = context.binding
        scope = scope_digest(origin, binding)
        now = _time(self.clock)
        bodies = {name: None for name in BODY_LIMITS}
        bodies.update(prepare_request=context.original_prepare, read_request=read_request, recover_request=recover_request)
        m = dict(schema='text-analysis-reissue-operation/v1', flow_id=str(uuid4()), scope_sha256=scope,
                 operation_id=context.operation_id, binding=binding, provider_origin=origin,
                 provider_name=provider_name, provider_key_name=provider_key_name,
                 public_key_jwk=context.public_key_jwk.decode('utf-8'), state='prepared', version=1,
                 created_at=now, updated_at=now, operation_expires_at=None,
                 confirmation_expires_at=None, reference_id=None,
                 digests={name: None if value is None else _sha(value) for name, value in bodies.items()})
        raw = _encode(m)
        _need(len(raw) <= METADATA_MAX, 'reissue_capacity_exhausted')
        for name, value in bodies.items():
            _need(value is None or type(value) is bytes and 0 < len(value) <= BODY_LIMITS[name])
        with self.database.transaction() as db:
            prior = db.execute('SELECT * FROM text_analysis_reissue_operations WHERE operation_id=?',
                               (context.operation_id,)).fetchone()
            if prior is not None:
                record = self._row(db, prior)
                _need(record.metadata['scope_sha256'] == scope and record.bodies['prepare_request'] == context.original_prepare
                      and record.metadata['public_key_jwk'] == m['public_key_jwk'], 'reissue_conflict')
                return record
            _need(db.execute("SELECT 1 FROM text_analysis_reissue_operations WHERE scope_sha256=? AND state IN ('prepared','awaiting_authorization','authorized')", (scope,)).fetchone() is None, 'reissue_conflict')
            count = db.execute('SELECT count(*) FROM text_analysis_reissue_operations').fetchone()[0]
            _need((count + 1) * RESERVATION_BYTES <= INSTALLATION_BUDGET_BYTES, 'reissue_capacity_exhausted')
            columns = ['flow_id', 'scope_sha256', 'operation_id', 'state', 'version', 'metadata', 'metadata_sha256', *BODY_LIMITS, 'reference_id']
            values = [m['flow_id'], scope, m['operation_id'], 'prepared', 1, raw, _sha(raw), *bodies.values(), None]
            db.execute('INSERT INTO text_analysis_reissue_operations (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', values)
            return self._row(db, db.execute('SELECT * FROM text_analysis_reissue_operations WHERE flow_id=?', (m['flow_id'],)).fetchone())

    def assert_current(self, record):
        current = self.get(record.flow_id)
        _need(current.version == record.version and current.metadata == record.metadata, 'reissue_conflict')

    def _change(self, db, record, *, state=None, bodies=None, metadata=None):
        current = self._row(db, db.execute('SELECT * FROM text_analysis_reissue_operations WHERE flow_id=?', (record.flow_id,)).fetchone())
        _need(current.version == record.version, 'reissue_conflict')
        m, values = dict(current.metadata), dict(current.bodies)
        values.update(bodies or {})
        m.update(metadata or {})
        m.update(state=state or current.state, version=current.version+1, updated_at=_time(self.clock),
                 digests={name: None if value is None else _sha(value) for name, value in values.items()})
        raw = _encode(m)
        _need(len(raw) <= METADATA_MAX, 'reissue_capacity_exhausted')
        for name, value in values.items():
            _need(value is None or type(value) is bytes and 0 < len(value) <= BODY_LIMITS[name])
        assignments = ['state=?', 'version=?', 'metadata=?', 'metadata_sha256=?', 'reference_id=?'] + [name+'=?' for name in BODY_LIMITS]
        db.execute('UPDATE text_analysis_reissue_operations SET '+','.join(assignments)+' WHERE flow_id=? AND version=?',
            [m['state'], m['version'], raw, _sha(raw), m['reference_id'], *values.values(), record.flow_id, current.version])
        return self._row(db, db.execute('SELECT * FROM text_analysis_reissue_operations WHERE flow_id=?', (record.flow_id,)).fetchone())

    def prepared(self, record, projection):
        if record.bodies['prepare_response'] is not None:
            _need(record.bodies['prepare_response'] == projection.response_bytes, 'reissue_conflict')
            return record
        with self.database.transaction() as db:
            return self._change(db, record, state='awaiting_authorization',
                bodies={'prepare_response': projection.response_bytes},
                metadata={'operation_expires_at': projection.context.operation_expires_at})

    def authorized(self, record, projection):
        confirmation = projection.confirmation
        _need(confirmation is not None)
        if record.bodies['confirmation'] is not None:
            _need(record.bodies['confirmation'] == confirmation.confirmation_bytes, 'reissue_conflict')
            return record
        with self.database.transaction() as db:
            return self._change(db, record, state='authorized',
                bodies={'authorized_response': projection.response_bytes, 'confirmation': confirmation.confirmation_bytes},
                metadata={'confirmation_expires_at': confirmation.expires_at})

    def finalization(self, record, inner):
        _need(record.state == 'authorized')
        if record.bodies['finalize_request'] is not None:
            _need(record.bodies['finalize_request'] == inner, 'reissue_conflict')
            return record
        with self.database.transaction() as db:
            return self._change(db, record, bodies={'finalize_request': inner})

    def expired(self, record):
        _need(record.state != 'adopted', 'reissue_conflict')
        with self.database.transaction() as db:
            return self._change(db, record, state='expired')

    def adopt(self, record, result, *, authority_store, authority_context, trust, schemas, policy=None):
        _need(authority_store.database is self.database)
        with self.database.transaction() as db:
            if policy is not None:
                _need(self.authentication.runtime_policy_is_current_in(db, policy, now=self.clock()), 'reissue_policy_stale')
            current = self._row(db, db.execute('SELECT * FROM text_analysis_reissue_operations WHERE flow_id=?', (record.flow_id,)).fetchone())
            from app.security.text_analysis_reissue import (
                ReissueContext, ValidatedReissueResult, validate_confirmation, validate_reissue_result,
            )
            _need(type(result) is ValidatedReissueResult, 'reissue_result_invalid')
            try:
                context = ReissueContext(current.bodies['prepare_request'],
                    current.metadata['public_key_jwk'].encode('utf-8'), current.metadata['operation_expires_at'])
                confirmation = validate_confirmation(current.bodies['confirmation'], context=context, schemas=schemas)
                checked = validate_reissue_result(result.response_bytes, confirmation=confirmation,
                    original_finalization_request=current.bodies['finalize_request'],
                    authority_context=authority_context, trust=trust, schemas=schemas,
                    previous_response=current.bodies['result'])
                _need(checked == result, 'reissue_result_invalid')
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                raise ReissueJournalError('reissue_result_invalid') from None
            if current.state == 'adopted':
                _need(current.bodies['result'] == result.response_bytes, 'reissue_conflict')
                return current
            _need(current.version == record.version and current.state in ('authorized', 'expired')
                  and result.operation_id == current.metadata['operation_id']
                  and current.bodies['finalize_request'] == result.original_finalization_request
                  and current.bodies['confirmation'] == result.confirmation.confirmation_bytes, 'reissue_conflict')
            reference = authority_store.import_verified_reference_in(db, result.authority.compact_jws,
                kind='license', context=authority_context, trust=trust, schemas=schemas)
            _need(reference.kind == 'license')
            return self._change(db, current, state='adopted', bodies={'result': result.response_bytes},
                                metadata={'reference_id': reference.reference_id})


def validate_reissue_security_in(db):
    """Validate recovery ownership without contacting a key provider or server."""
    try:
        records = [TextAnalysisReissueStore._row(db, row) for row in db.execute(
            'SELECT * FROM text_analysis_reissue_operations LIMIT 65')]
        _need(len(records) <= INSTALLATION_BUDGET_BYTES // RESERVATION_BYTES)
        if not records:
            return
        key = db.execute('SELECT * FROM installation_key_reference WHERE singleton=1 AND activated_at IS NOT NULL').fetchone()
        _need(key is not None)
        for record in records:
            m = record.metadata
            _need(all(key[name] == m[name] for name in ('provider_name', 'provider_key_name'))
                  and all(key[name] == m['binding'][name] for name in ('installation_key_id', 'installation_key_jkt'))
                  and _encode(json.loads(key['public_key_jwk'])) == _encode(json.loads(m['public_key_jwk'])))
            original = json.loads(record.bodies['prepare_request'])
            _need(original['binding'] == m['binding'] and original['operation_id'] == m['operation_id'])
            for phase in ('read', 'recover'):
                inner = json.loads(record.bodies[phase+'_request'])
                _need(set(inner) == {'profile', 'operation_id', 'binding'}
                      and all(inner[name] == original[name] for name in inner))
    except ReissueJournalError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        raise ReissueJournalError('reissue_journal_corrupt') from None
