"""Bounded exact-record replay, without worker or dependency admission."""
from __future__ import annotations

import hashlib

from app.analytics.nlp_findings_store import (
    PAGE_SIZE, MAX_LOCATORS, MAX_RECORD_STORAGE, account_ref,
    inventory_digest, require_erasure_idle,
)
from app.analytics.nlp_literals import capture_record, supported, LiteralUnavailable
from app.analytics.nlp_record_validation import validate_source_bound_record
from app.models.knowledge_contributions import SourceMemberSpec
from app.models.knowledge_contributions import record_digest
from app.nlp.profiles import read_profile
from app.persistence.contributions import _load


def replay_contribution(repository, findings, contribution_id, *, policy, after_ordinal=-1, limit=PAGE_SIZE):
    """Import a page into an explicitly selected matching derived profile.

    Source-verified literal derivations share the bounded import transaction.
    Saved generations and supplementary publications retain their own guards.
    """
    if type(after_ordinal) is not int or after_ordinal < -1 or type(limit) is not int or not 1 <= limit <= PAGE_SIZE:
        raise ValueError('knowledge_replay_page_invalid')
    with repository.read_committed(contribution_id, policy=policy) as (canonical, manifest, spec, profile):
        budget = repository._budget()
        mentions = any(binding.task == 'concept_mentions' for binding in profile.bindings)
        store_id = canonical.execute('SELECT store_id FROM knowledge_store_identity WHERE singleton=1').fetchone()[0]
        findings.database.bind_contribution_origins(repository.origin_is_live, repository.origin_epoch)
        rows = canonical.execute('SELECT s.document AS source,m.document AS record FROM knowledge_sources s '
            'JOIN knowledge_members m USING(job_id,ordinal) WHERE s.job_id=? AND s.ordinal>? ORDER BY s.ordinal LIMIT ?',
            (spec.job_id, after_ordinal, limit)).fetchall()
        ref = account_ref(spec.creator_account_id)
        with findings.database.transaction() as db:
            require_erasure_idle(db, ref)
            state = db.execute('SELECT * FROM nlp_accounts WHERE account_ref=?', (ref,)).fetchone()
            if (state is None or state['mode'] not in ('enabled', 'paused')
                    or read_profile(state['profile_json']).identity_digest() != profile.identity_digest()):
                raise ValueError('knowledge_replay_profile_unavailable')
            changed = captured = 0
            cursor = after_ordinal
            for row in rows:
                budget.check()
                member, raw = _load(SourceMemberSpec, row['source']), bytes(row['record'])
                cursor = member.ordinal
                key = hashlib.sha256(raw).hexdigest()
                # Origin ownership must be installed even when exact bytes were already imported.
                def bind_origin():
                    db.execute('INSERT OR IGNORE INTO nlp_record_origins VALUES(?,?,?,?,?)',
                               (ref, key, store_id, manifest.contribution_id, record_digest(manifest)))
                def capture_literals():
                    if (not mentions or not supported(db) or db.execute('SELECT 1 FROM nlp_literal_derivations '
                            'WHERE account_ref=? AND record_digest=?', (ref, key)).fetchone()):
                        return False
                    source = repository._source(canonical, spec, member, budget)
                    record = validate_source_bound_record(profile, source, raw)
                    try:
                        return capture_record(db, profile, source, record, key, check=budget.check, record_bytes=raw)
                    except LiteralUnavailable as error:
                        if error.reason != 'capacity':
                            raise
                        return False
                old = db.execute('SELECT * FROM nlp_jobs WHERE account_ref=? AND source_ref=?',
                                 (ref, member.source.source_ref)).fetchone()
                if old and old['state'] == 'running':
                    raise ValueError('knowledge_replay_busy')
                if (old and old['state'] == 'done' and old['record_digest'] == key
                        and old['source_version'] == member.source.source_version
                        and old['selection_epoch'] == state['selection_epoch']):
                    stored = db.execute('SELECT document FROM nlp_records WHERE account_ref=? AND record_digest=?', (ref, key)).fetchone()
                    if stored is None or bytes(stored[0]) != raw:
                        raise ValueError('knowledge_replay_record_corrupt')
                    bind_origin()
                    captured += bool(capture_literals())
                    continue
                if old and old['state'] == 'done' and old['record_digest'] != key:
                    raise ValueError('knowledge_replay_conflict')
                existing = db.execute('SELECT document FROM nlp_records WHERE account_ref=? AND record_digest=?', (ref, key)).fetchone()
                if existing and bytes(existing[0]) != raw:
                    raise ValueError('knowledge_replay_record_corrupt')
                if not existing:
                    used = db.execute('SELECT COALESCE(sum(length(document)),0) FROM nlp_records').fetchone()[0]
                    if used + len(raw) > MAX_RECORD_STORAGE:
                        raise ValueError('nlp_record_storage_limit')
                if (not db.execute('SELECT 1 FROM nlp_sources WHERE account_ref=? AND source_ref=?', (ref, member.source.source_ref)).fetchone()
                        and db.execute('SELECT count(*) FROM nlp_sources').fetchone()[0] >= MAX_LOCATORS):
                    raise ValueError('nlp_locator_storage_limit')
                db.execute('INSERT OR IGNORE INTO nlp_records VALUES(?,?,?)', (ref, key, raw))
                bind_origin()
                captured += bool(capture_literals())
                db.execute("INSERT INTO nlp_sources(account_ref,source_ref,source_version,speaker_ref,conversation_id,message_id,lane) "
                    "VALUES(?,?,?,?,?,?,'backfill') ON CONFLICT(account_ref,source_ref) DO UPDATE SET "
                    'source_version=excluded.source_version,speaker_ref=excluded.speaker_ref,conversation_id=excluded.conversation_id,message_id=excluded.message_id',
                    (ref, member.source.source_ref, member.source.source_version, member.source.speaker_ref,
                     member.location.conversation_id, member.location.message_id))
                db.execute("INSERT INTO nlp_jobs(account_ref,source_ref,source_version,selection_epoch,admission_epoch,state,lane,record_digest) "
                    "VALUES(?,?,?,?,?,'done','backfill',?) ON CONFLICT(account_ref,source_ref) DO UPDATE SET "
                    "source_version=excluded.source_version,selection_epoch=excluded.selection_epoch,admission_epoch=excluded.admission_epoch,"
                    "state='done',lane='backfill',owner=NULL,expires_at=NULL,record_digest=excluded.record_digest,terminal_reason=''",
                    (ref, member.source.source_ref, member.source.source_version, state['selection_epoch'], state['admission_epoch'], key))
                changed += 1
            if changed:
                db.execute('UPDATE nlp_accounts SET completion_revision=completion_revision+1,availability_revision=availability_revision+1,'
                    'notification_revision=notification_revision+1,inventory_digest=? WHERE account_ref=?', (inventory_digest(db, ref), ref))
            elif captured:
                db.execute('UPDATE nlp_accounts SET availability_revision=availability_revision+1,'
                    'notification_revision=notification_revision+1 WHERE account_ref=?', (ref,))
            return {'contribution_id': manifest.contribution_id, 'imported': changed, 'after_ordinal': cursor,
                    'complete': cursor >= manifest.member_count - 1}
