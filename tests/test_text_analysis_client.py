"""Real-session acknowledgements remain nonexecuting and actor-bound."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import hashlib
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from app.api.endpoints import text_analysis_client
from app.models.text_analysis_client import ClientIntentPut, ClientIntentCursor
from app.models.text_analysis_enrollment import EnrollmentCommand
from app.nlp.profiles import canonical_bytes
from app.persistence.contributions import storage_high_water
from app.persistence.retention import CreatorVaultRetention
from app.services.text_analysis_client import (
    FixtureTextAnalysisClientConfiguration,
    configure_client_service,
    client_service,
)
from tests.continuous_analytics_fixture import ACCOUNT
from tests.text_analysis_execution_fixture import execution_http_client
from tests.text_analysis_preview_fixture import (
    make_preview_fixture,
    close_preview_fixture,
    request,
    prepare_preview,
)

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]
PATH = '/api/v1/nlp/client-intents'


@pytest.fixture
def data(tmp_path):
    x = make_preview_fixture(tmp_path)
    x.client = FixtureTextAnalysisClientConfiguration(
        x.auth, x.repository, x.pair_owner
    ).bind(x.source, x.dependencies)
    try:
        yield x
    finally:
        close_preview_fixture(x)


def intent(x, kind='preview_prepare', key=None):
    key = key or str(uuid4())
    if kind == 'enrollment_command':
        body = EnrollmentCommand(
            command_key=key,
            expected_enrollment_id=None,
            expected_mode_version=0,
            action='enable',
            expected_profile_sha256=x.profile.identity_digest(),
        )
    else:
        body = request(x, key)
    return ClientIntentPut(kind=kind, key=key, body=body)


@contextmanager
def http(x):
    app = FastAPI()
    app.include_router(text_analysis_client.router)
    assert client_service() is None
    configure_client_service(x.client)
    try:
        with execution_http_client(x, service=x.license_service, app=app) as client:
            yield client
    finally:
        configure_client_service(None)


@pytest.mark.parametrize(
    'kind', ['enrollment_command', 'preview_prepare', 'preview_confirm']
)
def test_real_http_ack_is_durable_exact_and_never_dispatches(data, kind):
    x = data
    if kind == 'preview_confirm':
        body, prepared = prepare_preview(x, confirm=False)
        from app.models.text_analysis_client import PreviewConfirmIntent

        proposed = ClientIntentPut(
            kind=kind,
            key=body.external_key,
            body=PreviewConfirmIntent(
                external_key=body.external_key, confirmation=prepared.confirmation
            ),
        )
    else:
        proposed = intent(x, kind)
    with http(x) as client:
        response = client.post(PATH, json=proposed.model_dump(mode='json'))
        assert response.status_code == 200, response.text
        acknowledged = response.json()
        assert acknowledged['body'] == proposed.body.model_dump(mode='json')
        assert (
            acknowledged['body_sha256']
            == hashlib.sha256(canonical_bytes(proposed.body)).hexdigest()
        )
        assert client.get(PATH + '/' + kind + '/' + proposed.key).json() == acknowledged
        assert (
            client.post(PATH, json=proposed.model_dump(mode='json')).json()
            == acknowledged
        )
    with x.repository.database.read() as db:
        assert (
            db.execute('SELECT count(*) FROM text_analysis_client_intents').fetchone()[
                0
            ]
            == 1
        )
        assert db.execute('SELECT count(*) FROM knowledge_jobs').fetchone()[0] == int(
            kind == 'preview_confirm'
        )
        assert not db.execute('SELECT 1 FROM knowledge_attempts').fetchone()
        assert not db.execute('SELECT 1 FROM text_analysis_preview_outputs').fetchone()
        assert (
            db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[
                0
            ]
            == 'available'
        )


def test_retained_ack_survives_pair_loss_but_new_ack_requires_coherence(data):
    x = data
    proposed = intent(x)
    original = x.client.retain(x.http_policy(), proposed)
    with x.auth.database.transaction() as db:
        db.execute('UPDATE authoritative_pair SET valid=0')
    assert x.client.retain(x.http_policy(), proposed) == original
    assert x.client.read(x.http_policy(), proposed.kind, proposed.key) == original
    assert x.client.list(x.http_policy()).items == (original,)
    with pytest.raises(ValueError):
        x.client.retain(x.http_policy(), intent(x))
    with pytest.raises(ValueError, match='conflict'):
        changed = proposed.model_copy(
            update={
                'body': proposed.body.model_copy(
                    update={'expected_profile_sha256': 'f' * 64}
                )
            }
        )
        x.client.retain(x.http_policy(), changed)


def test_keyset_pages_are_bounded_and_rows_are_immutable_lifetime_metadata(data):
    x = data
    proposed = [
        intent(x, 'enrollment_command' if i % 2 else 'preview_prepare')
        for i in range(67)
    ]
    with x.repository.database.transaction() as db:
        baseline = x.identity.owner.rebuild(x.identity.identity)
        before = storage_high_water(db)
        for value in proposed:
            x.client.store.retain_in(db, value, policy=baseline)
        assert storage_high_water(db) != before
    first = x.client.list(x.http_policy())
    second = x.client.list(x.http_policy(), after=first.next_after)
    assert (
        len(first.items) == 64 and len(second.items) == 3 and second.next_after is None
    )
    assert [(r.kind, r.key) for r in (*first.items, *second.items)] == sorted(
        (r.kind, r.key) for r in proposed
    )
    for sql in (
        'DELETE FROM text_analysis_client_intents',
        'UPDATE text_analysis_client_intents SET retained_at=retained_at',
    ):
        with pytest.raises(Exception, match='immutable|lifetime'):
            with x.repository.database.transaction() as db:
                db.execute(sql)
    CreatorVaultRetention(
        x.repository.database, clock=lambda: x.f.clock.now
    ).delete_all(ACCOUNT)
    assert len(x.client.list(x.http_policy()).items) == 64


def test_capacity_failure_rolls_back_even_if_owned_transaction_catches_it(
    data, monkeypatch
):
    x = data
    baseline = x.identity.owner.rebuild(x.identity.identity)
    with x.repository.database.transaction() as db:
        monkeypatch.setattr(
            x.repository, 'max_storage_bytes', x.repository._storage(db) + 1
        )
        with pytest.raises(ValueError, match='storage_full'):
            x.client.store.retain_in(db, intent(x), policy=baseline)
        assert not db.execute('SELECT 1 FROM text_analysis_client_intents').fetchone()


def test_installation_limit_refuses_without_eviction(data):
    x = data
    first = intent(x)
    x.client.retain(x.http_policy(), first)
    with x.repository.database.transaction() as db:
        row = db.execute('SELECT * FROM text_analysis_client_intents').fetchone()
        for number in range(4095):
            value = intent(x, key=str(UUID(int=number, version=4)))
            raw = canonical_bytes(value.body)
            db.execute(
                'INSERT INTO text_analysis_client_intents VALUES(?,?,?,?,?,?,?,?,?)',
                (
                    row['installation_id'],
                    row['organization_id'],
                    row['creator_account_id'],
                    row['principal_id'],
                    value.kind,
                    value.key,
                    raw,
                    hashlib.sha256(raw).hexdigest(),
                    row['retained_at'],
                ),
            )
    with pytest.raises(ValueError, match='client_capacity'):
        x.client.retain(x.http_policy(), intent(x))
    assert x.client.read(x.http_policy(), first.kind, first.key).body == first.body


def test_new_principal_cannot_adopt_original_actor_journal(data):
    from tests.test_text_analysis_views import reenroll

    x = data
    proposed = intent(x)
    x.client.retain(x.http_policy(), proposed)
    policy = reenroll(x)
    assert x.client.list(policy).items == ()
    with pytest.raises(ValueError, match='actor_conflict'):
        x.client.read(policy, proposed.kind, proposed.key)
    with pytest.raises(ValueError, match='actor_conflict'):
        x.client.retain(policy, proposed)


def test_closed_kind_body_and_cursor_rejection(data):
    x = data
    with pytest.raises(ValueError):
        ClientIntentPut(kind='preview_confirm', key=str(uuid4()), body=request(x))
    with http(x) as client:
        assert client.get(PATH + '?after_kind=preview_prepare').status_code == 409
        assert client.get(PATH + '?limit=65').status_code == 422
        assert client.get(PATH + '/preview_prepare/' + str(uuid4())).status_code == 404
        raw = intent(x).model_dump(mode='json')
        raw['caller_account'] = 'other'
        assert client.post(PATH, json=raw).status_code == 422


def test_expired_session_after_actual_canonical_writer_wait_cannot_retain(
    data, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    x = data
    proposed = intent(x)
    policy = x.http_policy()
    attempted = Event()
    original = x.repository.database.transaction

    @contextmanager
    def waiting(*args, **kwargs):
        attempted.set()
        with original(*args, **kwargs) as db:
            yield db

    with ThreadPoolExecutor(max_workers=1) as pool:
        with original():
            monkeypatch.setattr(x.repository.database, 'transaction', waiting)
            future = pool.submit(x.client.retain, policy, proposed)
            assert attempted.wait(3)
            x.f.clock.now += timedelta(days=91)
        with pytest.raises(ValueError):
            future.result(timeout=5)
    with x.repository.database.read() as db:
        assert not db.execute('SELECT 1 FROM text_analysis_client_intents').fetchone()


def test_released_actual_root_lease_refuses_recovery_without_pair_coherence_dependency(
    data,
):
    from app.persistence.authoritative_pair import (
        AuthoritativePairOwner,
        RuntimeRootLease,
        RUNTIME_LOCK_FILENAME,
    )
    from app.persistence.migrations import InstallationMigrationLock
    from app.services.text_analysis_client import TextAnalysisClientService

    x = data
    proposed = intent(x)
    with InstallationMigrationLock(x.pair_owner.root / RUNTIME_LOCK_FILENAME) as lock:
        pair = AuthoritativePairOwner(
            x.auth,
            x.repository.database,
            installation_id=x.context.installation_id,
            root_lease=RuntimeRootLease.from_held_lock(x.pair_owner.root, lock),
        )
        client = TextAnalysisClientService(x.auth, x.repository, pair, x.dependencies)
        original = client.retain(x.http_policy(), proposed)
        with x.auth.database.transaction() as db:
            db.execute('UPDATE authoritative_pair SET valid=0')
        assert client.read(x.http_policy(), proposed.kind, proposed.key) == original
        assert client.retain(x.http_policy(), proposed) == original
    for operation in (
        lambda: client.read(x.http_policy(), proposed.kind, proposed.key),
        lambda: client.list(x.http_policy()),
        lambda: client.retain(x.http_policy(), proposed),
    ):
        with pytest.raises(ValueError, match='authoritative_pair_unavailable'):
            operation()
    with x.repository.database.read() as db:
        assert (
            db.execute('SELECT count(*) FROM text_analysis_client_intents').fetchone()[
                0
            ]
            == 1
        )


def test_client_page_rejects_duplicates_order_and_invented_cursor(data):
    from app.models.text_analysis_client import ClientIntentPage

    x = data
    values = sorted(
        [x.client.retain(x.http_policy(), intent(x)) for _ in range(2)],
        key=lambda item: (item.kind, item.key),
    )
    for items, cursor in (
        ((values[0], values[0]), None),
        ((values[1], values[0]), None),
        ((values[0],), ClientIntentCursor(kind=values[1].kind, key=values[1].key)),
    ):
        with pytest.raises(ValueError, match='page_invalid'):
            ClientIntentPage(items=items, next_after=cursor)
