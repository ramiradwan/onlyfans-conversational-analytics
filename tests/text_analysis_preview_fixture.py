"""Real session, signed preview and paired encrypted stores with typed adapters."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import timezone
import json
from uuid import uuid4

from app.analytics.nlp_preview import SELECTION_POLICY
from app.models.text_analysis_preview_execution import PreviewPrepareRequest
from app.nlp.preview_parameters import preview_parameters
from app.persistence.text_analysis_preview import TextAnalysisPreviewStore
from app.security.text_analysis_policy import encode_run_context
from app.services.text_analysis_preview import preview_service, configure_preview_service
from app.services.text_analysis_preview_selection import (
    FixtureTextAnalysisPreviewConfiguration, FixtureTextAnalysisPreviewSelection,
    FixtureTextAnalysisPreviewSelectionOwner,
)
from tests.test_text_analysis_execution_authority import CASES
from tests.text_analysis_execution_fixture import (
    make_execution_fixture, close_execution_fixture, execution_http_client,
)


def make_preview_fixture(path, kind='joint', *, findings=False):
    x = make_execution_fixture(path, kind)
    try:
        x.license_service, x.execution_configuration = x.service, x.configuration
        case = next(c for c in CASES if c['case_id'] == 'preview-accepted')
        x.reference = x.authority.import_verified_reference(case['token'], kind='preview', context=case['context'],
            trust=x.selection.trust, schemas=x.selection.schemas)
        params = preview_parameters(x.profile, x.context.algorithm_artifacts_sha256, SELECTION_POLICY)
        x.context = replace(x.context, reference_id=x.reference.reference_id,
            compact_jws_sha256=x.reference.compact_jws_sha256, policy_identity_sha256=params.policy_identity_sha256)
        x.parameters = params
        store = TextAnalysisPreviewStore(x.repository)
        x.selection = FixtureTextAnalysisPreviewSelectionOwner(x.authority, store, x.selection.trust,
            x.selection.schemas, x.context.installation_id,
            (FixtureTextAnalysisPreviewSelection(x.profile.identity_digest(), x.context),), x.pair_owner)
        baseline = x.identity.owner.rebuild(x.identity.identity)
        with x.authority.transition_fence(baseline, x.context, kind='preview', phase='import',
                evidence=x.selection.evidence(baseline, x.context), lifecycle=x.selection.lifecycle,
                fixture_only=True) as held:
            with x.repository.database.transaction() as db:
                store.jobs.import_preview_in(db, encode_run_context(x.context), policy=held)
        x.authority.activate_reference(baseline, x.context, kind='preview',
            evidence=x.selection.evidence(baseline, x.context), lifecycle=x.selection.lifecycle, fixture_only=True)
        x.configuration = FixtureTextAnalysisPreviewConfiguration(x.auth, x.selection)
        x.service = x.configuration.bind(x.source, x.dependencies)
        if findings:
            install_findings(x)
        return x
    except BaseException:
        close_execution_fixture(x)
        raise


def request(x, external_key=None):
    return PreviewPrepareRequest(external_key=external_key or str(uuid4()),
                                 expected_profile_sha256=x.profile.identity_digest())


def prepare_preview(x, *, confirm=True):
    body = request(x)
    prepared = x.service.prepare(x.http_policy(), body)
    result = x.service.confirm(x.http_policy(), body.external_key, prepared.confirmation) if confirm else prepared
    return body, result


def install_findings(x):
    original = x.environment.engine.open
    x.analyzed = []
    @contextmanager
    def opened(*args, **kwargs):
        with original(*args, **kwargs) as session:
            analyze = session.analyze
            def selected(request, cancelled, **bounds):
                x.analyzed.append(request.request_id)
                value = json.loads(analyze(request, cancelled, **bounds))
                component = x.profile.component(request.component_id)
                for item in value['items']:
                    for task, descriptor in zip(item['tasks'], component.tasks):
                        finding = ({'topic_id': descriptor.enabled_ids[0]} if descriptor.task == 'message_topics'
                            else {'concept_id': descriptor.enabled_ids[0], 'span': {'start': 0, 'end': 6}})
                        task['payload'].update(outcome='assessed_findings', findings=[finding])
                return json.dumps(value).encode()
            session.analyze = selected
            yield session
    x.environment.engine.open = opened


def accumulated_output(x, binding, *, findings=True):
    """Create source-bound typed observations without running an Engine."""
    import hashlib
    from app.analytics.nlp_preview import PreviewAccumulator, encode_preview
    from app.analytics.nlp_findings_store import source_binding
    from app.nlp import v2
    from app.models.text_analysis_preview import NoPreviewOutput
    accumulator = PreviewAccumulator(binding.spec, binding.profile, binding.sources)
    with x.repository.database.read() as db:
        for member in binding.sources:
            source = x.repository._source(db, binding.spec, member, x.repository._budget())
            source = source.model_copy(update={'source_revision': member.source_revision})
            tasks = []
            for task_binding in binding.profile.bindings:
                component = binding.profile.component(task_binding.component_id)
                descriptor = next(t for t in component.tasks if t.task == task_binding.task)
                positive = findings is True or (type(findings) is tuple and member.ordinal in findings)
                selected = ([{'topic_id': descriptor.enabled_ids[0]}] if descriptor.task == 'message_topics'
                    else [{'concept_id': descriptor.enabled_ids[0], 'span': {'start': 0, 'end': 6}}]) if positive else []
                task = dict(task=descriptor.task, outcome='assessed_findings' if positive else 'assessed_no_match',
                    reason='none', findings=selected, coverage=dict(source_codepoints=len(source.text),
                        examined=[dict(start=0, end=len(source.text))], excluded=[], limits_reached=[]))
                value = dict(source=source_binding(source).model_dump(), profile_sha256=binding.profile.identity_digest(),
                    result=dict(component_id=component.component_id, component_sha256=component.identity_digest(),
                        text_sha256=hashlib.sha256(source.text.encode()).hexdigest(), payload=task))
                tasks.append(v2.StoredTask.model_validate_json(json.dumps(value)))
            record = v2.EnrichmentRecord(source=source_binding(source), profile=binding.profile,
                analyzed_at=x.f.clock.now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), tasks=tuple(tasks))
            accumulator.add(member.ordinal, source, record.model_dump_json().encode())
    output = accumulator.finish()
    return output if type(output) is NoPreviewOutput else encode_preview(output)


@contextmanager
def preview_http_client(x, *, service=None):
    from fastapi import FastAPI
    from app.api.endpoints import text_analysis_preview
    from app.services.text_analysis_execution import execution_service
    selected = service or x.service
    previous = preview_service()
    assert previous is None or previous is selected
    app = FastAPI()
    app.include_router(text_analysis_preview.router)
    if previous is None:
        configure_preview_service(selected)
    try:
        licensed = execution_service() or x.license_service
        assert licensed.authentication is x.auth and licensed.repository is x.repository
        assert licensed.source is x.source and licensed.dependencies is selected.dependencies
        with execution_http_client(x, service=licensed, app=app) as client:
            yield client
    finally:
        if previous is None and preview_service() is selected:
            configure_preview_service(None)


close_preview_fixture = close_execution_fixture
