"""Canonical prepared executions, licensed child ownership and preview admission."""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from app.models.knowledge_contributions import AttemptToken, JobSpec, SourceMemberSpec, record_digest
from app.models.text_analysis_jobs import (
    EnrollmentIntent, EnrollmentReceipt, ExecutionIntent, ExecutionReceipt,
    JobPlan, PreviewConfirmation,
)
from app.nlp.profiles import PolicyIdentity, canonical_bytes
from app.persistence.contributions import MAX_MEMBERS, _load, _typed, _require_dormant_write
from app.security.runtime_policy import RuntimePolicy, authorized_account
from app.security.text_analysis_policy import (
    capability_binding, decode_run_context, require_text_analysis, require_text_analysis_fence,
)


def _digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    return hashlib.sha256(raw).hexdigest()


class TextAnalysisJobStore:
    """All mutation ports participate in the caller's existing transaction.

    A retained confirmation binds a local request; it proves no browser event
    and is never a hosted acquisition or issuance envelope.
    """

    def __init__(self, repository):
        self.repository = repository

    def _guard(self, db):
        self.repository._transaction(db)
        _require_dormant_write()

    @staticmethod
    def _receipt(row):
        return ExecutionReceipt(job_id=row['job_id'], contribution_id=row['contribution_id'],
            state=row['state'], confirmation=bytes(row['confirmation']) if row['confirmation'] is not None else None,
            enrollment_id=row['enrollment_id'], terminal_reason=row['terminal_reason'])

    @staticmethod
    def _read_scope(row, policy):
        if type(policy) is not RuntimePolicy:
            raise ValueError('text_analysis_policy_invalid')
        context = decode_run_context(bytes(row['context']))
        authorized_account(policy, context.creator_account_id)
        authority = policy.identity_authority
        if (policy.identity.principal_id != context.principal_id or authority is None
                or authority.organization_id != context.organization_id
                or authority.installation_id != context.installation_id):
            raise ValueError('text_analysis_receipt_scope_invalid')
        return context

    def read_in(self, db, job_id, *, policy):
        self.repository._owned_connection(db)
        row = db.execute('SELECT * FROM text_analysis_intents WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('text_analysis_intent_unavailable')
        self._read_scope(row, policy)
        return self._receipt(row)

    def read_key_in(self, db, external_key, *, policy):
        self.repository._owned_connection(db)
        authority = policy.identity_authority
        if authority is None:
            raise ValueError('text_analysis_receipt_scope_invalid')
        row = db.execute('SELECT * FROM text_analysis_intents WHERE installation_id=? AND external_key=?',
                         (authority.installation_id, external_key)).fetchone()
        return None if row is None else self.read_in(db, row['job_id'], policy=policy)

    def read_plan_in(self, db, job_id, *, policy):
        self.repository._owned_connection(db)
        self.read_in(db, job_id, policy=policy)
        _, spec, profile = self.repository._job(db, job_id, policy)
        return spec, profile, tuple(self.repository._sources(db, job_id))

    def read_enrollment_in(self, db, enrollment_id, *, policy):
        self.repository._owned_connection(db)
        row = db.execute('SELECT * FROM text_analysis_enrollments WHERE enrollment_id=?', (enrollment_id,)).fetchone()
        if row is None:
            raise ValueError('text_analysis_enrollment_unavailable')
        self._read_scope(row, policy)
        if hashlib.sha256(bytes(row['meaning'])).hexdigest() != row['meaning_sha256']:
            raise ValueError('text_analysis_enrollment_corrupt')
        return (EnrollmentReceipt(enrollment_id=enrollment_id, state=row['state']),
                _load(EnrollmentIntent, bytes(row['meaning'])))

    @staticmethod
    def _page(after_id, limit):
        if type(after_id) is not str or len(after_id) > 36 or type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError('text_analysis_page_invalid')

    def list_enrollments_in(self, db, *, policy, after_id='', limit=64):
        self.repository._owned_connection(db)
        self._page(after_id, limit)
        account_id = authorized_account(policy, None)
        if policy.identity_authority is None:
            raise ValueError('text_analysis_receipt_scope_invalid')
        rows = db.execute('SELECT enrollment_id FROM text_analysis_enrollments WHERE installation_id=? '
            "AND creator_account_id=? AND principal_id=? AND state IN ('enabled','paused') "
            'AND enrollment_id>? ORDER BY enrollment_id LIMIT ?',
            (policy.identity_authority.installation_id, account_id, policy.identity.principal_id, after_id, limit)).fetchall()
        return tuple(self.read_enrollment_in(db, row[0], policy=policy) for row in rows)

    def list_children_in(self, db, enrollment_id, *, policy, after_id='', limit=64):
        self.repository._owned_connection(db)
        self._page(after_id, limit)
        self.read_enrollment_in(db, enrollment_id, policy=policy)
        rows = db.execute('SELECT * FROM text_analysis_intents WHERE enrollment_id=? AND job_id>? '
                          'ORDER BY job_id LIMIT ?', (enrollment_id, after_id, limit)).fetchall()
        return tuple(self._receipt(row) for row in rows)

    @staticmethod
    def _plan_context(plan, context):
        if any(getattr(plan, name) != getattr(context, name) for name in
               ('algorithm_profile_sha256', 'algorithm_artifacts_sha256', 'policy_identity_sha256')):
            raise ValueError('text_analysis_plan_context_mismatch')

    @staticmethod
    def _members(sources):
        result = []
        for member in sources:
            if len(result) >= MAX_MEMBERS:
                raise ValueError('knowledge_source_limit')
            result.append(_typed(SourceMemberSpec, member))
        return result

    def prepare_in(self, db, intent, plan, sources, *, profile_bytes, policy):
        self._guard(db)
        intent, plan = _typed(ExecutionIntent, intent), _typed(JobPlan, plan)
        context = decode_run_context(intent.run_context)
        self._plan_context(plan, context)
        reference = require_text_analysis(policy, context, kind=intent.authority_kind, phase='prepare')
        members = self._members(sources)
        meaning = _digest([intent.model_dump(mode='json'), plan.model_dump(mode='json'),
                           [m.model_dump(mode='json') for m in members]])
        old = db.execute('SELECT * FROM text_analysis_intents WHERE installation_id=? AND external_key=?',
                         (context.installation_id, intent.external_key)).fetchone()
        if old:
            if old['meaning_sha256'] != meaning:
                raise ValueError('text_analysis_intent_conflict')
            self._read_scope(old, policy)
            return self._receipt(old)
        return self._prepare(db, intent, plan, members, profile_bytes, policy, context, reference, meaning)

    def _prepare(self, db, intent, plan, members, profile_bytes, policy, context, reference, meaning,
                 *, enrollment_id=None, slot_sha256=None):
        job_id, contribution_id = str(uuid4()), str(uuid4())
        spec = JobSpec(request_id=job_id, job_id=job_id, contribution_id=contribution_id,
            installation_id=context.installation_id, organization_id=context.organization_id,
            creator_account_id=context.creator_account_id, capability=capability_binding(reference, context),
            **plan.model_dump(mode='python'))
        confirmation = None
        if intent.authority_kind == 'preview':
            confirmation = canonical_bytes(PreviewConfirmation(external_key=intent.external_key,
                job_id=job_id, contribution_id=contribution_id, spec_sha256=record_digest(spec),
                context_sha256=hashlib.sha256(intent.run_context).hexdigest(), scope_sha256=intent.scope_sha256,
                parameter_sha256=plan.parameter_sha256, input_inventory_sha256=plan.input.inventory_sha256,
                reference_id=reference.reference_id, compact_jws_sha256=reference.compact_jws_sha256,
                preview_selection_policy=intent.preview_selection_policy))
        db.execute('INSERT INTO text_analysis_intents '
            '(job_id,contribution_id,installation_id,external_key,enrollment_id,slot_sha256,creator_account_id,'
            'principal_id,authority_kind,reference_id,compact_jws_sha256,context,meaning_sha256,spec_sha256,'
            'scope_sha256,parameter_sha256,state,confirmation,preview_selection_policy,created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (job_id, contribution_id, context.installation_id, None if enrollment_id else intent.external_key,
             enrollment_id, slot_sha256, context.creator_account_id, context.principal_id, intent.authority_kind,
             reference.reference_id, reference.compact_jws_sha256, intent.run_context, meaning, record_digest(spec),
             intent.scope_sha256, plan.parameter_sha256, 'prepared', confirmation,
             canonical_bytes(intent.preview_selection_policy) if intent.preview_selection_policy is not None else None,
             self.repository.clock()))
        self.repository.create_job_in(db, spec, members, profile_bytes=profile_bytes, policy=policy, initial_state='prepared')
        return self.read_in(db, job_id, policy=policy)

    def activate_in(self, db, job_id, *, confirmation=None, policy):
        self._guard(db)
        row, spec, _ = self.repository._job(db, job_id, policy)
        intent = self.repository._module_intent(db, job_id)
        if intent is None:
            raise ValueError('text_analysis_intent_unavailable')
        self.repository._write_scope(db, spec, policy, 'start')
        if row['state'] == 'prepared':
            budget = self.repository._budget()
            for member in self.repository._sources(db, job_id):
                self.repository._source(db, spec, member, budget)
        if intent['authority_kind'] == 'preview':
            if type(confirmation) is not bytes or confirmation != bytes(intent['confirmation'] or b''):
                raise ValueError('text_analysis_confirmation_mismatch')
            parsed = _load(PreviewConfirmation, confirmation)
            if (parsed.job_id != job_id or parsed.spec_sha256 != record_digest(spec)
                    or parsed.compact_jws_sha256 != intent['compact_jws_sha256']):
                raise ValueError('text_analysis_confirmation_mismatch')
            admission = self._admission(db, intent, job_id, allow_available=True, allow_spent=row['state'] == 'committed')
            if admission['state'] == 'available':
                if row['state'] != 'prepared':
                    raise ValueError('text_analysis_admission_conflict')
                db.execute("UPDATE text_analysis_preview_admissions SET state='reserved',job_id=? "
                           'WHERE reference_id=?', (job_id, intent['reference_id']))
            if row['state'] == 'prepared':
                db.execute('UPDATE text_analysis_intents SET confirmed_sha256=? WHERE job_id=?',
                           (hashlib.sha256(confirmation).hexdigest(), job_id))
        elif confirmation is not None:
            raise ValueError('text_analysis_confirmation_unexpected')
        if row['state'] == 'prepared':
            db.execute("UPDATE knowledge_jobs SET state='pending' WHERE job_id=?", (job_id,))
        elif row['state'] not in ('pending', 'running', 'committed'):
            raise ValueError('text_analysis_job_terminal')
        return self.read_in(db, job_id, policy=policy)

    @staticmethod
    def _admission(db, intent, job_id, *, allow_available=False, allow_spent=False):
        row = db.execute('SELECT * FROM text_analysis_preview_admissions WHERE reference_id=?',
                         (intent['reference_id'],)).fetchone()
        if (row is None or row['installation_id'] != intent['installation_id']
                or row['compact_jws_sha256'] != intent['compact_jws_sha256']):
            raise ValueError('text_analysis_admission_unavailable')
        allowed = (allow_available and row['state'] == 'available') or (
            row['state'] == 'reserved' and row['job_id'] == job_id) or (
            allow_spent and row['state'] == 'spent' and row['job_id'] == job_id
            and row['contribution_id'] == intent['contribution_id'])
        if not allowed:
            raise ValueError('text_analysis_admission_conflict')
        return row

    def import_preview_in(self, db, run_context, *, policy):
        self._guard(db)
        context = decode_run_context(run_context)
        reference = require_text_analysis_fence(policy, context, kind='preview', phase='import',
                                               canonical_database=self.repository.database)
        old = db.execute('SELECT * FROM text_analysis_preview_admissions WHERE installation_id=? AND permit_id=?',
                         (context.installation_id, reference.object_id)).fetchone()
        if old:
            if (old['reference_id'], old['compact_jws_sha256'], old['issuance_id']) != (
                    reference.reference_id, reference.compact_jws_sha256, reference.issuance_id):
                raise ValueError('text_analysis_permit_collision')
            return old['state']
        db.execute('INSERT INTO text_analysis_preview_admissions VALUES(?,?,?,?,?,?,NULL,NULL)',
                   (context.installation_id, reference.object_id, reference.reference_id,
                    reference.compact_jws_sha256, reference.issuance_id, 'available'))
        return 'available'

    def enroll_in(self, db, intent, *, policy):
        self._guard(db)
        intent = _typed(EnrollmentIntent, intent)
        context = decode_run_context(intent.run_context)
        self._plan_context(intent.parameters, context)
        require_text_analysis(policy, context, kind='license', phase='prepare')
        raw = canonical_bytes(intent)
        old = db.execute('SELECT * FROM text_analysis_enrollments WHERE installation_id=? AND external_key=?',
                         (context.installation_id, intent.external_key)).fetchone()
        if old:
            if bytes(old['meaning']) != raw:
                raise ValueError('text_analysis_enrollment_conflict')
            self._read_scope(old, policy)
            return EnrollmentReceipt(enrollment_id=old['enrollment_id'], state=old['state'])
        if self.repository._storage(db) + len(raw) + len(intent.run_context) > self.repository.max_storage_bytes:
            raise ValueError('knowledge_storage_full')
        enrollment_id = str(uuid4())
        db.execute('INSERT INTO text_analysis_enrollments VALUES(?,?,?,?,?,?,?,?,?,?)',
            (enrollment_id, context.installation_id, intent.external_key, context.creator_account_id,
             context.principal_id, raw, hashlib.sha256(raw).hexdigest(), intent.run_context, 'enabled', self.repository.clock()))
        return EnrollmentReceipt(enrollment_id=enrollment_id, state='enabled')

    def set_enrollment_mode_in(self, db, enrollment_id, mode, *, policy):
        self._guard(db)
        if mode not in ('enabled', 'paused', 'retired'):
            raise ValueError('text_analysis_enrollment_mode_invalid')
        row = db.execute('SELECT * FROM text_analysis_enrollments WHERE enrollment_id=?', (enrollment_id,)).fetchone()
        if row is None:
            raise ValueError('text_analysis_enrollment_unavailable')
        context = self._read_scope(row, policy)
        if row['state'] == 'retired':
            if mode != 'retired':
                raise ValueError('text_analysis_enrollment_retired')
        else:
            if mode == 'enabled':
                require_text_analysis_fence(policy, context, kind='license', phase='start',
                                            canonical_database=self.repository.database)
            db.execute('UPDATE text_analysis_enrollments SET state=? WHERE enrollment_id=?', (mode, enrollment_id))
            if mode == 'retired':
                db.execute('DELETE FROM knowledge_jobs WHERE job_id IN '
                           '(SELECT job_id FROM text_analysis_intents WHERE enrollment_id=?)', (enrollment_id,))
        return EnrollmentReceipt(enrollment_id=enrollment_id, state=mode)

    def prepare_child_in(self, db, enrollment_id, plan, sources, *, profile_bytes, policy):
        self._guard(db)
        row = db.execute('SELECT * FROM text_analysis_enrollments WHERE enrollment_id=?', (enrollment_id,)).fetchone()
        if row is None or row['state'] != 'enabled':
            raise ValueError('text_analysis_enrollment_unavailable')
        context = self._read_scope(row, policy)
        reference = require_text_analysis(policy, context, kind='license', phase='prepare')
        enrollment = _load(EnrollmentIntent, bytes(row['meaning']))
        plan = _typed(JobPlan, plan)
        if {k: v for k, v in plan.model_dump().items() if k != 'input'} != enrollment.parameters.model_dump():
            raise ValueError('text_analysis_enrollment_plan_mismatch')
        members = self._members(sources)
        if len(members) != 1:
            raise ValueError('text_analysis_child_source_count')
        member = members[0]
        slot = _digest(['text-analysis-child.v1', enrollment_id, member.source.model_dump(), member.text_sha256,
                        enrollment.parameters.model_dump()])
        old = db.execute('SELECT * FROM text_analysis_intents WHERE slot_sha256=?', (slot,)).fetchone()
        if old:
            return self._receipt(old)
        intent = ExecutionIntent(external_key=str(uuid4()), authority_kind='license',
                                 run_context=bytes(row['context']), scope_sha256=slot)
        return self._prepare(db, intent, plan, members, profile_bytes, policy, context, reference, slot,
                             enrollment_id=enrollment_id, slot_sha256=slot)

    def _preview_attempt(self, db, token, policy):
        self._guard(db)
        token = _typed(AttemptToken, token)
        row, spec, profile = self.repository._job(db, token.job_id, policy)
        intent = self.repository._module_intent(db, token.job_id)
        if intent is None or intent['authority_kind'] != 'preview':
            raise ValueError('text_analysis_preview_required')
        self.repository._write_scope(db, spec, policy, 'commit')
        return token, row, spec, profile, intent

    def commit_preview_in(self, db, token, frame, *, policy):
        token, row, spec, profile, intent = self._preview_attempt(db, token, policy)
        old = db.execute('SELECT * FROM text_analysis_preview_outputs WHERE job_id=?', (token.job_id,)).fetchone()
        if old:
            if (bytes(old['document']) != frame or old['document_sha256'] != hashlib.sha256(frame).hexdigest()
                    or old['sealing_attempt_fence'] != token.attempt_fence
                    or old['sealing_job_attempt_id'] != token.job_attempt_id):
                raise ValueError('knowledge_attempt_stale')
            self._admission(db, intent, token.job_id, allow_spent=True)
        else:
            self.repository._attempt(row, token)
            self._admission(db, intent, token.job_id)
        sources = tuple(self.repository._sources(db, token.job_id))
        output = self.repository.source_guard.preview_output(spec, profile, sources, frame)
        if output.selection_policy != _load(PolicyIdentity, bytes(intent['preview_selection_policy'])):
            raise ValueError('text_analysis_preview_policy_mismatch')
        budget = self.repository._budget()
        for member in sources:
            source = self.repository._source(db, spec, member, budget)
            self.repository.source_guard.preview_source(output, profile, member, source)
        budget.check()
        valid_until = self.repository._source_valid_until(db, spec.job_id)
        if valid_until is not None and valid_until <= self.repository.clock():
            raise ValueError('knowledge_source_expired')
        if old:
            return output
        self.repository._attempt(row, token)
        if self.repository._storage(db) + len(frame) > self.repository.max_storage_bytes:
            raise ValueError('knowledge_storage_full')
        db.execute('INSERT INTO text_analysis_preview_outputs VALUES(?,?,?,?,?,?)',
                   (spec.contribution_id, token.job_id, frame, hashlib.sha256(frame).hexdigest(),
                    token.attempt_fence, token.job_attempt_id))
        db.execute("UPDATE text_analysis_preview_admissions SET state='spent',contribution_id=? WHERE reference_id=?",
                   (spec.contribution_id, intent['reference_id']))
        db.execute("UPDATE knowledge_jobs SET state='committed',lease_expires_at=NULL,committed_at=? WHERE job_id=?",
                   (self.repository.clock(), token.job_id))
        return output

    def finish_without_output_in(self, db, token, outcome, *, policy):
        token, row, spec, profile, intent = self._preview_attempt(db, token, policy)
        self.repository._attempt(row, token)
        self._admission(db, intent, token.job_id)
        outcome = self.repository.source_guard.preview_no_output(
            spec, profile, tuple(self.repository._sources(db, token.job_id)), outcome)
        if outcome.selection_policy != _load(PolicyIdentity, bytes(intent['preview_selection_policy'])):
            raise ValueError('text_analysis_preview_policy_mismatch')
        db.execute("UPDATE text_analysis_preview_admissions SET state='available',job_id=NULL WHERE reference_id=?",
                   (intent['reference_id'],))
        db.execute("UPDATE text_analysis_intents SET terminal_reason=? WHERE job_id=?", (outcome.disposition, token.job_id))
        db.execute("UPDATE knowledge_jobs SET state='failed',lease_expires_at=NULL WHERE job_id=?", (token.job_id,))
        return self.read_in(db, token.job_id, policy=policy)
