"""Invented task selections through authenticated descriptors and published reads."""
import copy
from datetime import timedelta
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
import pytest

from app.analytics.errors import InvalidAnalyticsRequest, ProjectionUnavailable
from app.analytics.nlp_processing import FindingsRunner
from app.analytics.nlp_record_validation import validate_source_bound_record
from app.analytics.query_runtime import QuestionResources
from app.models.insights import FindingsCategoryFilter, FindingsReadResponse, FindingsRequest
from app.nlp import profile_authentication as authentication, v2
from app.nlp.contracts import NlpBoundaryError
from app.nlp.profiles import AnalysisProfile, SubsetAnalysisProfile, canonical_bytes, read_profile
from app.security.local_sessions import build_runtime_policy
from app.security.runtime_policy import AuthContext
from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup, advance
from tests.test_nlp_processing_publication import ConformanceAdapter
from tests.test_nlp_profile_authentication import keys, sign
from tests.test_nlp_record_validation import case, frame
from tools.generate_nlp_contracts import documents_v3, fixtures_v3

pytestmark = pytest.mark.ci_tier('integration')
ROOT = Path(__file__).resolve().parents[1]


def selected(kind='mentions'):
    return read_profile(json.dumps(fixtures_v3()[kind + '-profile.json']))


def test_shared_v3_schema_and_fixtures_are_generated_and_versioned():
    from jsonschema import Draft202012Validator
    for name, schema in documents_v3().items():
        assert json.loads((ROOT / 'shared/nlp/v3' / name).read_text()) == schema
    for name, value in fixtures_v3().items():
        assert json.loads((ROOT / 'shared/nlp/v3/fixtures' / name).read_text(encoding='utf-8')) == value
        if name.endswith('profile.json'):
            Draft202012Validator(documents_v3()['AnalysisProfile.schema.json']).validate(value)
    assert selected().identity_digest() != selected('joint').identity_digest()


@pytest.mark.parametrize('kind', ['mentions', 'joint'])
def test_signature_domains_do_not_cross_profile_versions(kind):
    profile = selected(kind)
    raw = canonical_bytes(profile)
    observed = authentication.authenticate_profile(sign(raw, typ=authentication.SUBSET_PROFILE_JWS_TYPE),
        trusted_nlp_keys=keys(), expected_profile_sha256=profile.identity_digest())
    assert observed.profile == profile
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authentication.authenticate_profile(sign(raw), trusted_nlp_keys=keys(), expected_profile_sha256=profile.identity_digest())
    legacy = AnalysisProfile.model_validate_json((ROOT / 'shared/nlp/v2/fixtures/joint-profile.json').read_bytes())
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authentication.authenticate_profile(sign(canonical_bytes(legacy), typ=authentication.SUBSET_PROFILE_JWS_TYPE),
            trusted_nlp_keys=keys(), expected_profile_sha256=legacy.identity_digest())


@pytest.mark.parametrize('fault', ['unknown', 'duplicate_json', 'missing_version', 'non_object'])
def test_profile_dispatch_and_authentication_refuse_malformed_versions(fault):
    profile = selected()
    raw = canonical_bytes(profile)
    if fault == 'unknown': raw = raw.replace(b'nlp-profile.v3', b'nlp-profile.v9')
    elif fault == 'duplicate_json': raw = b'{"schema_version":"nlp-profile.v2",' + raw[1:]
    elif fault == 'missing_version':
        value = profile.model_dump(mode='json')
        del value['schema_version']
        raw = json.dumps(value).encode()
    else: raw = b'[]'
    with pytest.raises(ValueError): read_profile(raw)
    with pytest.raises(authentication.ProfileAuthenticationRejected):
        authentication.authenticate_profile(sign(raw, typ=authentication.SUBSET_PROFILE_JWS_TYPE),
            trusted_nlp_keys=keys(), expected_profile_sha256=profile.identity_digest())


@pytest.mark.parametrize('family', [[], {}, True, 1, None])
def test_non_string_version_dispatch_has_fixed_boundary_errors(family):
    raw = json.dumps({'schema_version': family}).encode()
    with pytest.raises(ValueError, match='invalid profile version'):
        read_profile(raw)
    with pytest.raises(NlpBoundaryError, match='^nlp_invalid_snapshot$'):
        v2.decode_snapshot(raw)
    with pytest.raises(authentication.ProfileAuthenticationRejected, match='^NLP profile authentication failed$'):
        authentication.authenticate_profile(sign(raw, typ=authentication.SUBSET_PROFILE_JWS_TYPE),
            trusted_nlp_keys=keys(), expected_profile_sha256=selected().identity_digest())


@pytest.mark.parametrize('outcome', ['assessed_findings', 'assessed_no_match', 'partial', 'uncertain', 'unsupported', 'failed', 'unprocessed'])
def test_subset_worker_preserves_outcome_meanings(outcome):
    profile = selected()
    request = v2.validate_request(profile, json.dumps(fixtures_v3()['mentions-request.json']).encode())
    value = fixtures_v3()['mentions-response.json']
    payload = value['items'][0]['tasks'][0]['payload']
    payload['outcome'] = outcome
    payload['reason'] = {'partial': 'limit_reached', 'uncertain': 'below_threshold',
        'unsupported': 'unsupported_input', 'failed': 'worker_failure', 'unprocessed': 'dependency_unavailable'}.get(outcome, 'none')
    if outcome not in ('assessed_findings', 'partial'): payload['findings'] = []
    if outcome == 'partial':
        payload['coverage'].update(examined=[{'start': 0, 'end': 6}], excluded=[{'start': 6, 'end': 11}], limits_reached=['windows'])
        payload['findings'] = payload['findings'][:1]
    result = v2.validate_response(profile, request, json.dumps(value).encode())
    assert result.items[0].tasks[0].payload.outcome == outcome
    assert [t.payload.task for t in result.items[0].tasks] == ['concept_mentions']


@pytest.mark.parametrize('fault', ['empty', 'duplicate', 'unbound', 'undeclared_component_task', 'legacy_version'])
def test_profile_subset_is_exact_not_a_partial_legacy_profile(fault):
    document = selected().model_dump(mode='json')
    if fault == 'empty': document['bindings'] = []
    elif fault == 'duplicate': document['bindings'] *= 2
    elif fault == 'unbound': document['bindings'][0]['component_id'] = 'missing'
    elif fault == 'undeclared_component_task': document['components'][0]['tasks'] = fixtures_v3()['joint-profile.json']['components'][0]['tasks']
    else: document['schema_version'] = 'nlp-profile.v2'
    with pytest.raises(ValueError): read_profile(json.dumps(document))


def test_source_bound_v3_record_preserves_repeated_unicode_and_refuses_topic_substitution():
    _, source, document = case()
    profile = selected()
    document.update(schema_version='nlp-result.v3', profile=profile.model_dump(mode='json'))
    document['tasks'] = [t for t in document['tasks'] if t['result']['payload']['task'] == 'concept_mentions']
    task = document['tasks'][0]
    task['profile_sha256'] = profile.identity_digest()
    task['result']['component_sha256'] = profile.components[0].identity_digest()
    record = validate_source_bound_record(profile, source, frame(document))
    assert [source.text[f.span.start:f.span.end] for f in record.tasks[0].result.payload.findings] == ['echo', 'echo']
    assert v2.read_record(v2.encode_record(record)) == record
    for fault in ('legacy_version', 'duplicate', 'missing', 'wrong_source'):
        bad = copy.deepcopy(document)
        if fault == 'legacy_version': bad['schema_version'] = 'nlp-result.v2'
        elif fault == 'duplicate': bad['tasks'] *= 2
        elif fault == 'missing': bad['tasks'] = []
        else: bad['source']['source_version'] = 'f' * 64
        with pytest.raises(NlpBoundaryError): validate_source_bound_record(profile, source, frame(bad))
    request = v2.validate_request(profile, json.dumps(fixtures_v3()['mentions-request.json']).encode())
    bad = fixtures_v3()['mentions-response.json']
    bad['items'][0]['tasks'].append(fixtures_v3()['joint-response.json']['items'][0]['tasks'][0])
    with pytest.raises(NlpBoundaryError): v2.validate_response(profile, request, json.dumps(bad).encode())


class Occurrences(ConformanceAdapter):
    def __call__(self, request, cancelled):
        value = json.loads(super().__call__(request, cancelled))
        for task in value['items'][0]['tasks']:
            payload = task['payload']
            if payload['task'] == 'concept_mentions':
                payload.update(outcome='assessed_findings', findings=[
                    {'concept_id': 'concept.synthetic', 'span': {'start': start, 'end': start + 4}}
                    for start in (2, 7)])
        return json.dumps(value).encode()


@pytest.fixture(params=['mentions', 'joint'])
def subset(request, tmp_path):
    fixture = make_fixture(tmp_path, conversations=1, messages=2)
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='😀 echo echo'")
    profile = selected(request.param)
    allowed = [True]
    def authorize(account):
        if not allowed[0]: raise PermissionError('denied')
    adapter = Occurrences(profile)
    runner = FindingsRunner(fixture.source, fixture.stores.projections, authorize=authorize,
        execution_ready=lambda p: True, adapter=adapter, clock=lambda: fixture.clock.now,
        enrichment=fixture.pipeline.enrichment)
    runner.findings.select(ACCOUNT, profile)
    resources = QuestionResources(fixture.source, fixture.pipeline, clock=lambda: fixture.clock.now)
    policy = build_runtime_policy(AuthContext('principal.synthetic', ACCOUNT, 'creator',
        platform_creator_id='platform.synthetic', session_id='session.synthetic'))
    try: yield fixture, runner, profile, adapter, resources, policy, allowed
    finally:
        resources.close()
        assert resources.wait_closed()
        cleanup(fixture)


def publish(subset):
    fixture, runner, profile, adapter, resources, policy, allowed = subset
    while runner.process_one(ACCOUNT): pass
    run = runner.publish_pending(ACCOUNT)
    plan = FindingsRequest(expected_generation=run.artifact.projection.projection_generation,
        expected_projection_digest=run.artifact.projection.projection_digest)
    return resources.findings(policy, plan), plan


def test_subset_processing_snapshot_read_and_previous_window_use_only_declared_tasks(subset):
    fixture, runner, profile, adapter, resources, policy, _ = subset
    response, plan = publish(subset)
    declared = {b.task for b in profile.bindings}
    assert response.schema_version == 'analytics-findings.v2'
    assert {t.task for t in response.tasks} == declared
    assert {t.task: t.availability for t in response.task_availability} == {
        t: 'declared' if t in declared else 'not_declared' for t in ('message_topics', 'concept_mentions')}
    assert all(set(r.tasks) == declared and r.protocol == 'nlp-worker.v2' for r in adapter.requests)
    mentions = next(t for t in response.tasks if t.task == 'concept_mentions')
    assert mentions.rows[0].occurrences == 4 and mentions.rows[0].supporting_messages == 2
    spans = [e.reference.span for e in response.evidence_page.items if e.task == 'concept_mentions']
    assert sorted((s.start, s.end) for s in spans) == [(2, 6), (2, 6), (7, 11), (7, 11)]
    resolved = resources.resolve(policy, response.evidence_page.items[-1].reference)
    assert resolved.text == '😀 echo echo'
    bounded = resources.findings(policy, plan.model_copy(update={
        'start_date': fixture.clock.now - timedelta(days=9), 'end_date': fixture.clock.now}))
    assert {t.task for t in bounded.comparison.tasks} == declared
    assert TypeAdapter(FindingsReadResponse).validate_json(response.model_dump_json()) == response
    with fixture.stores.database.read() as db:
        records = [v2.read_record(bytes(row[0])) for row in db.execute('SELECT document FROM nlp_records')]
        snap = json.loads(db.execute('SELECT document FROM nlp_snapshots').fetchone()[0])['snapshot']
    assert all(r.schema_version == 'nlp-result.v3' for r in records)
    assert v2.decode_snapshot(json.dumps(snap).encode()).schema_version == 'nlp-snapshot.v3'
    assert {t['task'] for t in snap['tasks']} == declared


def test_subset_failure_does_not_publish_undeclared_no_match(subset):
    _, runner, profile, _, _, _, _ = subset
    runner.adapter = lambda *args: (_ for _ in ()).throw(RuntimeError('worker failed'))
    response, _ = publish(subset)
    assert all(t.failed_messages == 2 and t.complete_messages == 0 and
               all(row.occurrences == 0 and row.share_of_complete_messages is None for row in t.rows)
               for t in response.tasks)
    assert {t.task for t in response.tasks} == {b.task for b in profile.bindings}


def test_subset_category_keeps_availability_and_refuses_undeclared_task(subset):
    _, _, profile, _, resources, policy, _ = subset
    original, plan = publish(subset)
    mentions = next(t for t in original.tasks if t.task == 'concept_mentions')
    pair = FindingsCategoryFilter(task='concept_mentions', category_id=mentions.rows[0].category_id)
    response = resources.findings(policy, plan.model_copy(update={'category_filter': pair}))
    assert response.schema_version == 'analytics-findings.v3'
    assert response.tasks == original.tasks and response.comparison == original.comparison
    assert response.task_availability == original.task_availability
    assert TypeAdapter(FindingsReadResponse).validate_json(response.model_dump_json()) == response
    if not any(b.task == 'message_topics' for b in profile.bindings):
        with pytest.raises(InvalidAnalyticsRequest):
            resources.findings(policy, plan.model_copy(update={'category_filter':
                FindingsCategoryFilter(task='message_topics', category_id='topic.synthetic')}))


@pytest.mark.parametrize('change', ['authorization', 'source', 'selection'])
def test_subset_late_changes_never_complete_claim(subset, change):
    fixture, runner, profile, adapter, _, _, allowed = subset
    def before():
        if change == 'authorization': allowed[0] = False
        elif change == 'selection': runner.findings.select(ACCOUNT, profile)
        else:
            with fixture.repositories.database.transaction() as db:
                db.execute("UPDATE account_messages SET text='different'")
                advance(db)
    adapter.before = before
    try: assert not runner.process_one(ACCOUNT)
    except PermissionError: assert change == 'authorization'
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0] == 0


def test_subset_switch_to_legacy_invalidates_old_generation_and_keeps_rollback_identity(subset):
    fixture, runner, profile, _, resources, policy, _ = subset
    previous, plan = publish(subset)
    legacy = AnalysisProfile.model_validate_json((ROOT / 'shared/nlp/v2/fixtures/joint-profile.json').read_bytes())
    runner.findings.select(ACCOUNT, legacy, preserve_previous=True)
    # Selection does not rewrite the currently published immutable generation.
    assert resources.findings(policy, plan) == previous
    state = runner.findings.state(ACCOUNT)
    assert read_profile(state['previous_profile_json']) == profile
    runner.adapter = Occurrences(legacy)
    current, _ = publish(subset)
    with pytest.raises(ProjectionUnavailable): resources.findings(policy, plan)
    assert current.schema_version == 'analytics-findings.v1' and len(current.tasks) == 2
    assert current.profile_sha256 != previous.profile_sha256
    runner.findings.select(ACCOUNT, profile, preserve_previous=True)
    runner.adapter = Occurrences(profile)
    restored, _ = publish(subset)
    assert restored.schema_version == 'analytics-findings.v2'
    assert restored.profile_sha256 == previous.profile_sha256
    assert restored.snapshot_sha256 != previous.snapshot_sha256


def test_subset_erasure_revokes_published_read_and_evidence(subset):
    from app.analytics.evidence import EvidenceUnavailable
    from app.persistence.retention import CreatorVaultRetention
    fixture, _, _, _, resources, policy, _ = subset
    response, plan = publish(subset)
    reference = response.evidence_page.items[0].reference
    CreatorVaultRetention(fixture.repositories.database, clock=lambda: fixture.clock.now).delete_all(ACCOUNT)
    with pytest.raises(ProjectionUnavailable): resources.findings(policy, plan)
    with pytest.raises(EvidenceUnavailable): resources.resolve(policy, reference)


def test_shared_published_subset_wire_fixture(subset, tmp_path):
    fixture, _, profile, _, resources, policy, _ = subset
    response, plan = publish(subset)
    comparison = resources.findings(policy, plan.model_copy(update={
        'start_date': fixture.clock.now - timedelta(days=9), 'end_date': fixture.clock.now}))
    reference = next(e.reference for e in response.evidence_page.items
                     if e.task == 'concept_mentions' and e.reference.span.start == 7)
    evidence = resources.resolve(policy, reference)
    kind = 'mentions' if len(profile.bindings) == 1 else 'joint'
    for name, value in (
        (f'findings-response-{kind}.v2.json', response.model_dump(mode='json')),
        (f'findings-comparison-{kind}.v2.json', comparison.model_dump(mode='json')),
        (f'findings-evidence-{kind}.v2.json', {'account_id': ACCOUNT, 'resolved_evidence': evidence.model_dump(mode='json')})):
        path = tmp_path / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(path)


def test_subset_endpoint_uses_discriminated_response_without_erasing_availability(subset, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.analytics.runtime import AnalyticsRuntime
    from app.api.activation import require_activated_runtime
    from app.api.dependencies import get_authenticated_account_session
    from app.api.endpoints import insights
    from app.api.security import csrf_token
    from app.services import insights_service
    fixture, _, _, _, resources, policy, _ = subset
    response, plan = publish(subset)
    runtime = AnalyticsRuntime(fixture.source, fixture.pipeline,
        SimpleNamespace(request_recovery=AsyncMock()), resources)
    monkeypatch.setattr(insights_service, 'analytics_runtime', lambda: runtime)
    app = FastAPI()
    app.include_router(insights.router)
    app.dependency_overrides[get_authenticated_account_session] = lambda: policy
    app.dependency_overrides[require_activated_runtime] = lambda: None
    with TestClient(app) as client:
        client.headers['x-csrf-token'] = csrf_token(policy)
        result = client.get('/api/v1/insights/findings', params=plan.model_dump(mode='json', exclude_none=True))
    assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
    assert TypeAdapter(FindingsReadResponse).validate_json(result.content) == response


@pytest.mark.parametrize('fault', ['availability', 'duplicate_task', 'extra_comparison', 'extra_evidence'])
def test_subset_read_contract_refuses_undeclared_results(subset, fault):
    _, _, profile, _, _, _, _ = subset
    response, _ = publish(subset)
    value = response.model_dump(mode='json')
    if fault == 'availability': value['task_availability'][0]['availability'] = 'not_declared' if len(profile.bindings) == 2 else 'declared'
    elif fault == 'duplicate_task': value['tasks'] = [value['tasks'][0], value['tasks'][0]]
    elif fault == 'extra_comparison': value['comparison']['tasks'] = value['tasks']
    else:
        value['evidence_page']['items'][0]['task'] = 'message_topics' if len(profile.bindings) == 1 else 'invalid'
    with pytest.raises(ValidationError): TypeAdapter(FindingsReadResponse).validate_json(json.dumps(value))


def test_signed_subset_dependency_storage_restart_selection_rollback_and_withdrawal(tmp_path):
    from tests.test_nlp_dependencies import Environment
    e = Environment(tmp_path)
    try:
        e.selected()
        legacy = e.profile
        document = legacy.model_dump(mode='json')
        document.update(schema_version='nlp-profile.v3', revision='mentions')
        document['bindings'] = [b for b in document['bindings'] if b['task'] == 'concept_mentions']
        component = document['components'][0]
        component['tasks'] = [t for t in component['tasks'] if t['task'] == 'concept_mentions']
        component['executed_labels'] = [t for t in component['executed_labels'] if t['task'] == 'concept_mentions']
        e.profile = read_profile(json.dumps(document))
        digest = e.profile.identity_digest()
        e.approved.add(digest)
        e.objects['profiles/' + digest] = sign(canonical_bytes(e.profile), typ=authentication.SUBSET_PROFILE_JWS_TYPE)
        e.service.learn(e.catalog(sequence=2))
        e.selected()
        assert read_profile(e.findings.state(ACCOUNT)['profile_json']) == e.profile
        assert read_profile(e.findings.state(ACCOUNT)['previous_profile_json']) == legacy
        assert e.service.ready(e.profile)
        assert e.service.close()
        e.service = e.construct()
        e.service.start()
        assert not e.service.ready(e.profile)
        e.service.prepare_installed(digest)
        assert e.service.ready(e.profile)
        e.service.prepare_installed(legacy.identity_digest())
        e.service.select(ACCOUNT, None, None, rollback=True)
        assert read_profile(e.findings.state(ACCOUNT)['profile_json']) == legacy
        assert read_profile(e.findings.state(ACCOUNT)['previous_profile_json']) == e.profile
        e.service.learn(e.catalog(sequence=3, withdrawals=[digest]))
        with pytest.raises(ValueError): e.service.select(ACCOUNT, None, None, rollback=True)
        assert read_profile(e.findings.state(ACCOUNT)['profile_json']) == legacy
    finally:
        assert e.service.close()
        cleanup(e.fixture)
