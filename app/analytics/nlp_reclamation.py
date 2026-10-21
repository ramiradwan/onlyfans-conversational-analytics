"""Bounded orphan cleanup for immutable findings and their indexed owners."""
from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager

from app.analytics.nlp_findings_store import (
    FrozenFindings, checked_snapshot, check_deadline, encoded, VERIFICATION_SECONDS,
)
from app.analytics.validation_receipt import content_stamp
from app.nlp import v2
from app.nlp.profiles import AnalysisProfile

BATCH_SIZE = 64


@contextmanager
def _writing(database, account_ref, proof_candidate, install_transition):
    from app.analytics.enrichment_proof_transition import capture_transition, finish_transition
    transition=renewed=None
    with database.transaction() as db:
        if proof_candidate is not None:
            active=db.execute("SELECT * FROM projection_generations WHERE creator_account_id=? AND status='active'",
                (account_ref,)).fetchone()
            transition=capture_transition(db,active,proof_candidate(active))
        yield db
        if transition is not None:
            renewed=finish_transition(db,transition)
    if install_transition is not None:
        install_transition(transition,renewed)


def _current_input(value, state):
    return state is not None and (
        value['selection_epoch']==state['selection_epoch'] and
        value['snapshot']['completion_revision']==state['completion_revision'] and
        value['snapshot']['availability_revision']==state['availability_revision'] and
        value['snapshot']['source_validity_sha256']==state['inventory_digest'] and
        (not state['scan_identity'] or state['scan_identity']==encoded([
            value['snapshot']['canonical_revision'],value['canonical_content_digest']]).decode()))


def _supported(db):
    return db.execute('PRAGMA user_version').fetchone()[0] >= 27


def expire_generation_input(db, generation_id):
    """Revoke only the unbound current-input root when its report expires."""
    if _supported(db):
        db.execute('INSERT OR IGNORE INTO nlp_snapshot_expirations '
            'SELECT account_ref,snapshot_id FROM nlp_generation_refs WHERE generation_id=?',(generation_id,))


def _page(database, account_ref, proof_candidate, install_transition, deadline):
    """Authenticate one historical manifest page outside the writer lock."""
    with database.read() as db:
        db.execute('BEGIN')
        stamp = content_stamp(db)
        if stamp is None:
            raise ValueError('nlp_reclamation_guards_invalid')
        row = db.execute(
            'SELECT s.snapshot_id,COALESCE(i.next_ordinal,0) AS cursor '
            'FROM nlp_snapshots s LEFT JOIN nlp_snapshot_inventory i USING(account_ref,snapshot_id) '
            'WHERE s.account_ref=? AND COALESCE(i.complete,0)=0 ORDER BY s.snapshot_id LIMIT 1',
            (account_ref,)).fetchone()
        if row is None:
            return False
        check_deadline(deadline)
        binding = FrozenFindings(account_ref, row['snapshot_id'])
        value = checked_snapshot(db, binding, verify_records=False)
        keys = value['records']
        if (not isinstance(keys, list) or any(type(key) is not str for key in keys) or len(keys)>100_000 or
                len(set(keys)) != len(keys) or row['cursor']>len(keys)):
            raise ValueError('nlp_membership_invalid')
        cursor = row['cursor']
        members = db.execute('SELECT ordinal,record_digest FROM nlp_snapshot_members '
            'WHERE account_ref=? AND snapshot_id=? ORDER BY ordinal',
            (account_ref,binding.snapshot_id)).fetchall()
        if [(r[0],r[1]) for r in members] != list(enumerate(keys[:cursor])):
            raise ValueError('nlp_membership_invalid')
        page = keys[cursor:cursor+BATCH_SIZE]
        for key in page:
            check_deadline(deadline)
            record = db.execute('SELECT document FROM nlp_records WHERE account_ref=? AND record_digest=?',
                (account_ref,key)).fetchone()
            if record is None or hashlib.sha256(bytes(record[0])).hexdigest()!=key:
                raise ValueError('nlp_record_invalid')
            decoded = v2.read_record(bytes(record[0]))
            if decoded.source.account_scope != account_ref:
                raise ValueError('nlp_record_invalid')
            v2.validate_record(AnalysisProfile.model_validate_json(encoded(value['snapshot']['profile'])),
                decoded.source, bytes(record[0]))
    check_deadline(deadline)
    with _writing(database,account_ref,proof_candidate,install_transition) as db:
        if content_stamp(db) != stamp:
            return True
        db.execute('INSERT OR IGNORE INTO nlp_snapshot_inventory(account_ref,snapshot_id) VALUES(?,?)',
            (account_ref,binding.snapshot_id))
        db.executemany('INSERT INTO nlp_snapshot_members VALUES(?,?,?,?)',
            [(account_ref,binding.snapshot_id,cursor+n,key) for n,key in enumerate(page)])
        db.execute('UPDATE nlp_snapshot_inventory SET next_ordinal=?,complete=? '
            'WHERE account_ref=? AND snapshot_id=? AND complete=0 AND next_ordinal=?',
            (cursor+len(page),int(cursor+len(page)==len(keys)),account_ref,binding.snapshot_id,cursor))
    return True


def reclaim_orphans(database, account_ref, *, proof_candidate=None, install_transition=None):
    """Backfill one page, then reclaim at most one page of each content kind.

    Unsealed or inconsistent manifests prevent record deletion. All expensive
    authentication occurs in a reader; the content stamp fences its writer CAS.
    """
    deadline = time.monotonic()+VERIFICATION_SECONDS
    with database.read() as db:
        if not _supported(db):
            return 0
        if not db.execute('SELECT 1 FROM nlp_snapshots WHERE account_ref=? LIMIT 1',(account_ref,)).fetchone() and not db.execute(
                'SELECT 1 FROM nlp_records WHERE account_ref=? LIMIT 1',(account_ref,)).fetchone():
            return 0
    _page(database,account_ref,proof_candidate,install_transition,deadline)
    with database.read() as db:
        db.execute('BEGIN')
        stamp = content_stamp(db)
        if stamp is None:
            raise ValueError('nlp_reclamation_guards_invalid')
        snapshots=[]
        ready=True
        state=db.execute('SELECT * FROM nlp_accounts WHERE account_ref=?',(account_ref,)).fetchone()
        cursor_row=db.execute('SELECT record_cursor FROM nlp_reclamation_cursor WHERE account_ref=?',(account_ref,)).fetchone()
        cursor=cursor_row[0] if cursor_row else ''
        records=[row[0] for row in db.execute('SELECT record_digest FROM nlp_records '
            'WHERE account_ref=? AND record_digest>? ORDER BY record_digest LIMIT ?',
            (account_ref,cursor,BATCH_SIZE))]
        if not records and cursor:
            records=[row[0] for row in db.execute('SELECT record_digest FROM nlp_records '
                'WHERE account_ref=? ORDER BY record_digest LIMIT ?',(account_ref,BATCH_SIZE))]
        for row in db.execute('SELECT s.snapshot_id,i.next_ordinal,i.complete,i.retired_ordinal FROM nlp_snapshots s '
                'LEFT JOIN nlp_snapshot_inventory i USING(account_ref,snapshot_id) WHERE s.account_ref=?',
                (account_ref,)):
            check_deadline(deadline)
            if row['complete'] not in (1,2):
                ready=False
                continue
            value=checked_snapshot(db,FrozenFindings(account_ref,row['snapshot_id']),verify_records=False)
            members=[]
            for member in db.execute('SELECT ordinal,record_digest FROM nlp_snapshot_members '
                    'WHERE account_ref=? AND snapshot_id=? ORDER BY ordinal',(account_ref,row['snapshot_id'])):
                check_deadline(deadline)
                members.append(tuple(member))
            retired=row['retired_ordinal']
            if (members!=list(enumerate(value['records'][retired:],start=retired)) or
                    row['next_ordinal']!=len(value['records']) or
                    (row['complete']==1 and retired!=0)):
                raise ValueError('nlp_membership_invalid')
            if row['complete']==2:
                ready=False
            expired=db.execute('SELECT 1 FROM nlp_snapshot_expirations WHERE account_ref=? AND snapshot_id=?',
                (account_ref,row['snapshot_id'])).fetchone()
            if not snapshots and (expired or not _current_input(value,state)) and not db.execute('SELECT 1 FROM nlp_generation_refs '
                    'WHERE account_ref=? AND snapshot_id=? LIMIT 1',(account_ref,row['snapshot_id'])).fetchone():
                snapshots.append((row['snapshot_id'],retired,len(value['records'])))
    deleted=0
    check_deadline(deadline)
    with _writing(database,account_ref,proof_candidate,install_transition) as db:
        if content_stamp(db)!=stamp:
            return 0
        live=db.execute('SELECT * FROM nlp_accounts WHERE account_ref=?',(account_ref,)).fetchone()
        if (tuple(live) if live else None)!=(tuple(state) if state else None):
            return 0
        for key,retired,total in snapshots:
            if db.execute('SELECT 1 FROM nlp_generation_refs WHERE account_ref=? AND snapshot_id=? LIMIT 1',
                    (account_ref,key)).fetchone():
                continue
            end=min(total,retired+BATCH_SIZE)
            db.execute('UPDATE nlp_snapshot_inventory SET complete=2,retired_ordinal=? '
                'WHERE account_ref=? AND snapshot_id=?',(end,account_ref,key))
            db.execute('DELETE FROM nlp_snapshot_members WHERE account_ref=? AND snapshot_id=? AND ordinal>=? AND ordinal<?',
                (account_ref,key,retired,end))
            if end==total:
                deleted+=db.execute('DELETE FROM nlp_snapshots WHERE account_ref=? AND snapshot_id=?',
                    (account_ref,key)).rowcount
            else:
                ready=False
        if ready:
            # Jobs need not be runnable: pause and temporary readiness loss retain
            # completed findings. Stale selection/source jobs do not own records.
            for key in records:
                deleted+=db.execute('DELETE FROM nlp_records AS r WHERE account_ref=? AND record_digest=? '
                'AND NOT EXISTS(SELECT 1 FROM nlp_snapshot_members m WHERE m.account_ref=r.account_ref AND m.record_digest=r.record_digest) '
                'AND NOT EXISTS(SELECT 1 FROM nlp_jobs j JOIN nlp_accounts a USING(account_ref) '
                'JOIN nlp_sources s USING(account_ref,source_ref) WHERE j.account_ref=r.account_ref '
                "AND j.record_digest=r.record_digest AND j.state='done' AND j.selection_epoch=a.selection_epoch "
                'AND j.source_version=s.source_version)',(account_ref,key)).rowcount
            db.execute('INSERT OR IGNORE INTO nlp_reclamation_cursor(account_ref) VALUES(?)',(account_ref,))
            db.execute('UPDATE nlp_reclamation_cursor SET record_cursor=? WHERE account_ref=? AND record_cursor=?',
                (records[-1] if records else '',account_ref,cursor))
    return deleted
