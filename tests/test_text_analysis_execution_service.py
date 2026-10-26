"""Account commands against actual encrypted authority and canonical stores."""
from datetime import timedelta
from dataclasses import replace
from contextlib import contextmanager
import gc
import sys
from threading import Event, Thread
import weakref
from uuid import uuid4

import pytest

from app.core.config import settings
from app.services import text_analysis_execution as execution_module
from app.nlp.execution_parameters import execution_parameters, require_original_parameters, SOURCE_POLICY
from app.nlp.profiles import canonical_bytes
from app.persistence.auth import RevocationKey, RevocationScopeType
from app.security.runtime_policy import RuntimeAuthorizationDenied
from tests.continuous_analytics_fixture import ACCOUNT
from tests.text_analysis_execution_fixture import make_execution_fixture, close_execution_fixture, command

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


@pytest.fixture
def execution(tmp_path):
    x = make_execution_fixture(tmp_path)
    yield x
    close_execution_fixture(x)


@pytest.mark.parametrize('kind', ['joint', 'split'])
def test_new_enrollment_freezes_server_recipe_and_background_lineage(tmp_path, kind):
    x = make_execution_fixture(tmp_path, kind)
    try:
        before = x.environment.engine.prepares
        result = x.service.command(x.http_policy(), command(x))
        assert x.environment.engine.prepares == before
        assert x.source.full_reads == 0
        with x.repository.database.read() as db:
            assert db.execute('SELECT count(*) FROM knowledge_jobs').fetchone()[0] == 0
            capsule = x.enrollments.read_capsule_in(db, result.current.enrollment_id,
                policy=x.identity.owner.rebuild(x.identity.identity))
        assert capsule.profile_bytes == canonical_bytes(x.profile)
        assert capsule.identity == x.identity.identity and b'session' not in canonical_bytes(capsule.identity)
        assert capsule.intent.parameters == x.parameters
        assert x.parameters.policy_identity_sha256 == SOURCE_POLICY.identity_digest()
        binding = x.service.bind_current(x.service.current_locators()[0])
        assert binding.execution.enrollment_id == result.current.enrollment_id
        assert binding.identity_policy.identity.session_id is None
        x.service.authorize_binding(binding, ACCOUNT)
        x.f.clock.now += timedelta(minutes=11)
        assert x.auth.read_bridge_session(x.identity.session.session_value) is None
        assert x.service.bind_current(binding.locator).capsule == capsule
        with pytest.raises(RuntimeAuthorizationDenied):
            x.service.command(x.identity.policy, command(x, 'pause', current=result.current))
    finally:
        close_execution_fixture(x)


def test_receipt_retry_precedes_missing_engine_and_module_reference(execution):
    x = execution
    enable = command(x)
    first = x.service.command(x.http_policy(), enable)
    paused = x.service.command(x.http_policy(), command(x, 'pause'))
    x.environment.engine.present = False
    x.authority.revoke(x.reference.reference_id)
    replay = x.service.command(x.http_policy(), enable)
    assert replay.receipt == first.receipt and replay.current == paused.current
    assert x.service.read_command(x.http_policy(), enable.command_key) == replay
    assert x.service.read_command(x.http_policy(), str(uuid4())) is None
    with pytest.raises(ValueError, match='command_conflict'):
        x.service.command(x.http_policy(), enable.model_copy(update={'expected_mode_version': 2}))
    with pytest.raises(ValueError):
        x.service.command(x.http_policy(), command(x, 'resume'))
    retired = x.service.command(x.http_policy(), command(x, 'retire'))
    assert retired.current.state == 'retired'


def test_restart_can_enroll_cached_profile_without_reusing_warmup(execution):
    x = execution
    assert x.dependencies.close()
    restarted = x.construct_dependencies(x.source, x.dependencies.findings)
    service = x.configuration.bind(x.source, restarted)
    try:
        restarted.start()
        assert not restarted.ready(x.profile) and not restarted.available
        before = (x.environment.engine.prepares, list(x.environment.requests), x.source.full_reads)
        enabled = service.command(x.http_policy(), command(x))
        assert enabled.current.state == 'enabled'
        assert not restarted.ready(x.profile) and not restarted.available
        assert before == (x.environment.engine.prepares, x.environment.requests, x.source.full_reads)
        assert service.current_locators()[0].enrollment_id == enabled.current.enrollment_id
    finally:
        assert restarted.close()


def test_selection_requires_real_cached_profile_engine_and_single_owner(execution):
    x = execution
    cmd = command(x)
    with pytest.raises(ValueError, match='composition_owner_invalid'):
        x.configuration.bind(x.source, x.dependencies)
    x.environment.engine.present = False
    with pytest.raises(ValueError, match='engine_unavailable'):
        x.service.command(x.http_policy(), cmd)
    x.environment.engine.present = True
    x.dependencies.persistence.removed(x.profile.identity_digest())
    with pytest.raises(ValueError, match='selection_unavailable'):
        x.service.command(x.http_policy(), cmd)
    with x.repository.database.read() as db:
        assert db.execute('SELECT count(*) FROM text_analysis_enrollments').fetchone()[0] == 0


def test_pause_replace_and_noop_projection_reconciliation(execution):
    x = execution
    enabled = x.service.command(x.http_policy(), command(x))
    first = x.dependencies.findings.state(ACCOUNT)
    x.service.reconcile_current(x.identity.owner.rebuild(x.identity.identity))
    assert x.dependencies.findings.state(ACCOUNT) == first
    original = x.service.bind_current(x.service.current_locators()[0])
    paused = x.service.command(x.http_policy(), command(x, 'pause'))
    assert x.dependencies.findings.state(ACCOUNT)['mode'] == 'paused'
    with pytest.raises(ValueError, match='stale'):
        x.service.authorize_binding(original, ACCOUNT)
    resumed = x.service.command(x.http_policy(), command(x, 'resume'))
    assert resumed.current.enrollment_id == enabled.current.enrollment_id
    replaced = x.service.command(x.http_policy(), command(x, 'replace'))
    assert replaced.current.enrollment_id != enabled.current.enrollment_id
    assert replaced.current.mode_version == paused.current.mode_version + 2
    assert x.dependencies.findings.state(ACCOUNT)['selection_epoch'] == first['selection_epoch'] + 1


def test_original_http_policy_is_rechecked_after_capture_before_commit(execution, monkeypatch):
    x = execution
    cmd = command(x)
    original = x.selection.choose
    def expire(*args):
        selected = original(*args)
        x.f.clock.now += timedelta(minutes=11)
        return selected
    monkeypatch.setattr(type(x.selection), 'choose', lambda self, *args: expire(*args))
    with pytest.raises(ValueError, match='session_unavailable'):
        x.service.command(x.http_policy(), cmd)
    with x.repository.database.read() as db:
        assert db.execute('SELECT count(*) FROM text_analysis_enrollments').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM text_analysis_enrollment_commands').fetchone()[0] == 0


def test_original_session_logout_is_not_hidden_by_new_background_epoch(execution, monkeypatch):
    x = execution
    x.service.command(x.http_policy(), command(x))
    x.service.command(x.http_policy(), command(x, 'pause'))
    resume = command(x, 'resume')
    original_policy = x.http_policy()
    original = x.service.identity.rebuild
    calls = 0
    def rebuild(identity):
        nonlocal calls
        calls += 1
        if calls == 2:
            x.auth.revoke(RevocationKey(RevocationScopeType.BRIDGE_SESSION, original_policy.identity.session_id))
        return original(identity)
    monkeypatch.setattr(x.service.identity, 'rebuild', rebuild)
    with pytest.raises(ValueError, match='session_unavailable'):
        x.service.command(original_policy, resume)
    with x.repository.database.read() as db:
        current = x.enrollments.read_current_in(db, policy=original(x.identity.identity))
        assert current.state == 'paused' and current.mode_version == 2
        assert x.enrollments.read_command_in(db, resume.command_key, policy=original(x.identity.identity)) is None


def test_expiry_after_mutation_rolls_back_command_and_enrollment(execution, monkeypatch):
    x = execution
    cmd = command(x)
    original = x.enrollments.apply_command_in
    def apply(*args, **kwargs):
        result = original(*args, **kwargs)
        x.f.clock.now += timedelta(minutes=11)
        return result
    monkeypatch.setattr(x.enrollments, 'apply_command_in', apply)
    with pytest.raises(ValueError, match='session_unavailable'):
        x.service.command(x.http_policy(), cmd)
    with x.repository.database.read() as db:
        assert db.execute('SELECT count(*) FROM text_analysis_enrollments').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM text_analysis_enrollment_commands').fetchone()[0] == 0


def test_committed_command_survives_projection_failure_and_repairs_on_retry(execution, monkeypatch):
    x = execution
    cmd = command(x)
    original = x.dependencies.findings.reconcile_enrollment
    def unavailable(*args, **kwargs):
        raise ValueError('projection_unavailable')
    monkeypatch.setattr(x.dependencies.findings, 'reconcile_enrollment', unavailable)
    with pytest.raises(ValueError, match='projection_unavailable'):
        x.service.command(x.http_policy(), cmd)
    receipt = x.service.read_command(x.http_policy(), cmd.command_key)
    assert receipt.current.enrollment_id is not None
    x.environment.engine.present = False
    monkeypatch.setattr(x.dependencies.findings, 'reconcile_enrollment', original)
    assert x.service.command(x.http_policy(), cmd) == receipt
    assert x.dependencies.findings.state(ACCOUNT)['mode'] == 'enabled'
    with x.repository.database.read() as db:
        assert db.execute('SELECT count(*) FROM text_analysis_enrollments').fetchone()[0] == 1


def test_legacy_mutations_and_delayed_callback_cannot_change_selection(execution):
    x = execution
    x.service.command(x.http_policy(), command(x))
    before = x.dependencies.findings.state(ACCOUNT)
    for action in (lambda: x.dependencies.select(ACCOUNT, x.profile.profile_id, x.profile.revision),
                   lambda: x.dependencies.select(ACCOUNT, None, None, rollback=True),
                   lambda: x.dependencies.account_mode(ACCOUNT, 'paused'),
                   lambda: x.dependencies.remove(ACCOUNT)):
        with pytest.raises(ValueError, match='authenticated_command_required'):
            action()
    op = {'id': uuid4().hex, 'kind': 'select', 'state': 'pending', 'account': ACCOUNT,
          'source_token': x.dependencies._source_token(ACCOUNT),
          'expected': x.dependencies.findings.selection_token(ACCOUNT), 'profile': x.profile.identity_digest()}
    x.dependencies._finish_selection(op)
    assert op['state'] == 'refused' and x.dependencies.findings.state(ACCOUNT) == before
    x.dependencies.remove(ACCOUNT, x.profile.profile_id, x.profile.revision)
    assert x.dependencies.findings.state(ACCOUNT) == before


def test_retained_visibility_does_not_require_engine_or_installed_profile(execution):
    x = execution
    x.service.command(x.http_policy(), command(x))
    x.service.command(x.http_policy(), command(x, 'pause'))
    x.environment.engine.present = False
    x.dependencies.persistence.removed(x.profile.identity_digest())
    x.authority.revoke(x.reference.reference_id)
    binding = x.service.bind_current(x.service.current_locators()[0])
    with x.service.retained_visibility(binding):
        assert binding.current.state == 'paused'
    x.dependencies.persistence.update(lambda state: {**state, 'withdrawals': [x.profile.identity_digest()]})
    with pytest.raises(ValueError, match='withdrawn'):
        with x.service.retained_visibility(binding):
            pytest.fail('Withdrawn output became visible')


def test_original_parameters_and_cross_account_binding_cannot_be_substituted(execution):
    x = execution
    with pytest.raises(ValueError, match='closure_mismatch'):
        execution_parameters(x.profile, '0'*64)
    with pytest.raises(ValueError, match='version_unavailable'):
        require_original_parameters(x.profile, x.parameters.model_copy(update={'parameter_sha256': '0'*64}))
    x.service.command(x.http_policy(), command(x))
    binding = x.service.bind_current(x.service.current_locators()[0])
    with pytest.raises(ValueError, match='binding_invalid'):
        x.service.authorize_binding(binding, 'other')
    with pytest.raises(ValueError):
        x.service.bind_current(binding.locator.model_copy(update={'principal_id': 'other'}))


@pytest.mark.parametrize('frozen', [False, True])
def test_non_test_and_frozen_commands_refuse_without_writes(execution, monkeypatch, frozen):
    x = execution
    cmd = command(x)
    if frozen:
        monkeypatch.setattr(sys, 'frozen', True, raising=False)
    else:
        monkeypatch.setattr(settings, 'environment', 'production')
    with pytest.raises(ValueError, match='execution_unavailable'):
        x.service.command(x.http_policy(), cmd)
    with x.repository.database.read() as db:
        assert db.execute('SELECT count(*) FROM text_analysis_enrollments').fetchone()[0] == 0


def _enabled_binding(x):
    x.service.command(x.http_policy(), command(x))
    return x.service.bind_current(x.service.current_locators()[0])


def test_binding_currentness_uses_actual_auth_without_rebuilding_or_decoding(execution, monkeypatch):
    x = execution
    binding = _enabled_binding(x)
    original = x.auth.runtime_policy_is_current_in
    observed = []
    def current(db, policy, *, now):
        observed.append(policy)
        return original(db, policy, now=now)
    def forbidden(*args, **kwargs):
        pytest.fail('Immutable binding was decoded again')
    monkeypatch.setattr(x.auth, 'runtime_policy_is_current_in', current)
    monkeypatch.setattr(x.service.identity, 'rebuild', forbidden)
    for name in ('read_locator_identity_in', 'read_capsule_in', 'read_current_in'):
        monkeypatch.setattr(x.enrollments, name, forbidden)
    for name in ('read_profile', 'canonical_bytes', 'require_original_parameters'):
        monkeypatch.setattr(execution_module, name, forbidden)
    for _ in range(3):
        x.service.authorize_binding(binding, ACCOUNT)
    assert observed == [binding.identity_policy]*3


@pytest.mark.parametrize('scope', ['PRINCIPAL', 'CREATOR_ACCOUNT', 'INSTALLATION', 'VERIFIED_GRANT'])
def test_binding_currentness_refuses_actual_revocation_and_fabricated_new_epoch(execution, scope):
    x = execution
    binding = _enabled_binding(x)
    identity = binding.capsule.identity
    values = {'PRINCIPAL': identity.principal_id, 'CREATOR_ACCOUNT': identity.creator_account_id,
              'INSTALLATION': identity.installation_id, 'VERIFIED_GRANT': identity.grant_reference_ids[0]}
    previous_epoch = x.auth.read_epoch()
    x.auth.revoke(RevocationKey(getattr(RevocationScopeType, scope), values[scope]))
    assert x.auth.read_epoch().value > previous_epoch.value
    with pytest.raises(ValueError, match='identity_unavailable'):
        x.service.authorize_binding(binding, ACCOUNT)
    manufactured = replace(binding, identity_policy=replace(binding.identity_policy, authorization_epoch=x.auth.read_epoch()))
    with pytest.raises(ValueError, match='binding_invalid'):
        x.service.authorize_binding(manufactured, ACCOUNT)


def test_binding_currentness_retains_actual_grant_expiry_check(execution):
    x = execution
    binding = _enabled_binding(x)
    epoch = x.auth.read_epoch()
    x.f.clock.now = binding.identity_policy.expires_at
    assert x.auth.read_epoch() == epoch
    with pytest.raises(ValueError, match='identity_unavailable'):
        x.service.authorize_binding(binding, ACCOUNT)


@pytest.mark.parametrize('action', ['pause', 'replace', 'retire'])
def test_binding_currentness_rechecks_exact_current_pointer_and_version(execution, action):
    x = execution
    binding = _enabled_binding(x)
    changed = x.service.command(x.http_policy(), command(x, action))
    assert changed.current.mode_version > binding.current.mode_version
    with pytest.raises(ValueError, match='locator_stale'):
        x.service.authorize_binding(binding, ACCOUNT)
    if action != 'retire':
        fresh = x.service.bind_current(x.service.current_locators()[0])
        x.service.authorize_binding(fresh, ACCOUNT)


def test_binding_currentness_rechecks_parent_state_without_pointer_change(execution):
    x = execution
    binding = _enabled_binding(x)
    with x.repository.database.transaction() as db:
        x.enrollments.jobs.set_enrollment_mode_in(db, binding.locator.enrollment_id, 'paused', policy=binding.identity_policy)
    with pytest.raises(ValueError, match='binding_changed'):
        x.service.authorize_binding(binding, ACCOUNT)


@pytest.mark.parametrize('column', ['meaning_sha256', 'identity_sha256', 'profile_bytes_sha256', 'capsule_sha256'])
def test_binding_currentness_refuses_changed_immutable_hash_lineage(execution, column):
    x = execution
    binding = _enabled_binding(x)
    parent = column == 'meaning_sha256'
    table = 'text_analysis_enrollments' if parent else 'text_analysis_enrollment_capsules'
    trigger = 'text_analysis_enrollment_immutable' if parent else 'text_analysis_capsule_immutable'
    with x.repository.database.transaction() as db:
        # A damaged store must not make a retained binding current.
        db.execute('DROP TRIGGER '+trigger)
        db.execute('UPDATE '+table+' SET '+column+'=? WHERE enrollment_id=?', ('0'*64, binding.locator.enrollment_id))
    with pytest.raises(ValueError, match='binding_changed'):
        x.service.authorize_binding(binding, ACCOUNT)


def test_binding_currentness_rejects_fabrication_foreign_owner_and_field_replacement(execution):
    x = execution
    binding = _enabled_binding(x)
    with pytest.raises(ValueError, match='binding_invalid'):
        x.service.authorize_binding(replace(binding), ACCOUNT)
    other_dependencies = x.construct_dependencies(x.source, x.dependencies.findings)
    try:
        other = x.configuration.bind(x.source, other_dependencies)
        foreign = other.bind_current(binding.locator)
        with pytest.raises(ValueError, match='binding_invalid'):
            x.service.authorize_binding(foreign, ACCOUNT)
    finally:
        assert other_dependencies.close()
    object.__setattr__(binding, 'identity_policy', replace(binding.identity_policy, identity=replace(
        binding.identity_policy.identity, role='operator')))
    with pytest.raises(ValueError, match='binding_invalid'):
        x.service.authorize_binding(binding, ACCOUNT)


def test_binding_registry_does_not_retain_discarded_bindings(execution):
    x = execution
    binding = _enabled_binding(x)
    key, reference = id(binding), weakref.ref(binding)
    assert key in x.service._bindings
    del binding
    gc.collect()
    assert reference() is None and key not in x.service._bindings


def test_binding_currentness_refuses_replaced_canonical_store_identity(execution):
    x = execution
    binding = _enabled_binding(x)
    with x.repository.database.transaction() as db:
        db.execute('UPDATE knowledge_store_identity SET store_id=? WHERE singleton=1', ('0'*32,))
    with pytest.raises(ValueError, match='binding_changed'):
        x.service.authorize_binding(binding, ACCOUNT)


@pytest.mark.parametrize('change', ['revocation', 'expiry'])
def test_retained_visibility_rechecks_authority_after_acquiring_writer(execution, monkeypatch, change):
    x = execution
    binding = _enabled_binding(x)
    original = x.dependencies.persistence.recovery_visibility
    @contextmanager
    def after_wait(*args, **kwargs):
        if change == 'revocation':
            x.auth.revoke(RevocationKey(RevocationScopeType.PRINCIPAL, binding.capsule.identity.principal_id))
        else:
            x.f.clock.now = binding.identity_policy.expires_at
        with original(*args, **kwargs) as barrier:
            yield barrier
    monkeypatch.setattr(x.dependencies.persistence, 'recovery_visibility', after_wait)
    entered = False
    with pytest.raises(ValueError, match='identity_unavailable'):
        with x.service.retained_visibility(binding):
            entered = True
    assert not entered


def test_retained_visibility_refuses_revocation_committed_during_actual_writer_wait(execution, monkeypatch):
    x = execution
    binding = _enabled_binding(x)
    writer = x.auth.database.transaction
    attempted, acquired = Event(), Event()
    entered, errors = [], []
    @contextmanager
    def observed_writer(*args, **kwargs):
        attempted.set()
        with writer(*args, **kwargs) as db:
            acquired.set()
            yield db
    def publish():
        try:
            with x.service.retained_visibility(binding):
                entered.append(True)
        except BaseException as error:
            errors.append(error)
    thread = Thread(target=publish, name='retained-visibility-check')
    try:
        with writer() as db:
            monkeypatch.setattr(x.auth.database, 'transaction', observed_writer)
            thread.start()
            assert attempted.wait(5)
            assert not acquired.is_set()
            x.auth._revoke_in_transaction(db, RevocationKey(RevocationScopeType.PRINCIPAL,
                binding.capsule.identity.principal_id), reason='revoked')
            x.auth._increment_authorization_epoch(db)
    finally:
        if thread.ident is not None:
            thread.join(10)
    assert not thread.is_alive()
    assert acquired.is_set() and not entered
    assert len(errors) == 1 and isinstance(errors[0], RuntimeAuthorizationDenied)
    assert str(errors[0]) == 'text_analysis_identity_unavailable'
