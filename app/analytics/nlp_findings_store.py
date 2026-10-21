"""Bounded optional findings, claims and immutable publication inputs."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from uuid import uuid4

from app.analytics.identity import CanonicalIdentity
from app.analytics.evidence_contracts import EvidenceMessage
from app.analytics.opaque_refs import account_ref, participant_ref
from app.analytics.nlp_record_validation import validate_source_bound_record
from app.nlp import v2
from app.nlp.contracts import SourceBinding
from app.nlp.profiles import AnalysisProfile, TASK_CONTRACTS

MAX_SOURCES = 4096
MAX_PENDING = 256
PAGE_SIZE = 64
MAX_JOB_SECONDS = 120
MAX_RECORD_STORAGE = 256 * 1024 * 1024
MAX_SNAPSHOT_STORAGE = 64 * 1024 * 1024
MAX_LOCATORS = 100_000
MAX_ACCOUNTS = 16
VERIFICATION_SECONDS = 30


def check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise ValueError('nlp_findings_deadline')


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def source_binding(source):
    try:
        if type(source) is not EvidenceMessage:
            raise ValueError()
        source=EvidenceMessage.model_validate(source.model_dump(mode='python',warnings=False))
        json.dumps(source.model_dump(mode='json'),ensure_ascii=False).encode('utf-8',errors='strict')
        ref = source.reference()
        return SourceBinding(account_scope=ref.account_ref, source_ref=ref.message_ref,
            source_version=ref.source_version_digest.removeprefix('sha256:'),
            speaker_ref=participant_ref(source.account_id, source.sender_id))
    except Exception:
        raise ValueError('nlp_source_invalid') from None


def inventory_digest(db, ref, deadline=None):
    deadline=time.monotonic()+VERIFICATION_SECONDS if deadline is None else deadline
    result = hashlib.sha256(b'nlp-source-inventory.v1\0')
    for row in db.execute('SELECT source_ref,source_version,speaker_ref FROM nlp_sources WHERE account_ref=? ORDER BY source_ref',(ref,)):
        check_deadline(deadline)
        raw = encoded(tuple(row))
        result.update(len(raw).to_bytes(8,'big'))
        result.update(raw)
    return result.hexdigest()


def effective_config(base_config, snapshot_id):
    return 'sha256:' + digest({'schema':'analytics-findings-input.v1',
        'base_config':base_config, 'snapshot':snapshot_id})


@dataclass(frozen=True)
class FrozenFindings:
    account_ref: str
    snapshot_id: str


@dataclass(frozen=True)
class Claim:
    account_ref: str
    source_ref: str
    source_version: str
    selection_epoch: int
    admission_epoch: int
    owner: str
    conversation_id: str
    message_id: str
    expires_at: float


def checked_snapshot(db, binding, *, verify_records=True, deadline=None):
    """Verify immutable stored bytes, without granting live source access."""
    deadline = min(
        deadline if deadline is not None else float("inf"),
        time.monotonic() + VERIFICATION_SECONDS,
    )
    row = db.execute('SELECT document FROM nlp_snapshots WHERE account_ref=? AND snapshot_id=?',
        (binding.account_ref, binding.snapshot_id)).fetchone()
    if row is None or hashlib.sha256(bytes(row[0])).hexdigest() != binding.snapshot_id:
        raise ValueError('nlp_snapshot_invalid')
    value = json.loads(row[0])
    snapshot = v2.decode_snapshot(encoded(value['snapshot']))
    if snapshot.account_scope != binding.account_ref:
        raise ValueError('nlp_snapshot_invalid')
    for task in snapshot.tasks:
        if digest(value['coverage'][task.task]) != task.coverage_sha256:
            raise ValueError('nlp_snapshot_coverage_invalid')
    seen=set()
    for record_digest in value['records'] if verify_records else ():
        check_deadline(deadline)
        record = db.execute('SELECT document FROM nlp_records WHERE account_ref=? AND record_digest=?',
            (binding.account_ref, record_digest)).fetchone()
        if record is None or hashlib.sha256(bytes(record[0])).hexdigest() != record_digest:
            raise ValueError('nlp_record_invalid')
        decoded=v2.read_record(bytes(record[0]))
        if decoded.source.account_scope!=binding.account_ref or decoded.source.source_ref in seen:
            raise ValueError('nlp_snapshot_source_invalid')
        seen.add(decoded.source.source_ref)
        v2.validate_record(snapshot.profile, decoded.source, bytes(record[0]))
    return value


def require_current_snapshot(
    db, binding, canonical=None, *, verify_records=True, deadline=None
):
    """Fence local findings revisions; callers separately admit live sources."""
    require_erasure_idle(db, binding.account_ref)
    value = checked_snapshot(
        db, binding, verify_records=verify_records, deadline=deadline
    )
    state = db.execute('SELECT * FROM nlp_accounts WHERE account_ref=?', (binding.account_ref,)).fetchone()
    snapshot = value['snapshot']
    if (state is None or state['mode'] in ('withdrawn','removed')
        or state['selection_epoch'] != value['selection_epoch']
        or state['completion_revision'] != snapshot['completion_revision']
        or state['availability_revision'] != snapshot['availability_revision']
        or state['inventory_digest'] != snapshot['source_validity_sha256']
        or AnalysisProfile.model_validate_json(state['profile_json']).identity_digest() !=
            AnalysisProfile.model_validate_json(encoded(snapshot['profile'])).identity_digest()

        or (canonical is not None and (canonical.revision != snapshot['canonical_revision']
            or canonical.content_digest != value['canonical_content_digest']))):
        raise ValueError('nlp_snapshot_stale')
    return value


def bind_generation(db, generation_id, binding, base_config, canonical):
    require_current_snapshot(db, binding, canonical, verify_records=False)
    db.execute('INSERT INTO nlp_generation_refs VALUES(?,?,?,?)',
        (generation_id,binding.account_ref,binding.snapshot_id,base_config))


def check_generation(
    db, generation, *, current=False, verify_records=True, deadline=None
):
    if db.execute('PRAGMA user_version').fetchone()[0] < 26:
        if '+nlp.snapshot.v1' in generation['pipeline_revision']:
            raise ValueError('nlp_generation_schema_missing')
        return
    row = db.execute('SELECT * FROM nlp_generation_refs WHERE generation_id=?',
        (generation['generation_id'],)).fetchone()
    if row is None:
        if '+nlp.snapshot.v1' in generation['pipeline_revision']:
            raise ValueError('nlp_generation_binding_missing')
        return
    if '+nlp.snapshot.v1' not in generation['pipeline_revision']:
        raise ValueError('nlp_generation_binding_unexpected')
    binding = FrozenFindings(row['account_ref'],row['snapshot_id'])
    if (row['account_ref'] != generation['creator_account_id'] or
        effective_config(row['base_config'],row['snapshot_id']) != generation['pipeline_config_digest']):
        raise ValueError('nlp_generation_binding_invalid')
    canonical = CanonicalIdentity(generation['canonical_revision'],generation['canonical_content_digest'])
    value = (
        require_current_snapshot(
            db, binding, canonical, verify_records=verify_records, deadline=deadline
        )
        if current
        else checked_snapshot(
            db, binding, verify_records=verify_records, deadline=deadline
        )
    )
    if (value['snapshot']['canonical_revision'] != canonical.revision or
            value['canonical_content_digest'] != canonical.content_digest):
        raise ValueError('nlp_generation_source_invalid')


def require_erasure_idle(db, ref):
    if (
        db.execute("PRAGMA user_version").fetchone()[0] >= 28
        and db.execute(
            "SELECT 1 FROM nlp_erasure_state WHERE account_ref=? AND phase!='complete'",
            (ref,),
        ).fetchone()
    ):
        raise ValueError("nlp_erasure_pending")


class FindingsStore:
    def __init__(self, database, *, clock=time.time):
        self.database, self.clock = database, clock

    def select(self, account_id, profile, *, mode='enabled'):
        if mode not in ('enabled','paused','withdrawn','removed'):
            raise ValueError('nlp_mode_invalid')
        profile = AnalysisProfile.model_validate_json(profile.model_dump_json())
        ref = account_ref(account_id)
        with self.database.transaction() as db:
            require_erasure_idle(db, ref)
            state = db.execute('SELECT * FROM nlp_accounts WHERE account_ref=?',(ref,)).fetchone()
            ledger = db.execute(
                "SELECT selection_epoch FROM nlp_epoch_ledger WHERE account_ref=?",
                (ref,),
            ).fetchone()
            epoch = (
                max(
                    state["selection_epoch"] if state else 0, ledger[0] if ledger else 0
                )
                + 1
            )
            db.execute(
                "INSERT INTO nlp_epoch_ledger VALUES(?,?) ON CONFLICT(account_ref) DO UPDATE SET selection_epoch=excluded.selection_epoch",
                (ref, epoch),
            )
            if state is None:
                if db.execute('SELECT count(*) FROM nlp_accounts').fetchone()[0] >= MAX_ACCOUNTS:
                    raise ValueError('nlp_account_limit')
                db.execute(
                    "INSERT INTO nlp_accounts(account_ref,profile_json,selection_epoch,mode) VALUES(?,?,?,?)",
                    (ref, profile.model_dump_json(), epoch, mode),
                )
            else:
                db.execute(
                    "UPDATE nlp_accounts SET profile_json=?,selection_epoch=?,"
                    "admission_epoch=admission_epoch+1,availability_revision=availability_revision+1,"
                    "notification_revision=notification_revision+1,mode=? WHERE account_ref=?",
                    (profile.model_dump_json(), epoch, mode, ref),
                )
                db.execute('DELETE FROM nlp_jobs WHERE account_ref=?',(ref,))

    def mode(self, account_id, mode):
        if mode not in ('enabled','paused','withdrawn','removed'):
            raise ValueError('nlp_mode_invalid')
        with self.database.transaction() as db:
            ref = account_ref(account_id)
            require_erasure_idle(db, ref)
            state = db.execute('SELECT mode FROM nlp_accounts WHERE account_ref=?',(ref,)).fetchone()
            if state is None:
                raise ValueError('nlp_account_missing')
            if state[0] == mode:
                return
            invalidates = mode in ('withdrawn','removed') or state[0] in ('withdrawn','removed')
            db.execute('UPDATE nlp_accounts SET mode=?,admission_epoch=admission_epoch+1,'
                'availability_revision=availability_revision+?,notification_revision=notification_revision+? '
                'WHERE account_ref=?',(mode,int(invalidates),int(invalidates),ref))
            db.execute("UPDATE nlp_jobs SET state='pending',owner=NULL,expires_at=NULL WHERE account_ref=? AND state='running'",(ref,))

    def state(self, account_id):
        with self.database.read() as db:
            row = db.execute('SELECT * FROM nlp_accounts WHERE account_ref=?',(account_ref(account_id),)).fetchone()
            if row is None:
                raise ValueError('nlp_account_missing')
            return dict(row)

    def invalidate_source(self, account_id, source_ref):
        ref=account_ref(account_id)
        with self.database.transaction() as db:
            db.execute('DELETE FROM nlp_jobs WHERE account_ref=? AND source_ref=?',(ref,source_ref))
            changed=db.execute('DELETE FROM nlp_sources WHERE account_ref=? AND source_ref=?',(ref,source_ref)).rowcount
            if changed:
                db.execute('UPDATE nlp_accounts SET inventory_digest=?,availability_revision=availability_revision+1,'
                    'notification_revision=notification_revision+1 WHERE account_ref=?',(inventory_digest(db,ref),ref))

    def retry(self, account_id, source_ref):
        """An explicit retry invalidates current availability, never old manifests."""
        ref=account_ref(account_id)
        with self.database.transaction() as db:
            require_erasure_idle(db, ref)
            state=db.execute('SELECT mode FROM nlp_accounts WHERE account_ref=?',(ref,)).fetchone()
            if state is None or state[0]!='enabled':
                raise ValueError('nlp_retry_unavailable')
            pending=db.execute("SELECT count(*) FROM nlp_jobs WHERE account_ref=? AND state IN ('pending','running')",(ref,)).fetchone()[0]
            if pending>=MAX_PENDING:
                raise ValueError('nlp_queue_full')
            changed=db.execute("UPDATE nlp_jobs SET state='pending',record_digest=NULL,owner=NULL,expires_at=NULL,recoveries=0,terminal_reason='' "
                "WHERE account_ref=? AND source_ref=? AND state IN ('done','abandoned')",(ref,source_ref)).rowcount
            if changed:
                db.execute('UPDATE nlp_accounts SET availability_revision=availability_revision+1,'
                    'notification_revision=notification_revision+1 WHERE account_ref=?',(ref,))
            return bool(changed)

    def abandon(self, claim, reason):
        if reason not in ('unsupported_input','interrupted','record_limit'):
            raise ValueError('nlp_terminal_reason_invalid')
        with self.database.transaction() as db:
            if not self._valid_claim(db,claim):
                raise ValueError('nlp_claim_stale')
            db.execute("UPDATE nlp_jobs SET state='abandoned',terminal_reason=?,expires_at=NULL WHERE account_ref=? AND source_ref=?",
                (reason,claim.account_ref,claim.source_ref))
            db.execute('UPDATE nlp_accounts SET availability_revision=availability_revision+1,'
                'notification_revision=notification_revision+1 WHERE account_ref=?',(claim.account_ref,))

    def reconcile_page(self, account_id, canonical, sources, cursor, *, complete):
        """Commit one bounded page; unfinished scans never claim full coverage."""
        if len(sources) > PAGE_SIZE:
            raise ValueError('nlp_page_limit')
        ref = account_ref(account_id)
        identity = encoded([canonical.revision,canonical.content_digest]).decode()
        with self.database.transaction() as db:
            require_erasure_idle(db, ref)
            state = db.execute('SELECT * FROM nlp_accounts WHERE account_ref=?',(ref,)).fetchone()
            if state is None:
                raise ValueError('nlp_account_missing')
            if state['scan_identity'] != identity:
                if cursor is not None:
                    raise ValueError('nlp_scan_restart_required')
                db.execute('UPDATE nlp_accounts SET scan_identity=?,scan_epoch=scan_epoch+1,scan_complete=0,'
                    "scan_cursor='[]',availability_revision=availability_revision+1,notification_revision=notification_revision+1 WHERE account_ref=?",(identity,ref))
                state = db.execute('SELECT * FROM nlp_accounts WHERE account_ref=?',(ref,)).fetchone()
            expected = json.loads(state['scan_cursor'])
            if list(cursor or ()) != expected:
                raise ValueError('nlp_scan_cursor_stale')
            last = cursor
            for source,lane in sources:
                binding = source_binding(source)
                location = [source.location.conversation_id,source.location.message_id]
                if binding.account_scope != ref or lane not in ('live','backfill') or (last and location[1] <= last[1]):
                    raise ValueError('nlp_scan_page_invalid')
                old = db.execute('SELECT source_version FROM nlp_sources WHERE account_ref=? AND source_ref=?',(ref,binding.source_ref)).fetchone()
                if not old and db.execute('SELECT count(*) FROM nlp_sources').fetchone()[0] >= MAX_LOCATORS:
                    raise ValueError('nlp_locator_storage_limit')
                if old and old[0] != binding.source_version:
                    db.execute('DELETE FROM nlp_jobs WHERE account_ref=? AND source_ref=?',(ref,binding.source_ref))
                db.execute(
                    "INSERT INTO nlp_sources(account_ref,source_ref,source_version,speaker_ref,conversation_id,message_id,seen_epoch,lane) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account_ref,source_ref) "
                    "DO UPDATE SET source_version=excluded.source_version,speaker_ref=excluded.speaker_ref,"
                    "conversation_id=excluded.conversation_id,message_id=excluded.message_id,seen_epoch=excluded.seen_epoch,lane=excluded.lane",
                    (
                        ref,
                        binding.source_ref,
                        binding.source_version,
                        binding.speaker_ref,
                        *location,
                        state["scan_epoch"],
                        lane,
                    ),
                )
                last = location
            # The caller's cursor includes excluded/expired rows too.
            return state['scan_epoch'],state['scan_cursor']

    def finish_page(self, account_id, canonical, epoch, cursor, *, complete, expected_cursor):
        ref = account_ref(account_id)
        with self.database.transaction() as db:
            require_erasure_idle(db, ref)
            state = db.execute('SELECT * FROM nlp_accounts WHERE account_ref=?',(ref,)).fetchone()
            identity = encoded([canonical.revision,canonical.content_digest]).decode()
            if state['scan_epoch'] != epoch or state['scan_identity'] != identity or state['scan_cursor'] != expected_cursor:
                raise ValueError('nlp_scan_stale')
            if complete:
                db.execute('DELETE FROM nlp_jobs WHERE account_ref=? AND NOT EXISTS('
                    'SELECT 1 FROM nlp_sources s WHERE s.account_ref=nlp_jobs.account_ref '
                    'AND s.source_ref=nlp_jobs.source_ref AND s.seen_epoch=?)',(ref,epoch))
                db.execute('DELETE FROM nlp_sources WHERE account_ref=? AND seen_epoch!=?',(ref,epoch))
            inventory = inventory_digest(db,ref)
            changed = inventory != state['inventory_digest'] or bool(complete) != bool(state['scan_complete'])
            db.execute('UPDATE nlp_accounts SET scan_cursor=?,scan_complete=?,inventory_digest=?,'
                'availability_revision=availability_revision+?,notification_revision=notification_revision+? WHERE account_ref=?',
                (encoded(cursor or []).decode(),int(complete),inventory,int(changed),int(changed),ref))
            self._fill(db,ref)

    def _fill(self, db, ref):
        try:
            require_erasure_idle(db, ref)
        except ValueError:
            return
        state = db.execute('SELECT * FROM nlp_accounts WHERE account_ref=?',(ref,)).fetchone()
        if state is None:
            raise ValueError('nlp_account_missing')
        pending = db.execute("SELECT count(*) FROM nlp_jobs WHERE account_ref=? AND state IN ('pending','running')",(ref,)).fetchone()[0]
        for lane in ('live','backfill'):
            waiting=db.execute('SELECT s.* FROM nlp_sources s WHERE account_ref=? AND seen_epoch=? AND lane=? '
                'AND NOT EXISTS(SELECT 1 FROM nlp_jobs j WHERE j.account_ref=s.account_ref AND j.source_ref=s.source_ref) '
                'ORDER BY source_ref LIMIT 1',(ref,state['scan_epoch'],lane)).fetchone()
            represented=db.execute("SELECT 1 FROM nlp_jobs WHERE account_ref=? AND lane=? AND state IN ('pending','running') LIMIT 1",(ref,lane)).fetchone()
            if waiting is not None and represented is None and pending>=MAX_PENDING:
                victim=db.execute("SELECT source_ref FROM nlp_jobs WHERE account_ref=? AND lane!=? AND state='pending' ORDER BY source_ref DESC LIMIT 1",(ref,lane)).fetchone()
                if victim is not None:
                    db.execute('DELETE FROM nlp_jobs WHERE account_ref=? AND source_ref=?',(ref,victim[0]))
                    db.execute("INSERT INTO nlp_jobs(account_ref,source_ref,source_version,selection_epoch,admission_epoch,state,lane) VALUES(?,?,?,?,?,'pending',?)",
                        (ref,waiting['source_ref'],waiting['source_version'],state['selection_epoch'],state['admission_epoch'],lane))
        missing = db.execute('SELECT s.* FROM nlp_sources s WHERE account_ref=? AND seen_epoch=? AND NOT EXISTS('
            'SELECT 1 FROM nlp_jobs j WHERE j.account_ref=s.account_ref AND j.source_ref=s.source_ref) '
            "ORDER BY (lane='live') DESC,source_ref LIMIT ?",(ref,state['scan_epoch'],max(0,MAX_PENDING-pending))).fetchall()
        db.executemany("INSERT INTO nlp_jobs(account_ref,source_ref,source_version,selection_epoch,admission_epoch,state,lane) VALUES(?,?,?,?,?,'pending',?)",
            [(ref,r['source_ref'],r['source_version'],state['selection_epoch'],state['admission_epoch'],r['lane']) for r in missing])

    def claim(self, account_id, *, deadline_seconds=MAX_JOB_SECONDS):
        if not 1 <= deadline_seconds <= MAX_JOB_SECONDS:
            raise ValueError('nlp_deadline_invalid')
        ref, now = account_ref(account_id), self.clock()
        with self.database.transaction() as db:
            try:
                require_erasure_idle(db, ref)
            except ValueError:
                return None
            expired = db.execute("SELECT * FROM nlp_jobs WHERE state='running' AND expires_at<=?",(now,)).fetchall()
            for row in expired:
                db.execute("UPDATE nlp_jobs SET state=?,terminal_reason=?,recoveries=recoveries+1,owner=NULL,expires_at=NULL WHERE account_ref=? AND source_ref=?",
                    ('pending' if row['recoveries'] == 0 else 'abandoned',
                     '' if row['recoveries']==0 else 'interrupted',row['account_ref'],row['source_ref']))
            for ref_changed in {r['account_ref'] for r in expired}:
                db.execute('UPDATE nlp_accounts SET availability_revision=availability_revision+1,notification_revision=notification_revision+1 WHERE account_ref=?',(ref_changed,))
            self._fill(db,ref)
            if db.execute("SELECT 1 FROM nlp_jobs WHERE state='running'").fetchone():
                return None
            state = db.execute('SELECT * FROM nlp_accounts WHERE account_ref=?',(ref,)).fetchone()
            if state is None or state['mode'] != 'enabled':
                return None
            lane = 'backfill' if state['live_claims'] >= 3 else 'live'
            row = db.execute("SELECT j.*,s.conversation_id,s.message_id FROM nlp_jobs j JOIN nlp_sources s USING(account_ref,source_ref) "
                "WHERE j.account_ref=? AND j.state='pending' ORDER BY (j.lane=? ) DESC,j.source_ref LIMIT 1",(ref,lane)).fetchone()
            if row is None:
                return None
            owner = uuid4().hex
            db.execute("UPDATE nlp_jobs SET state='running',owner=?,expires_at=?,admission_epoch=? WHERE account_ref=? AND source_ref=?",
                (owner,now+deadline_seconds,state['admission_epoch'],ref,row['source_ref']))
            db.execute('UPDATE nlp_accounts SET live_claims=? WHERE account_ref=?',
                (state['live_claims']+1 if row['lane']=='live' else 0,ref))
            return Claim(ref,row['source_ref'],row['source_version'],state['selection_epoch'],state['admission_epoch'],
                owner,row['conversation_id'],row['message_id'],now+deadline_seconds)

    def valid_claim(self, claim):
        with self.database.read() as db:
            return self._valid_claim(db,claim)

    def release(self, claim):
        """Release a cancelled, still-live claim without changing saved findings."""
        with self.database.transaction() as db:
            db.execute("UPDATE nlp_jobs SET state='pending',owner=NULL,expires_at=NULL "
                "WHERE account_ref=? AND source_ref=? AND state='running' AND owner=? AND expires_at>?",
                (claim.account_ref,claim.source_ref,claim.owner,self.clock()))

    def _valid_claim(self, db, claim):
        try:
            require_erasure_idle(db, claim.account_ref)
        except ValueError:
            return False
        row = db.execute("SELECT j.*,a.mode,a.selection_epoch AS epoch,a.admission_epoch AS admission FROM nlp_jobs j "
            'JOIN nlp_accounts a USING(account_ref) WHERE j.account_ref=? AND j.source_ref=?',
            (claim.account_ref,claim.source_ref)).fetchone()
        return bool(row and row['state']=='running' and row['owner']==claim.owner and row['expires_at']>self.clock()
            and row['mode']=='enabled' and row['epoch']==claim.selection_epoch and row['admission']==claim.admission_epoch
            and row['source_version']==claim.source_version)

    def complete(self, claim, profile, source, frame):
        record = validate_source_bound_record(profile,source,frame)
        if (record.source.source_ref != claim.source_ref or record.source.source_version != claim.source_version
                or record.source.account_scope != claim.account_ref):
            raise ValueError('nlp_claim_source_mismatch')
        raw = record.model_dump_json().encode()
        key = hashlib.sha256(raw).hexdigest()
        with self.database.transaction() as db:
            row = db.execute('SELECT * FROM nlp_jobs WHERE account_ref=? AND source_ref=?',
                (claim.account_ref,claim.source_ref)).fetchone()
            if (row and row['state']=='done' and row['record_digest']==key and row['owner']==claim.owner
                    and row['selection_epoch']==claim.selection_epoch):
                return False
            if not self._valid_claim(db,claim):
                raise ValueError('nlp_claim_stale')
            state = db.execute('SELECT profile_json FROM nlp_accounts WHERE account_ref=?',(claim.account_ref,)).fetchone()
            if AnalysisProfile.model_validate_json(state[0]).identity_digest()!=profile.identity_digest():
                raise ValueError('nlp_profile_stale')
            if db.execute('SELECT COALESCE(sum(length(document)),0) FROM nlp_records').fetchone()[0] + len(raw) > MAX_RECORD_STORAGE:
                raise ValueError('nlp_record_storage_limit')
            db.execute('INSERT OR IGNORE INTO nlp_records VALUES(?,?,?)',(claim.account_ref,key,raw))
            db.execute("UPDATE nlp_jobs SET state='done',record_digest=?,expires_at=NULL WHERE account_ref=? AND source_ref=?",
                (key,claim.account_ref,claim.source_ref))
            db.execute('UPDATE nlp_accounts SET completion_revision=completion_revision+1,'
                'notification_revision=notification_revision+1 WHERE account_ref=?',(claim.account_ref,))
            self._fill(db,claim.account_ref)
        return True

    def freeze(self, account_id, canonical):
        ref = account_ref(account_id)
        deadline=time.monotonic()+VERIFICATION_SECONDS
        with self.database.read() as db:
            db.execute('BEGIN')
            require_erasure_idle(db, ref)
            state = db.execute('SELECT * FROM nlp_accounts WHERE account_ref=?',(ref,)).fetchone()
            if state is None or state['mode'] in ('withdrawn','removed'):
                raise ValueError('nlp_findings_unavailable')
            profile = AnalysisProfile.model_validate_json(state['profile_json'])
            eligible = db.execute('SELECT count(*) FROM nlp_sources WHERE account_ref=?',(ref,)).fetchone()[0]
            if inventory_digest(db,ref,deadline) != state['inventory_digest']:
                raise ValueError('nlp_inventory_invalid')
            rows = db.execute("SELECT r.record_digest,r.document,j.source_ref,j.source_version,s.speaker_ref "
                "FROM nlp_jobs j JOIN nlp_sources s USING(account_ref,source_ref) JOIN nlp_records r USING(account_ref,record_digest) "
                "WHERE j.account_ref=? AND j.state='done' AND j.source_version=s.source_version AND j.selection_epoch=? "
                "ORDER BY j.source_ref",(ref,state['selection_epoch']))
            hashes={b.task:hashlib.sha256(b'nlp-task-results.v1\0') for b in profile.bindings}
            assessed={b.task:0 for b in profile.bindings}
            keys=[]
            for row in rows:
                check_deadline(deadline)
                if len(keys)>=100_000:
                    raise ValueError('nlp_snapshot_record_limit')
                raw=bytes(row['document'])
                if hashlib.sha256(raw).hexdigest()!=row['record_digest']:
                    raise ValueError('nlp_record_invalid')
                record=v2.read_record(raw)
                expected=SourceBinding(account_scope=ref,source_ref=row['source_ref'],
                    source_version=row['source_version'],speaker_ref=row['speaker_ref'])
                v2.validate_record(profile,expected,raw)
                keys.append(row['record_digest'])
                for task in record.tasks:
                    raw_task=encoded(task.result.model_dump(mode='json'))
                    hashes[task.result.payload.task].update(len(raw_task).to_bytes(8,'big'))
                    hashes[task.result.payload.task].update(raw_task)
                    assessed[task.result.payload.task]+=int(task.result.payload.outcome.startswith('assessed_'))
            abandoned=db.execute("SELECT count(*) FROM nlp_jobs WHERE account_ref=? AND state='abandoned'",(ref,)).fetchone()[0]
            tasks=[]
            coverage={}
            for binding in profile.bindings:
                missing=eligible-len(keys)
                availability=('partial' if assessed[binding.task]<len(keys) or abandoned else
                    'pending' if missing or not state['scan_complete'] else 'current')
                coverage[binding.task]={'eligible':eligible,'completed':len(keys),'assessed':assessed[binding.task],
                    'missing':missing,'scan_complete':bool(state['scan_complete']),
                    'terminal_reasons':{r[0]:r[1] for r in db.execute("SELECT terminal_reason,count(*) FROM nlp_jobs WHERE account_ref=? AND state='abandoned' GROUP BY terminal_reason",(ref,))}}
                tasks.append(v2.TaskSnapshot(task=binding.task,task_contract=TASK_CONTRACTS[binding.task],
                    component_sha256=profile.component(binding.component_id).identity_digest(),availability=availability,
                    completion_revision=state['completion_revision'],availability_revision=state['availability_revision'],
                    findings_sha256=hashes[binding.task].hexdigest(),
                    coverage_sha256=digest(coverage[binding.task])))
            snapshot=v2.EnrichmentSnapshot(account_scope=ref,canonical_revision=canonical.revision,
                completion_revision=state['completion_revision'],availability_revision=state['availability_revision'],
                profile=profile,source_validity_sha256=state['inventory_digest'],tasks=tuple(tasks))
            document=encoded({'snapshot':snapshot.model_dump(mode='json'),'selection_epoch':state['selection_epoch'],
                'canonical_content_digest':canonical.content_digest,'records':keys,'coverage':coverage})
            key=hashlib.sha256(document).hexdigest()
        with self.database.transaction() as db:
            require_erasure_idle(db, ref)
            current=db.execute('SELECT * FROM nlp_accounts WHERE account_ref=?',(ref,)).fetchone()
            fields=('selection_epoch','completion_revision','availability_revision','inventory_digest','scan_complete')
            if current is None or current['mode'] in ('withdrawn','removed') or any(current[k]!=state[k] for k in fields):
                raise ValueError('nlp_snapshot_stale')
            exists=db.execute('SELECT 1 FROM nlp_snapshots WHERE account_ref=? AND snapshot_id=?',(ref,key)).fetchone()
            if not exists and db.execute('SELECT COALESCE(sum(length(document)),0) FROM nlp_snapshots').fetchone()[0] + len(document) > MAX_SNAPSHOT_STORAGE:
                raise ValueError('nlp_snapshot_storage_limit')
            db.execute('INSERT OR IGNORE INTO nlp_snapshots VALUES(?,?,?)',(ref,key,document))
        return FrozenFindings(ref,key)

    def acknowledge(self, account_id, notification_revision):
        with self.database.transaction() as db:
            db.execute('UPDATE nlp_accounts SET acknowledged_revision=? WHERE account_ref=? AND notification_revision=?',
                (notification_revision,account_ref(account_id),notification_revision))
