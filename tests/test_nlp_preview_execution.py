"""Shared adapter validation and bounded preview computation."""
from pathlib import Path

import pytest

from app.analytics.nlp_processing import execution_slot, infer_source
from app.analytics.nlp_preview import PreviewAccumulator
from app.nlp.profiles import AnalysisProfile
from tests.test_nlp_preview import case
from tests.test_nlp_processing_publication import ConformanceAdapter

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


@pytest.mark.parametrize('kind', ['joint', 'split'])
def test_shared_inference_uses_validated_component_requests(kind):
    profile = AnalysisProfile.model_validate_json((Path(__file__).resolve().parents[1] /
        f'shared/nlp/v2/fixtures/{kind}-profile.json').read_bytes())
    original = case()[3][0]
    execute = ConformanceAdapter(profile)
    results = infer_source(profile, original, execute, cancelled=lambda: False, remaining_ms=lambda: 1000)
    assert len(execute.requests) == len(profile.components)
    assert {result.payload.task for result in results} == {binding.task for binding in profile.bindings}
    assert all(result.payload.outcome == 'assessed_no_match' for result in results)


def test_split_interruption_never_fabricates_missing_task_results():
    profile = AnalysisProfile.model_validate_json((Path(__file__).resolve().parents[1] /
        'shared/nlp/v2/fixtures/split-profile.json').read_bytes())
    execute = ConformanceAdapter(profile)
    result = infer_source(profile, case()[3][0], execute,
        cancelled=lambda: bool(execute.requests), remaining_ms=lambda: 1000)
    assert result is False and len(execute.requests) == 1


def test_worker_failure_fallback_remains_truthful_for_licensed_consumer():
    _, profile, _, messages = case()
    def failed(request, cancelled):
        raise ValueError('worker_unavailable')
    results = infer_source(profile, messages[0], failed, cancelled=lambda: False, remaining_ms=lambda: 1000)
    assert all(result.payload.outcome == 'failed' and result.payload.reason == 'worker_failure'
               and not result.payload.findings for result in results)


def test_existing_global_inference_slot_excludes_another_owner():
    with execution_slot() as first:
        assert first
        with execution_slot() as second:
            assert not second
    with execution_slot() as released:
        assert released


def test_unprocessed_deadline_is_explicit_and_does_not_claim_assessment():
    spec, profile, members, _ = case()
    accumulator = PreviewAccumulator(spec, profile, members)
    accumulator.unprocessed(0, reason='deadline')
    outcome = accumulator.finish()
    assert outcome.disposition == 'incomplete'
    with pytest.raises(ValueError, match='preview_reason_invalid'):
        PreviewAccumulator(spec, profile, members).unprocessed(0, reason='worker_failure')


@pytest.fixture
def preview_execution(tmp_path, request):
    from tests.text_analysis_preview_fixture import make_preview_fixture, close_preview_fixture, prepare_preview
    from app.analytics.nlp_preview_execution import PreviewComputeOwner
    x = make_preview_fixture(tmp_path, getattr(request, "param", "joint"), findings=True)
    try:
        x.body, x.outcome = prepare_preview(x)
        x.binding = x.service.bind_pending(x.service.pending_locators()[0])
        x.compute = PreviewComputeOwner(x.service, x.dependencies, x.source, clock=lambda: x.f.clock.now)
        yield x
    finally:
        close_preview_fixture(x)


def preview_rows(x):
    with x.repository.database.read() as db:
        return (db.execute('SELECT state FROM knowledge_jobs').fetchone()[0],
                db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0],
                db.execute('SELECT count(*) FROM text_analysis_preview_outputs').fetchone()[0],
                db.execute('SELECT count(*) FROM knowledge_members').fetchone()[0])


def test_actual_compute_cutoff_preserves_completed_observations_without_complete_totals(preview_execution, monkeypatch):
    from app.analytics.nlp_preview_execution import PreviewComputeOwner
    x = preview_execution
    elapsed = [0.0]
    reads = []
    original = x.source.read_evidence_message
    def read(*args, **kwargs):
        value = original(*args, **kwargs)
        reads.append(value)
        if len(reads) == 2:
            elapsed[0] = 91.0
        return value
    monkeypatch.setattr(x.source, 'read_evidence_message', read)
    compute = PreviewComputeOwner(x.service, x.dependencies, x.source,
        clock=lambda: x.f.clock.now, monotonic=lambda: elapsed[0])
    result = compute.run(x.binding)
    assert result.state == 'committed'
    shown = x.service.read_shown(x.http_policy(), result.contribution_id)
    assert all(summary.ranking_scope == 'assessed_portion' and summary.additional_categories is None
               for summary in shown.summaries)
    assert any(entry.assessment == 'unprocessed' and entry.reason == 'deadline' for entry in shown.examination)
    assert preview_rows(x) == ('committed', 'spent', 1, 0)


def test_explicit_stop_discards_candidates_without_spend_or_synthetic_terminal(preview_execution, monkeypatch):
    from app.analytics.nlp_preview_execution import PreviewComputeOwner
    x = preview_execution
    stopped = [False]
    original = x.source.read_evidence_message
    reads = []
    def read(*args, **kwargs):
        value = original(*args, **kwargs)
        reads.append(value)
        if len(reads) == 2:
            stopped[0] = True
        return value
    monkeypatch.setattr(x.source, 'read_evidence_message', read)
    compute = PreviewComputeOwner(x.service, x.dependencies, x.source,
        clock=lambda: x.f.clock.now, cancellation_check=lambda: stopped[0])
    with pytest.raises(ValueError, match='preview_cancelled'):
        compute.run(x.binding)
    assert preview_rows(x) == ('running', 'reserved', 0, 0)
    with x.repository.database.read() as db:
        assert db.execute('SELECT terminal_reason FROM text_analysis_intents').fetchone()[0] is None
    from datetime import timedelta
    x.f.clock.now += timedelta(seconds=121)
    stopped[0] = False
    retained = x.service.bind_pending(x.service.pending_locators()[0])
    result = compute.run(retained)
    assert result.job_id == x.binding.spec.job_id and result.contribution_id == x.binding.spec.contribution_id
    assert preview_rows(x) == ('committed', 'spent', 1, 0)


def test_shared_inference_owner_prevents_claiming_a_preview(preview_execution):
    x = preview_execution
    with execution_slot() as acquired:
        assert acquired
        assert x.compute.run(x.binding) is None
    assert preview_rows(x) == ('pending', 'reserved', 0, 0)


def test_authority_failure_after_worker_never_commits_candidates(preview_execution, monkeypatch):
    from contextlib import contextmanager
    x = preview_execution
    original = x.environment.engine.open
    @contextmanager
    def opened(*args, **kwargs):
        with original(*args, **kwargs) as session:
            analyze = session.analyze
            def revoked(*args, **kwargs):
                value = analyze(*args, **kwargs)
                x.authority.revoke(x.reference.reference_id)
                return value
            session.analyze = revoked
            yield session
    monkeypatch.setattr(x.environment.engine, 'open', opened)
    with pytest.raises(ValueError):
        x.compute.run(x.binding)
    assert preview_rows(x) == ('running', 'reserved', 0, 0)


def test_frozen_preview_refuses_before_claim_or_worker(preview_execution, monkeypatch):
    import sys
    x = preview_execution
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    with pytest.raises(ValueError):
        x.compute.run(x.binding)
    assert preview_rows(x) == ('pending', 'reserved', 0, 0)
    assert not x.analyzed


@pytest.mark.parametrize('preview_execution', ['split'], indirect=True)
def test_actual_split_cutoff_discards_unfinished_source_and_releases_without_spend(preview_execution, monkeypatch):
    from contextlib import contextmanager
    from app.analytics.nlp_preview_execution import PreviewComputeOwner
    x = preview_execution
    elapsed = [0.0]
    original = x.environment.engine.open
    @contextmanager
    def opened(*args, **kwargs):
        with original(*args, **kwargs) as session:
            analyze = session.analyze
            def cutoff(*args, **kwargs):
                value = analyze(*args, **kwargs)
                elapsed[0] = 91.0
                return value
            session.analyze = cutoff
            yield session
    monkeypatch.setattr(x.environment.engine, 'open', opened)
    compute = PreviewComputeOwner(x.service, x.dependencies, x.source,
        clock=lambda: x.f.clock.now, monotonic=lambda: elapsed[0])
    result = compute.run(x.binding)
    assert result.state == 'failed'
    assert preview_rows(x) == ('failed', 'available', 0, 0)
    with x.repository.database.read() as db:
        assert db.execute('SELECT terminal_reason FROM text_analysis_intents').fetchone()[0] == 'incomplete'
    assert len(x.analyzed) == 1


@pytest.mark.parametrize('preview_execution', ['split'], indirect=True)
def test_split_adapter_exception_without_callback_discards_the_whole_unfinished_source(preview_execution, monkeypatch):
    from contextlib import contextmanager
    x = preview_execution
    original = x.environment.engine.open
    calls = []
    @contextmanager
    def opened(*args, **kwargs):
        with original(*args, **kwargs) as session:
            analyze = session.analyze
            def failed(request, cancelled, **bounds):
                calls.append(request.component_id)
                if len(calls) == 2:
                    raise ValueError('adapter_unavailable')
                return analyze(request, cancelled, **bounds)
            session.analyze = failed
            yield session
    monkeypatch.setattr(x.environment.engine, 'open', opened)
    with pytest.raises(ValueError, match='adapter_unavailable'):
        x.compute.run(x.binding)
    assert len(calls) == 2
    assert preview_rows(x) == ('running', 'reserved', 0, 0)
    with x.repository.database.read() as db:
        assert db.execute('SELECT terminal_reason FROM text_analysis_intents').fetchone()[0] is None


@pytest.fixture
def catalogue_preview_execution(tmp_path, request):
    from app.analytics.nlp_preview_execution import PreviewComputeOwner
    from tests.test_nlp_presentation import presentation_example
    from tests.text_analysis_preview_fixture import make_preview_fixture, close_preview_fixture, prepare_preview
    x = make_preview_fixture(tmp_path, 'split', findings=getattr(request, 'param', True),
        profile_factory=presentation_example)
    try:
        prepare_preview(x)
        x.binding = x.service.bind_pending(x.service.pending_locators()[0])
        x.compute = PreviewComputeOwner(x.service, x.dependencies, x.source, clock=lambda: x.f.clock.now)
        yield x
    finally:
        close_preview_fixture(x)


def test_actual_preview_freezes_verified_catalogue_names_after_preparation(catalogue_preview_execution, monkeypatch):
    from contextlib import contextmanager
    from app.analytics.nlp_read_projection import frozen_labels
    x = catalogue_preview_execution
    events = []
    ready = x.dependencies.ready
    prepare = x.dependencies.prepare_installed
    resolve = x.dependencies.presentation_resolver
    retain = x.dependencies.retain
    first = [True]
    def cold(profile):
        if first[0]:
            first[0] = False
            return False
        return ready(profile)
    def prepared(*args, **kwargs):
        events.append('prepare')
        return prepare(*args, **kwargs)
    def resolved(profile):
        events.append('labels')
        return resolve(profile)
    @contextmanager
    def retained(profile):
        events.append('retain')
        with retain(profile) as execute:
            yield execute
    expected = {(row['task_contract'], row['category_id']): {**row,
        'label_identity_sha256': row['label_identity_sha256'].removeprefix('sha256:')}
        for row in frozen_labels(x.profile, resolve(x.profile))}
    monkeypatch.setattr(x.dependencies, 'ready', cold)
    monkeypatch.setattr(x.dependencies, 'prepare_installed', prepared)
    monkeypatch.setattr(x.dependencies, 'presentation_resolver', resolved)
    monkeypatch.setattr(x.dependencies, 'retain', retained)
    result = x.compute.run(x.binding)
    assert result.state == 'committed'
    assert events == ['prepare', 'labels', 'retain']
    shown = x.service.read_shown(x.http_policy(), result.contribution_id)
    assert len(shown.summaries) == 2
    for summary in shown.summaries:
        label = summary.label
        assert label.model_dump(mode='json') == expected[(label.task_contract, label.category_id)]
        assert label.label_status == 'resolved' and label.display_label == 'Example caf\u00e9 \U0001f600'
    assert shown.profile_sha256 == x.binding.profile.identity_digest()
    assert shown.input_inventory_sha256 == x.binding.spec.input.inventory_sha256
    assert preview_rows(x) == ('committed', 'spent', 1, 0)
    available = x.environment.artifacts.reopen(x.profile)
    available.directory.rename(available.directory.with_name('held-preview-catalogue'))
    x.environment.objects.clear()
    x.environment.engine.present = False
    monkeypatch.setattr(x.dependencies, 'presentation_resolver',
        lambda _: pytest.fail('saved preview labels must not reload optional data'))
    assert x.service.read_shown(x.http_policy(), result.contribution_id) == shown


@pytest.mark.parametrize('missing', [False, True])
def test_actual_preview_refuses_corrupt_catalogue_without_spend(catalogue_preview_execution, missing):
    from app.persistence.private_files import PrivateFileSecurityError
    x = catalogue_preview_execution
    available = x.environment.artifacts.reopen(x.profile)
    member = next(a for a in available.artifacts if a.artifact.artifact_id == 'presentation')
    if missing:
        member.path.rename(member.path.with_name('held-preview-catalogue.data'))
    else:
        member.path.write_bytes(b'x' * member.artifact.size_bytes)
    with pytest.raises((ValueError, OSError, PrivateFileSecurityError)):
        x.compute.run(x.binding)
    assert not x.analyzed
    assert preview_rows(x) == ('running', 'reserved', 0, 0)
    with x.repository.database.read() as db:
        assert db.execute('SELECT terminal_reason FROM text_analysis_intents').fetchone()[0] is None
    with execution_slot() as acquired:
        assert acquired


def test_actual_legacy_preview_keeps_id_only_labels(preview_execution):
    from app.analytics.nlp_read_projection import frozen_labels
    x = preview_execution
    result = x.compute.run(x.binding)
    shown = x.service.read_shown(x.http_policy(), result.contribution_id)
    expected = {(row['task_contract'], row['category_id']): row['label_identity_sha256'].removeprefix('sha256:')
        for row in frozen_labels(x.profile)}
    assert shown.summaries
    for summary in shown.summaries:
        label = summary.label
        assert label.label_status == 'id_only' and label.display_label == label.category_id
        assert label.label_identity_sha256 == expected[(label.task_contract, label.category_id)]
    assert preview_rows(x) == ('committed', 'spent', 1, 0)


@pytest.mark.parametrize('catalogue_preview_execution', [False], indirect=True)
def test_actual_catalogue_preview_without_findings_releases_admission(catalogue_preview_execution):
    x = catalogue_preview_execution
    result = x.compute.run(x.binding)
    assert result.state == 'failed'
    assert preview_rows(x) == ('failed', 'available', 0, 0)
    with x.repository.database.read() as db:
        assert db.execute('SELECT terminal_reason FROM text_analysis_intents').fetchone()[0] == 'empty'
