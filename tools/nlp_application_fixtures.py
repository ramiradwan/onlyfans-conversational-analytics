"""Invented local execution examples for independent client validation."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

from app.analytics.evidence_contracts import EvidenceLocation, EvidenceMessage
from app.analytics.opaque_refs import participant_ref
from app.models.text_analysis_client import ClientIntentPut, ClientIntentRecord, ClientIntentPage
from app.models.text_analysis_enrollment import EnrollmentCommand, EnrollmentCommandResult, EnrollmentView
from app.models.text_analysis_jobs import PreviewConfirmation
from app.models.text_analysis_preview_execution import (
    PreviewOutcome, PreviewPrepareRequest, PreviewSavedEnvelope, PreviewSavedPage, PreviewScope,
)
from app.nlp.profiles import canonical_bytes, read_profile


def application_fixtures(profile_document):
    """No captures, authority bytes or runtime inference enter these examples."""
    profile = read_profile(json.dumps(profile_document))
    tasks = [task.model_dump(mode='json') for component in profile.components for task in component.tasks]
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    source = EvidenceMessage(account_id='example-account', location=EvidenceLocation(
        conversation_id='example-chat', message_id='example-message'), source_revision=7,
        text='😀 echo echo', sent_at=now, direction='inbound', sender_id='example-speaker',
        content_hash='example-content', stream_epoch=1, source_sequence=1)
    reference = source.reference().model_dump(mode='json')
    binding = dict(account_scope=reference['account_ref'], source_ref=reference['message_ref'],
        source_version=reference['source_version_digest'][7:],
        speaker_ref=participant_ref(source.account_id, source.sender_id))
    key = '00000000-0000-4000-8000-000000000001'
    job = '00000000-0000-4000-8000-000000000002'
    contribution = '00000000-0000-4000-8000-000000000003'
    enrollment = '00000000-0000-4000-8000-000000000004'
    selection = dict(policy_id='example-selection', revision='1', sha256='6' * 64)
    command = EnrollmentCommand.model_validate_json(json.dumps(dict(command_key=key,
        expected_enrollment_id=None, expected_mode_version=0, action='enable',
        expected_profile_sha256=profile.identity_digest())))
    current = dict(installation_id='example-installation', creator_account_id=source.account_id,
        principal_id='example-principal', mode_version=1, enrollment_id=enrollment,
        state='enabled', capsule_available=True)
    view = EnrollmentView.model_validate_json(json.dumps(dict(current=current,
        profile=dict(profile_sha256=profile.identity_digest(), profile_id=profile.profile_id,
            revision=profile.revision, languages=['en'], tasks=tasks,
            presentation=profile.presentation.model_dump(mode='json')))))
    result = EnrollmentCommandResult.model_validate_json(json.dumps(dict(current=current,
        receipt=dict(command_key=key, meaning_sha256=hashlib.sha256(canonical_bytes(command)).hexdigest(),
                     mode_version=1, enrollment_id=enrollment, state='enabled'))))
    prepare = PreviewPrepareRequest(external_key=key, expected_profile_sha256=profile.identity_digest())
    scope = dict(schema_version='text-analysis-preview-scope.v1', cutoff=now.isoformat(),
        lower_exclusive=(now - timedelta(days=90)).isoformat(), directions='both', message_cap=32,
        source_count=1, earliest=now.isoformat(), latest=now.isoformat(), inventory_sha256='7' * 64)
    scope_record = PreviewScope.model_validate_json(json.dumps(scope))
    confirmation = PreviewConfirmation.model_validate_json(json.dumps(dict(
        external_key=key, job_id=job, contribution_id=contribution, spec_sha256='1' * 64,
        context_sha256='2' * 64, scope_sha256=hashlib.sha256(canonical_bytes(scope_record)).hexdigest(), parameter_sha256='4' * 64,
        input_inventory_sha256=scope['inventory_sha256'], reference_id='example-preview',
        compact_jws_sha256='5' * 64, preview_selection_policy=selection)))
    disclosure = dict(profile_sha256=profile.identity_digest(), languages=['en'], tasks=tasks,
        presentation=profile.presentation.model_dump(mode='json'), selection_policy=selection)
    outcome = PreviewOutcome.model_validate_json(json.dumps(dict(request=prepare.model_dump(mode='json'),
        receipt=dict(job_id=job, contribution_id=contribution, state='prepared', terminal_reason=None),
        scope=scope, confirmation=confirmation.model_dump(mode='json'), detailed_scope_available=True,
        disclosure=disclosure)))
    coverage = dict(source_codepoints=len(source.text), examined=[dict(start=0, end=len(source.text))],
                    excluded=[], limits_reached=[])
    summaries = []
    for descriptor in tasks:
        topic = descriptor['task'] == 'message_topics'
        category = descriptor['enabled_ids'][0]
        label = dict(presentation=disclosure['presentation'], task_contract=descriptor['task_contract'],
            taxonomy=descriptor['taxonomy'], category_id=category, display_label=category, label_status='id_only')
        label['label_identity_sha256'] = hashlib.sha256(json.dumps(label, ensure_ascii=True,
            sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()
        evidence = []
        for span in ([None] if topic else [dict(start=2, end=6), dict(start=7, end=11)]):
            evidence.append(dict(ordinal=0, source=binding, reference={**reference, 'span': span}, assessment='complete'))
        summaries.append(dict(kind='topic_category' if topic else 'mention_category', label=label,
            evidence=evidence, supporting_conversations=1, supporting_messages=1, occurrences=len(evidence),
            complete_supporting_messages=1, partial_supporting_messages=0, share_of_complete_messages=1.0,
            ranking_scope='frozen_scope', additional_categories=0))
    output = dict(job_id=job, contribution_id=contribution, spec_sha256=confirmation.spec_sha256,
        input_inventory_sha256=scope['inventory_sha256'], profile_sha256=profile.identity_digest(),
        selection_policy=selection, source_count=1,
        examination=[dict(ordinal=0, task=task['task'], assessment='assessed', reason='none', coverage=coverage)
                     for task in tasks],
        tasks=[dict(task=task['task'], eligible=1, assessed=1, partial=0, uncertain=0,
                    unsupported=0, failed=0, unprocessed=0) for task in tasks], summaries=summaries)
    saved = PreviewSavedEnvelope.model_validate_json(json.dumps(dict(scope=scope, disclosure=disclosure, output=output)))
    partial = deepcopy(saved.model_dump(mode='json'))
    examination = partial['output']['examination'][1]
    examination.update(assessment='partial', reason='limit_reached', coverage=dict(source_codepoints=11,
        examined=[dict(start=0, end=6)], excluded=[dict(start=6, end=11)], limits_reached=['windows']))
    partial['output']['tasks'][1].update(assessed=0, partial=1)
    mention = partial['output']['summaries'][1]
    mention.update(evidence=[{**mention['evidence'][0], 'assessment': 'partial'}], occurrences=1,
        complete_supporting_messages=0, partial_supporting_messages=1, share_of_complete_messages=None,
        ranking_scope='assessed_portion', additional_categories=None)
    partial = PreviewSavedEnvelope.model_validate_json(json.dumps(partial))

    def intent(kind, body):
        return ClientIntentPut.model_validate_json(json.dumps(dict(kind=kind, key=key, body=body)))

    retained = intent('enrollment_command', command.model_dump(mode='json'))
    record = ClientIntentRecord.model_validate_json(json.dumps(dict(
        **retained.model_dump(mode='json', exclude={'schema_version'}), retained_at=now.isoformat(),
        body_sha256=hashlib.sha256(canonical_bytes(command)).hexdigest())))
    records = {
        'enrollment-command': command, 'enrollment-view': view, 'enrollment-result': result,
        'preview-prepare': prepare, 'preview-outcome': outcome, 'preview-confirmation': confirmation,
        'preview-saved': saved, 'preview-saved-partial': partial,
        'client-enrollment-intent': retained, 'client-enrollment-record': record,
        'client-prepare-intent': intent('preview_prepare', prepare.model_dump(mode='json')),
        'client-confirm-intent': intent('preview_confirm', dict(external_key=key,
                                                            confirmation=confirmation.model_dump(mode='json'))),
        'client-intent-page': ClientIntentPage(items=(record,), next_after=None),
        'preview-saved-page': PreviewSavedPage.model_validate_json(json.dumps(
            dict(items=[dict(contribution_id=contribution)], next_after=None))),
    }
    return {name + '.json': record.model_dump(mode='json') for name, record in records.items()}
