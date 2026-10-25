"""Signed grants, module references and deterministic local execution stores."""
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from app.analytics.nlp_contribution_sources import ContributionSourceGuard
from app.models.text_analysis_enrollment import EnrollmentCommand
from app.nlp.execution_parameters import execution_parameters
from app.nlp.profiles import artifact_closure_digest
from app.persistence.contributions import ContributionRepository
from app.persistence.text_analysis_authority import TextAnalysisAuthorityStore
from app.persistence.text_analysis_enrollment import TextAnalysisEnrollmentStore
from app.persistence.text_analysis_jobs import TextAnalysisJobStore
from app.security.text_analysis_authority import fixture_trust
from app.security.text_analysis_contract import TextAnalysisSchemas
from app.security.text_analysis_policy import TextAnalysisRunContext
from app.services.text_analysis_selection import (
    FixtureTextAnalysisExecutionConfiguration, FixtureTextAnalysisSelection, FixtureTextAnalysisSelectionOwner,
)
from app.services.nlp_dependencies import DependencyService
from tests.continuous_analytics_fixture import ACCOUNT, cleanup
from tests.test_nlp_dependencies import Environment
from tests.test_text_analysis_execution_authority import CASES
from tests.test_text_analysis_identity import make_identity_fixture


def make_execution_fixture(path, kind='joint'):
    environment = Environment(path, kind)
    try:
        return _bind_execution_fixture(environment, path)
    except BaseException:
        assert environment.service.close()
        cleanup(environment.fixture)
        raise


def _bind_execution_fixture(environment, path):
    f = environment.fixture
    now = datetime.now(timezone.utc)
    f.clock.now = now
    environment.service.clock = lambda: f.clock.now.timestamp()
    environment.service.learn(environment.catalog(sequence=2, expires=int(now.timestamp()) + 3600))
    operation = environment.acquire()
    assert environment.service._operation(operation)['state'] == 'complete'
    auth = environment.persistence.authentication
    auth._clock = lambda: f.clock.now
    authority = TextAnalysisAuthorityStore(auth)
    trust, schemas = fixture_trust(environment='test'), TextAnalysisSchemas()
    case = next(c for c in CASES if c['case_id'] == 'license-accepted')
    reference = authority.import_verified_reference(case['token'], kind='license', context=case['context'],
                                                    trust=trust, schemas=schemas)
    payload = case['payload']
    scope = {key: payload[key] for key in
             ('organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt')}
    identity = make_identity_fixture(path/'identity', scope=scope, creator_account_id=ACCOUNT,
        authentication=auth, now=now)
    repository = ContributionRepository(f.repositories.database, source_guard=ContributionSourceGuard(),
                                        clock=lambda: f.clock.now.timestamp())
    enrollments = TextAnalysisEnrollmentStore(TextAnalysisJobStore(repository))
    profile = environment.profile
    closure = artifact_closure_digest(tuple(a for a in profile.artifacts
                                            if a.role in ('weights', 'tokenizer', 'configuration')))
    parameters = execution_parameters(profile, closure)
    context = TextAnalysisRunContext(reference.reference_id, reference.compact_jws_sha256,
        identity.identity.principal_id, ACCOUNT, scope['organization_id'], scope['installation_id'],
        scope['installation_key_id'], scope['installation_key_jkt'], payload['seat_id'], payload['seat_scope'],
        1, 1, 'text-analysis-artifact', profile.identity_digest(), closure, parameters.policy_identity_sha256)
    selection = FixtureTextAnalysisSelectionOwner(authority, enrollments, trust, schemas, scope['installation_id'],
                                                 (FixtureTextAnalysisSelection(profile.identity_digest(), context),))
    baseline = identity.owner.rebuild(identity.identity)
    authority.activate_reference(baseline, context, kind='license', evidence=selection.evidence(baseline, context),
                                lifecycle=selection.lifecycle, fixture_only=True)
    configuration = FixtureTextAnalysisExecutionConfiguration(auth, selection)
    service = configuration.bind(f.source, environment.service)
    def construct_dependencies(source, findings):
        assert source.history.database is repository.database
        return DependencyService(environment.persistence, environment.artifacts, findings, source,
            environment.distribution, keys=environment.service.keys, approvals=environment.service.approvals,
            engine_provider=environment.engine, resource_policy=environment.policy, spool_root=path/'spool',
            clock=lambda: f.clock.now.timestamp(), owners=environment.owners)

    return SimpleNamespace(environment=environment, f=f, source=f.source, auth=auth, identity=identity,
        authority=authority, reference=reference, context=context, profile=profile, parameters=parameters,
        repository=repository, enrollments=enrollments, selection=selection, configuration=configuration,
        service=service, dependencies=environment.service, construct_dependencies=construct_dependencies,
        http_policy=lambda: auth.read_bridge_session(identity.session.session_value).policy)


def command(x, action='enable', *, current=None, command_key=None):
    current = current or x.service.read_current(x.http_policy())
    return EnrollmentCommand(command_key=command_key or str(uuid4()), expected_enrollment_id=current.enrollment_id,
        expected_mode_version=current.mode_version, action=action,
        expected_profile_sha256=x.profile.identity_digest() if action in ('enable', 'replace') else None)


def close_execution_fixture(x):
    assert x.dependencies.close()
    cleanup(x.f)


@contextmanager
def execution_http_client(x, *, service=None, app=None):
    """Use actual durable session dependencies, activation and CSRF checks."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pytest import MonkeyPatch
    from app.api.endpoints import text_analysis_execution, nlp_dependencies
    from app.api.security import webauthn_session_token
    from app.core.config import settings
    from app.security.activation_gate import reset_activation
    from app.services.nlp_dependencies import configure_dependency_service, dependency_service
    from app.services.text_analysis_execution import configure_execution_service, execution_service
    selected = service or x.service
    previous, previous_dependencies = execution_service(), dependency_service()
    assert previous is None or previous is selected
    assert previous_dependencies is None or previous_dependencies is selected.dependencies
    with MonkeyPatch.context() as patch:
        patch.setattr(settings, 'auth_database_path', x.auth.database.path)
        patch.setattr(settings, 'websocket_auth_mode', 'local_session')
        patch.setattr(settings, 'bridge_origin', 'http://testserver')
        if previous is None:
            configure_execution_service(selected)
        if previous_dependencies is None:
            configure_dependency_service(selected.dependencies)
        reset_activation()
        if app is None:
            app = FastAPI()
            app.include_router(text_analysis_execution.router)
            app.include_router(nlp_dependencies.router)
        try:
            assert not app.dependency_overrides
            with TestClient(app) as client:
                client.cookies.set(settings.bridge_session_cookie_name,
                    webauthn_session_token(x.http_policy().identity, x.identity.session.session_value))
                client.headers.update({'Origin': 'http://testserver', 'X-CSRF-Token': x.identity.session.csrf_value})
                yield client
        finally:
            if previous is None and execution_service() is selected:
                configure_execution_service(None)
            if previous_dependencies is None and dependency_service() is selected.dependencies:
                configure_dependency_service(None)
            reset_activation()
