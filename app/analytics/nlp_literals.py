"""Source-verified literal derivations and immutable bounded publications."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
import hashlib
import json
from app.persistence import sqlite_api as sqlite3
import time
import unicodedata
from uuid import uuid4

from app.analytics.errors import AnalyticsError, ProjectionUnavailable
from app.analytics.nlp_findings_store import (FrozenFindings, encoded, require_current_snapshot,
                                             validate_source_bound_record)
from app.analytics.validation_receipt import content_stamp
from app.nlp import normalization as n, v2
from app.nlp.profiles import PolicyIdentity, canonical_bytes, read_profile

MAX_BYTES = 32 * 1024 * 1024
MAX_BATCH_BYTES = 65_536
MAX_BATCHES = 512
MAX_OCCURRENCES = 100_000
MAX_RECORDS = 100_000
PAGE_ROWS = 64
TABLES = ('nlp_literal_accounts', 'nlp_literal_derivations', 'nlp_literal_derivation_batches',
          'nlp_literal_publications', 'nlp_literal_publication_batches')
_POLICY_BYTES = encoded({'schema': 'nlp-literal-policy.v1', 'unicode': 'NFC', 'strip': True,
                         'case': 'preserve', 'max_codepoints': 256, 'equivalence': False,
                         'literal_type_schema': 'nlp-literal-type.v1',
                         'type_inputs': ['task_contract','canonical_exact_taxonomy','category_id'],
                         'namespace': 'random_per_account_enrollment_removed_on_account_erasure'})
POLICY = PolicyIdentity(policy_id='nlp.literal', revision='1', sha256=hashlib.sha256(_POLICY_BYTES).hexdigest())
EMPTY_LEDGER = hashlib.sha256(encoded([])).hexdigest()


class LiteralUnavailable(AnalyticsError):
    code = 'analytics_literals_unavailable'
    public_message = 'Literal findings are unavailable for this saved analysis.'

    def __init__(self, reason='not_prepared'):
        if reason not in ('not_prepared', 'capacity', 'task_not_declared'):
            raise ValueError('literal unavailability reason invalid')
        self.reason = reason
        super().__init__()


class LiteralIntegrityError(AnalyticsError):
    code = 'analytics_literals_invalid'
    public_message = 'Literal findings could not be verified.'


@dataclass(frozen=True)
class LiteralPublicationData:
    envelope: n.LiteralPublicationEnvelope
    nodes: tuple[n.LiteralNode, ...]
    mappings: tuple[n.OccurrenceMapping, ...]
    dispositions: tuple[n.LiteralDisposition, ...]
    records: tuple[n.LiteralRecordState, ...]

    @property
    def publication_id(self):
        return self.envelope.identity_digest()


def supported(db):
    return db.execute('PRAGMA user_version').fetchone()[0] >= 33


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _load(raw):
    raw = bytes(raw)
    if not 0 < len(raw) <= MAX_BATCH_BYTES:
        raise LiteralIntegrityError()
    value = json.loads(raw, object_pairs_hook=v2._unique_object,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if encoded(value) != raw:
        raise LiteralIntegrityError()
    return value


def _guard(db):
    if not supported(db):
        raise LiteralUnavailable()
    if content_stamp(db) is None:
        raise LiteralIntegrityError()


def serialized_bytes(db):
    if not supported(db):
        return 0
    sizes = {
        'nlp_literal_accounts': '2*(length(account_ref)+length(namespace))+128',
        'nlp_literal_derivations': 'length(account_ref)+length(record_digest)+length(document)+128',
        'nlp_literal_derivation_batches': 'length(account_ref)+length(record_digest)+length(digest)+length(document)+136',
        'nlp_literal_publications': 'length(account_ref)+length(snapshot_id)+length(owner)+length(state)+COALESCE(length(publication_id),0)+length(document)+136',
        'nlp_literal_publication_batches': 'length(account_ref)+length(snapshot_id)+length(digest)+length(document)+136',
    }
    return sum(db.execute('SELECT COALESCE(sum(' + expression + '),0) FROM ' + table).fetchone()[0]
               for table,expression in sizes.items())


def _capacity(db, additional):
    if additional < 0 or serialized_bytes(db) + additional > MAX_BYTES:
        raise LiteralUnavailable('capacity')


def ensure_namespace(db, account):
    if not supported(db):
        return None
    row = db.execute('SELECT namespace FROM nlp_literal_accounts WHERE account_ref=?', (account,)).fetchone()
    if row:
        return row[0]
    namespace = uuid4().hex
    _capacity(db, 2*(len(account)+len(namespace))+128)
    db.execute('INSERT INTO nlp_literal_accounts VALUES(?,?)', (account, namespace))
    return namespace


def _batches(rows, check):
    result, page = [], []
    for row in rows:
        check()
        candidate = encoded({'schema': 'nlp-literal-content.v1', 'items': page + [row]})
        if page and (len(page) >= PAGE_ROWS or len(candidate) > MAX_BATCH_BYTES):
            result.append(encoded({'schema': 'nlp-literal-content.v1', 'items': page}))
            page = []
        page.append(row)
        if len(encoded({'schema': 'nlp-literal-content.v1', 'items': page})) > MAX_BATCH_BYTES:
            raise LiteralUnavailable('capacity')
        if len(result) >= MAX_BATCHES:
            raise LiteralUnavailable('capacity')
    if page:
        result.append(encoded({'schema': 'nlp-literal-content.v1', 'items': page}))
    if len(result) > MAX_BATCHES or sum(map(len, result)) > MAX_BYTES:
        raise LiteralUnavailable('capacity')
    return result


def _rows(db, table, account, key_column, key, check):
    result, hashes = [], []
    for index, row in enumerate(db.execute('SELECT ordinal,digest,document FROM ' + table +
            ' WHERE account_ref=? AND ' + key_column + '=? ORDER BY ordinal', (account, key))):
        check()
        if index >= MAX_BATCHES or row[0] != index or _hash(bytes(row[2])) != row[1]:
            raise LiteralIntegrityError()
        value = _load(row[2])
        if set(value) != {'schema', 'items'} or value['schema'] != 'nlp-literal-content.v1' or not isinstance(value['items'], list):
            raise LiteralIntegrityError()
        if not 1 <= len(value['items']) <= PAGE_ROWS:
            raise LiteralIntegrityError()
        hashes.append(row[1])
        result.extend(value['items'])
        if len(result) > 3 * MAX_OCCURRENCES + MAX_RECORDS:
            raise LiteralIntegrityError()
    return result, hashes


def _mention(profile, record):
    binding = next((b for b in profile.bindings if b.task == 'concept_mentions'), None)
    if binding is None:
        raise LiteralUnavailable('task_not_declared')
    component = profile.component(binding.component_id)
    task = next(t for t in component.tasks if t.task == 'concept_mentions')
    payload = next(t.result.payload for t in record.tasks if t.result.payload.task == 'concept_mentions')
    return component, task, payload


def _base(profile, record, record_digest, namespace):
    component, task, payload = _mention(profile, record)
    return {'schema': 'nlp-literal-derivation.v1', 'record_sha256': record_digest,
            'namespace': namespace, 'policy': POLICY.model_dump(mode='json'),
            'source': record.source.model_dump(mode='json'), 'profile_sha256': profile.identity_digest(),
            'component_id': component.component_id, 'component_sha256': component.identity_digest(),
            'task_contract': task.task_contract, 'taxonomy': task.taxonomy.model_dump(mode='json'),
            'outcome': payload.outcome, 'coverage': payload.coverage.model_dump(mode='json')}


def capture_record(db, profile, source, record, record_digest, *, check=lambda: None, record_bytes=None):
    """Caller owns the current canonical completion transaction and source."""
    if not supported(db) or not any(b.task == 'concept_mentions' for b in profile.bindings):
        return False
    _guard(db)
    raw = record.model_dump_json().encode() if record_bytes is None else record_bytes
    validated = validate_source_bound_record(profile, source, raw)
    if validated != record or _hash(raw) != record_digest:
        raise LiteralIntegrityError()
    db.execute('SAVEPOINT literal_capture')
    try:
        namespace = ensure_namespace(db, record.source.account_scope)
        header = _base(profile, record, record_digest, namespace)
        _, task, payload = _mention(profile, record)
        items = []
        for finding in payload.findings:
            check()
            key = unicodedata.normalize('NFC', source.text[finding.span.start:finding.span.end].strip())
            entry = {'category_id': finding.concept_id, 'span': finding.span.model_dump(mode='json'),
                     'type_id': n.literal_type_id(task.task_contract, task.taxonomy, finding.concept_id)}
            if not key or len(key) > 256:
                entry['reason'] = 'empty_key' if not key else 'key_too_long'
            else:
                entry['node'] = n.provisional_literal_node(namespace, entry['type_id'], key).model_dump(mode='json')
            items.append(entry)
        batches = _batches(items, check)
        header['batches'] = list(map(_hash, batches))
        raw = encoded(header)
        if len(raw) > MAX_BATCH_BYTES:
            raise LiteralUnavailable('capacity')
        old = db.execute('SELECT document FROM nlp_literal_derivations WHERE account_ref=? AND record_digest=?',
                         (record.source.account_scope, record_digest)).fetchone()
        if old:
            if bytes(old[0]) != raw:
                raise LiteralIntegrityError()
            _checked_derivation(db, profile, record, record_digest, namespace, check)
        else:
            _capacity(db, len(raw)+len(record.source.account_scope)+len(record_digest)+128 +
                      sum(len(b)+len(record.source.account_scope)+len(record_digest)+200 for b in batches))
            db.execute('INSERT INTO nlp_literal_derivations VALUES(?,?,?)', (record.source.account_scope, record_digest, raw))
            db.executemany('INSERT INTO nlp_literal_derivation_batches VALUES(?,?,?,?,?)',
                [(record.source.account_scope, record_digest, i, _hash(b), b) for i, b in enumerate(batches)])
        db.execute('RELEASE literal_capture')
        return True
    except BaseException as error:
        db.execute('ROLLBACK TO literal_capture')
        db.execute('RELEASE literal_capture')
        if isinstance(error, (ValueError, TypeError, KeyError, RecursionError, sqlite3.Error)):
            raise LiteralIntegrityError() from error
        raise


def _checked_derivation(db, profile, record, key, namespace, check, *, required=False):
    row = db.execute('SELECT document FROM nlp_literal_derivations WHERE account_ref=? AND record_digest=?',
                     (record.source.account_scope, key)).fetchone()
    if row is None:
        raise LiteralIntegrityError() if required else LiteralUnavailable()
    header = _load(row[0])
    entries, hashes = _rows(db, 'nlp_literal_derivation_batches', record.source.account_scope, 'record_digest', key, check)
    if header != {**_base(profile, record, key, namespace), 'batches': hashes}:
        raise LiteralIntegrityError()
    _, task, payload = _mention(profile, record)
    if len(entries) != len(payload.findings):
        raise LiteralIntegrityError()
    for entry, finding in zip(entries, payload.findings):
        check()
        base = {'category_id': finding.concept_id, 'span': finding.span.model_dump(mode='json'),
                'type_id': n.literal_type_id(task.task_contract, task.taxonomy, finding.concept_id)}
        if 'node' in entry:
            node = n.LiteralNode.model_validate_json(encoded(entry['node']))
            if node.account_namespace != namespace or node.type_id != base['type_id'] or entry != {**base, 'node': node.model_dump(mode='json')}:
                raise LiteralIntegrityError()
        elif entry not in ({**base, 'reason': 'empty_key'}, {**base, 'reason': 'key_too_long'}):
            raise LiteralIntegrityError()
    return header, entries


def _material(db, binding, value, check, *, required=False):
    profile = read_profile(encoded(value['snapshot']['profile']))
    if not any(b.task == 'concept_mentions' for b in profile.bindings):
        raise LiteralUnavailable('task_not_declared')
    row = db.execute('SELECT namespace FROM nlp_literal_accounts WHERE account_ref=?', (binding.account_ref,)).fetchone()
    if row is None:
        raise LiteralIntegrityError() if required else LiteralUnavailable()
    if len(value['records']) > MAX_RECORDS:
        raise LiteralUnavailable('capacity')
    namespace = row[0]
    if (not isinstance(value['records'], list) or len(set(value['records']))!=len(value['records'])):
        raise LiteralIntegrityError()
    seen_sources = set()
    result, nodes, count, mapped = [], set(), 0, 0
    material_bytes = 0
    def add(item):
        nonlocal material_bytes
        material_bytes += len(encoded(item))
        if material_bytes > MAX_BYTES:
            raise LiteralUnavailable('capacity')
        result.append(item)
    for key in value['records']:
        check()
        stored = db.execute('SELECT document FROM nlp_records WHERE account_ref=? AND record_digest=?',
                            (binding.account_ref, key)).fetchone()
        if stored is None or _hash(bytes(stored[0])) != key:
            raise LiteralIntegrityError()
        record = v2.read_record(bytes(stored[0]))
        v2.validate_record(profile, record.source, bytes(stored[0]))
        if record.source.account_scope != binding.account_ref or record.source.source_ref in seen_sources:
            raise LiteralIntegrityError()
        seen_sources.add(record.source.source_ref)
        header, entries = _checked_derivation(db, profile, record, key, namespace, check, required=required)
        mapped_here = sum('node' in entry for entry in entries)
        state = n.LiteralRecordState(record_sha256=key, source=record.source, outcome=header['outcome'],
            coverage=record.tasks[next(i for i,t in enumerate(record.tasks) if t.result.payload.task == 'concept_mentions')].result.payload.coverage,
            occurrences=len(entries), mapped=mapped_here, unrepresentable=len(entries)-mapped_here)
        add({'kind': 'record', 'record': state.model_dump(mode='json')})
        extraction = n.ExtractionProvenance(extraction_record_sha256=key, extraction_snapshot_sha256=binding.snapshot_id,
            profile_sha256=profile.identity_digest(), component_id=header['component_id'],
            component_sha256=header['component_sha256'], taxonomy=profile.component(header['component_id']).tasks[
                next(i for i,t in enumerate(profile.component(header['component_id']).tasks) if t.task == 'concept_mentions')].taxonomy)
        for entry in entries:
            check()
            anchor = n.OccurrenceAnchor(source=record.source, category_id=entry['category_id'], span=finding_span(entry))
            if 'node' in entry:
                node = n.LiteralNode.model_validate_json(encoded(entry['node']))
                if node.node_id not in nodes:
                    nodes.add(node.node_id)
                    add({'kind': 'node', 'node': node.model_dump(mode='json')})
                mapping = n.OccurrenceMapping(occurrence=anchor, extraction=extraction, type_id=entry['type_id'],
                    node_id=node.node_id, normalization_policy=POLICY, reason='unreviewed_equivalence')
                add({'kind': 'mapping', 'mapping': mapping.model_dump(mode='json')})
            else:
                disposition = n.LiteralDisposition(occurrence=anchor, extraction=extraction,
                                                  type_id=entry['type_id'], reason=entry['reason'])
                add({'kind': 'disposition', 'disposition': disposition.model_dump(mode='json')})
        count += len(entries)
        mapped += mapped_here
        if count > MAX_OCCURRENCES:
            raise LiteralUnavailable('capacity')
    batches = _batches(result, check)
    snapshot = value['snapshot']
    manifest = n.NormalizationPublication(account_scope=binding.account_ref,
        extraction_snapshot_sha256=binding.snapshot_id, normalization_policy=POLICY,
        ledger_revision=0, ledger_sha256=EMPTY_LEDGER, source_validity_sha256=snapshot['source_validity_sha256'],
        projection_policy=POLICY, completion_revision=snapshot['completion_revision'],
        availability_revision=snapshot['availability_revision'], mapping_batch_sha256=tuple(sorted(map(_hash,batches))),
        membership_batch_sha256=())
    envelope = _envelope(publication=manifest, account_namespace=namespace,
        profile_sha256=profile.identity_digest(), records=len(value['records']), occurrences=count,
        mapped=mapped, unrepresentable=count-mapped, content_batch_sha256=tuple(map(_hash,batches)))
    return envelope, batches, result


def _envelope(**fields):
    payload = dict(fields, publication=fields['publication'].model_dump(mode='json'),
                   schema_version='nlp-literal-publication.v1', canonicalization='nlp-canonical.v1')
    if len(encoded(payload)) > MAX_BATCH_BYTES:
        raise LiteralUnavailable('capacity')
    return n.LiteralPublicationEnvelope(**fields)


def finding_span(entry):
    return n.Span.model_validate_json(encoded(entry['span']))


def _current(db, binding):
    _guard(db)
    if not db.execute('SELECT 1 FROM nlp_snapshots WHERE account_ref=? AND snapshot_id=?',
                      (binding.account_ref, binding.snapshot_id)).fetchone():
        raise ProjectionUnavailable(availability='building')
    try:
        value = require_current_snapshot(db, binding, verify_records=False)
    except ValueError as error:
        if str(error) in ('nlp_snapshot_stale', 'nlp_erasure_pending'):
            raise ProjectionUnavailable(availability='building') from None
        raise LiteralIntegrityError() from error
    if db.execute('SELECT 1 FROM nlp_snapshot_expirations WHERE account_ref=? AND snapshot_id=?',
                  (binding.account_ref, binding.snapshot_id)).fetchone() or db.execute(
            'SELECT 1 FROM nlp_snapshot_inventory WHERE account_ref=? AND snapshot_id=? AND complete=2',
            (binding.account_ref, binding.snapshot_id)).fetchone():
        raise ProjectionUnavailable(availability='building')
    return value


def _token(db, binding):
    from app.analytics.nlp_findings_store import require_erasure_idle
    try:
        require_erasure_idle(db, binding.account_ref)
    except ValueError as exc:
        if str(exc) == "nlp_erasure_pending":
            raise ProjectionUnavailable(availability="building") from exc
        raise
    row = db.execute('SELECT selection_epoch,completion_revision,availability_revision,inventory_digest,scan_complete,mode,profile_json '
                     'FROM nlp_accounts WHERE account_ref=?', (binding.account_ref,)).fetchone()
    if row is None or row[5] in ('withdrawn','removed'):
        raise ProjectionUnavailable(availability='building')
    return tuple(row)


def publish(database, binding, *, canonical, source_current, check=lambda: None, visibility_guard=nullcontext):
    """Authenticate in readers and seal with short, owned writer transactions."""
    deadline = time.monotonic() + 30
    def tick():
        check()
        if time.monotonic() >= deadline:
            raise LiteralUnavailable('capacity')
    owner = uuid4().hex
    started = False
    try:
        with database.read() as db:
            db.execute('BEGIN')
            value = _current(db, binding)
            if (canonical.revision != value["snapshot"]["canonical_revision"] or
                    canonical.content_digest != value["canonical_content_digest"]):
                raise ProjectionUnavailable(availability="building")
            token = _token(db, binding)
            old = db.execute('SELECT state FROM nlp_literal_publications WHERE account_ref=? AND snapshot_id=?',
                             (binding.account_ref,binding.snapshot_id)).fetchone()
            reuse = bool(old and old[0] == 'sealed')
            if reuse:
                envelope = checked_publication(db,binding,check=tick).envelope
                batches = []
            else:
                envelope,batches,_ = _material(db,binding,value,tick)
            stamp = content_stamp(db)
        raw = canonical_bytes(envelope)
        tick()
        source_current()
        with visibility_guard(), database.transaction() as db:
            tick()
            if content_stamp(db) != stamp or _token(db,binding) != token:
                raise ProjectionUnavailable(availability='building')
            old = db.execute('SELECT state,document,expires_at,publication_id FROM nlp_literal_publications WHERE account_ref=? AND snapshot_id=?',
                             (binding.account_ref,binding.snapshot_id)).fetchone()
            if reuse:
                if old is None or old[0]!='sealed' or bytes(old[1])!=raw or old[3]!=envelope.identity_digest():
                    raise LiteralIntegrityError()
                return envelope.identity_digest()
            if old:
                if old[0]=='sealed' or old[2]>time.time():
                    raise LiteralUnavailable()
                db.execute('DELETE FROM nlp_literal_publications WHERE account_ref=? AND snapshot_id=?',
                           (binding.account_ref,binding.snapshot_id))
            _capacity(db, len(raw)+len(binding.account_ref)+len(binding.snapshot_id)+len(owner)+206 +
                      sum(len(b)+len(binding.account_ref)+len(binding.snapshot_id)+200 for b in batches))
            db.execute("INSERT INTO nlp_literal_publications VALUES(?,?,?,?, 'building',NULL,?)",
                       (binding.account_ref,binding.snapshot_id,owner,time.time()+30,b''))
            started = True
        # One already-bounded batch per writer, at most 64 content rows / 64 KiB.
        for ordinal, batch in enumerate(batches):
            tick()
            source_current()
            with database.transaction() as db:
                _owned(db,binding,owner,token)
                _capacity(db,len(batch)+len(binding.account_ref)+len(binding.snapshot_id)+200)
                db.execute('INSERT INTO nlp_literal_publication_batches VALUES(?,?,?,?,?)',
                           (binding.account_ref,binding.snapshot_id,ordinal,_hash(batch),batch))
        tick()
        with database.read() as db:
            db.execute('BEGIN')
            if _current(db,binding)!=value:
                raise ProjectionUnavailable(availability='building')
            _owned(db,binding,owner,token)
            _, hashes = _rows(db,'nlp_literal_publication_batches',binding.account_ref,'snapshot_id',binding.snapshot_id,tick)
            if hashes!=list(envelope.content_batch_sha256):
                raise LiteralIntegrityError()
            seal_stamp=content_stamp(db)
        tick()
        source_current()
        with visibility_guard(), database.transaction() as db:
            tick()
            if content_stamp(db)!=seal_stamp:
                raise ProjectionUnavailable(availability='building')
            _owned(db,binding,owner,token)
            _capacity(db,len(raw)+64)
            db.execute("UPDATE nlp_literal_publications SET state='sealed',publication_id=?,document=? WHERE account_ref=? AND snapshot_id=? AND owner=? AND state='building'",
                       (envelope.identity_digest(),raw,binding.account_ref,binding.snapshot_id,owner))
        started = False
        return envelope.identity_digest()
    except (ValueError, TypeError, KeyError, RecursionError, sqlite3.Error) as error:
        raise LiteralIntegrityError() from error
    finally:
        if started:
            with database.transaction() as db:
                db.execute("DELETE FROM nlp_literal_publications WHERE account_ref=? AND snapshot_id=? AND owner=? AND state='building'",
                           (binding.account_ref,binding.snapshot_id,owner))


def _owned(db,binding,owner,token):
    if _token(db,binding) != token or not db.execute(
            "SELECT 1 FROM nlp_literal_publications WHERE account_ref=? AND snapshot_id=? AND owner=? AND state='building' AND expires_at>?",
            (binding.account_ref,binding.snapshot_id,owner,time.time())).fetchone():
        raise ProjectionUnavailable(availability='building')


def checked_publication(db, binding, *, check=lambda: None):
    """Authenticate on the caller's borrowed connection, without opening a writer."""
    try:
        value = _current(db,binding)
        row = db.execute("SELECT publication_id,document FROM nlp_literal_publications WHERE account_ref=? AND snapshot_id=? AND state='sealed'",
                         (binding.account_ref,binding.snapshot_id)).fetchone()
        if row is None:
            raise LiteralUnavailable()
        envelope = n.LiteralPublicationEnvelope.model_validate_json(bytes(row[1]))
        if canonical_bytes(envelope) != bytes(row[1]) or envelope.identity_digest() != row[0]:
            raise LiteralIntegrityError()
        rows, hashes = _rows(db,'nlp_literal_publication_batches',binding.account_ref,'snapshot_id',binding.snapshot_id,check)
        expected, _, items = _material(db,binding,value,check,required=True)
        if envelope != expected or hashes != list(envelope.content_batch_sha256) or rows != items:
            raise LiteralIntegrityError()
        return LiteralPublicationData(envelope,
            tuple(n.LiteralNode.model_validate_json(encoded(r['node'])) for r in rows if r['kind']=='node'),
            tuple(n.OccurrenceMapping.model_validate_json(encoded(r['mapping'])) for r in rows if r['kind']=='mapping'),
            tuple(n.LiteralDisposition.model_validate_json(encoded(r['disposition'])) for r in rows if r['kind']=='disposition'),
            tuple(n.LiteralRecordState.model_validate_json(encoded(r['record'])) for r in rows if r['kind']=='record'))
    except (ValueError, TypeError, KeyError, RecursionError, sqlite3.Error) as error:
        raise LiteralIntegrityError() from error


def assert_publication(db, binding, publication_id, *, check=lambda: None):
    """Recheck exact sealed identity after the caller revalidates live sources."""
    check()
    try:
        value = _current(db, binding)
        row = db.execute("SELECT publication_id,document FROM nlp_literal_publications WHERE account_ref=? AND snapshot_id=? AND state='sealed'",
                         (binding.account_ref, binding.snapshot_id)).fetchone()
        if row is None:
            raise LiteralUnavailable()
        envelope = n.LiteralPublicationEnvelope.model_validate_json(bytes(row[1]))
        namespace = db.execute('SELECT namespace FROM nlp_literal_accounts WHERE account_ref=?', (binding.account_ref,)).fetchone()
        snapshot = value['snapshot']
        if (row[0] != publication_id or envelope.identity_digest() != publication_id
                or canonical_bytes(envelope) != bytes(row[1])
                or namespace is None or namespace[0] != envelope.account_namespace
                or envelope.profile_sha256 != read_profile(encoded(snapshot['profile'])).identity_digest()
                or envelope.publication.account_scope != binding.account_ref
                or envelope.publication.extraction_snapshot_sha256 != binding.snapshot_id
                or envelope.publication.normalization_policy != POLICY
                or envelope.publication.projection_policy != POLICY
                or envelope.publication.source_validity_sha256 != snapshot['source_validity_sha256']
                or envelope.publication.completion_revision != snapshot['completion_revision']
                or envelope.publication.availability_revision != snapshot['availability_revision']):
            raise LiteralIntegrityError()
    except (ValueError, TypeError, KeyError, RecursionError, sqlite3.Error) as error:
        raise LiteralIntegrityError() from error
    check()


def payload_tables(db):
    return TABLES if supported(db) else ()


def erase_page(db, account, *, account_wide):
    """Revoke publications first; retain enrollment namespace for scoped erasure."""
    if not supported(db):
        return False
    for table, key in (('nlp_literal_publications', 'snapshot_id'), ('nlp_literal_derivations', 'record_digest')):
        predicate = '' if account_wide or table == 'nlp_literal_publications' else (
            ' AND EXISTS(SELECT 1 FROM nlp_record_bindings b JOIN nlp_erasure_state e USING(account_ref) '
            'WHERE b.account_ref=nlp_literal_derivations.account_ref AND b.record_digest=nlp_literal_derivations.record_digest '
            'AND b.seen_epoch!=e.scan_epoch)')
        keys = [r[0] for r in db.execute('SELECT ' + key + ' FROM ' + table + ' WHERE account_ref=?' + predicate + ' LIMIT ?', (account,PAGE_ROWS))]
        if keys:
            db.executemany('DELETE FROM ' + table + ' WHERE account_ref=? AND ' + key + '=?', [(account,k) for k in keys])
            return True
    if account_wide:
        # Include orphan-only payload in the absence proof.
        for table in ('nlp_literal_publication_batches', 'nlp_literal_derivation_batches'):
            rows = db.execute('SELECT * FROM ' + table + ' WHERE account_ref=? LIMIT ?', (account,PAGE_ROWS)).fetchall()
            if rows:
                key = 'snapshot_id' if 'publication' in table else 'record_digest'
                db.executemany('DELETE FROM ' + table + ' WHERE account_ref=? AND ' + key + '=? AND ordinal=?',
                               [(account,r[1],r[2]) for r in rows])
                return True
        return bool(db.execute('DELETE FROM nlp_literal_accounts WHERE account_ref=?',(account,)).rowcount)
    return False
