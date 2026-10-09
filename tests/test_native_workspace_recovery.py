"""Exact launcher-authorized workspace renewal on real SQLCipher storage."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.onboarding import OnboardingJourneyStore, OnboardingJourneyUnavailable
from app.provisioning.app import create_provisioning_app
from app.provisioning.events import OnboardingEvents
from app.provisioning.session import (
    NATIVE_ENTRY_COOKIE_NAME, PROVISIONING_ORIGIN, PROVISIONING_SESSION_COOKIE_NAME,
    ProvisioningSessionManager,
)
from test_onboarding_enrollment_runtime import count
from test_webauthn_routes import INSTANT

pytestmark = [pytest.mark.ci_tier("integration")]
PATH = "/api/v1/provisioning/native-entry"
TOKEN = "t" * 32


@pytest.fixture
def owner(tmp_path):
    clock = [INSTANT]
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: clock[0])
    journeys = OnboardingJourneyStore(store)
    sessions = ProvisioningSessionManager(TOKEN, journeys=journeys,
        monotonic=lambda: clock[0].timestamp())
    app = create_provisioning_app(claim_submission=lambda **_: None,
        creator_association_initiation=lambda **_: None, creator_binding_acquisition=lambda: None,
        completion_ready=lambda: False, finalize_action=lambda **_: None, session_manager=sessions)
    with TestClient(app, base_url=PROVISIONING_ORIGIN) as browser:
        yield SimpleNamespace(clock=clock, store=store, journeys=journeys, sessions=sessions, browser=browser)


def entry(owner, *, targeted=None, session=None):
    native = owner.sessions.redeem_native_entry(owner.sessions.issue_native_entry(
        "Provisioning " + TOKEN, journey_id=targeted))
    cookie = f"{NATIVE_ENTRY_COOKIE_NAME}={native.identifier}"
    if session:
        cookie += f"; {PROVISIONING_SESSION_COOKIE_NAME}={session}"
    headers = {"Cookie": cookie}
    context = owner.browser.get(PATH, headers=headers).json()
    headers.update({"Origin": PROVISIONING_ORIGIN, "X-Provisioning-CSRF": context["csrf_token"]})
    return native, headers


def renew(owner, prior, headers):
    return owner.browser.post(PATH, json={"journey_id": prior, "recover": True}, headers=headers)


def browser_request(identifier):
    return Request({"type": "http", "headers": [(b"host", b"bridge.localhost:17871"),
        (b"cookie", f"{PROVISIONING_SESSION_COOKIE_NAME}={identifier}".encode())]})


@pytest.mark.parametrize("prior_state", ["current", "expired", "missing"])
def test_targeted_native_context_exposes_only_issued_binding_for_desktop_recovery(owner, prior_state):
    prior = str(uuid4()) if prior_state == "missing" else owner.journeys.open()["journey_id"]
    if prior_state == "expired":
        owner.clock[0] += timedelta(minutes=31)
    native, headers = entry(owner, targeted=prior)
    context = owner.browser.get(PATH, headers=headers).json()
    assert set(context) == {"state", "csrf_token", "entry_id", "target_journey_id"}
    assert context["state"] == "select_workspace"
    assert context["target_journey_id"] == prior and context["entry_id"] == native.entry_id
    result = renew(owner, context["target_journey_id"], headers)
    if prior_state == "missing":
        assert result.status_code == 409 and native.consumed is False
        assert count(owner.store, "onboarding_journeys") == count(owner.store, "provisioning_browser_sessions") == 0
    else:
        assert result.status_code == 200 and result.json()["previous_journey_id"] == prior
        assert (result.json()["journey_id"] == prior) is (prior_state == "current")
        assert count(owner.store, "onboarding_journeys") == count(owner.store, "provisioning_browser_sessions") == 1


def test_untargeted_native_context_does_not_invent_a_prior_workspace(owner):
    _, headers = entry(owner)
    context = owner.browser.get(PATH, headers=headers).json()
    assert set(context) == {"state", "csrf_token", "entry_id"}
    assert context["state"] == "select_workspace"
    assert count(owner.store, "onboarding_journeys") == count(owner.store, "provisioning_browser_sessions") == 0


def test_expired_empty_workspace_is_replaced_once_and_lost_reply_uses_exact_read(owner):
    prior = owner.journeys.open()["journey_id"]
    owner.clock[0] += timedelta(minutes=31)
    _, headers = entry(owner, targeted=prior)
    result = renew(owner, prior, headers)
    assert result.status_code == 200
    selected = result.json()
    assert set(selected) == {"state", "journey_id", "previous_journey_id"}
    assert selected["state"] == "selected" and selected["previous_journey_id"] == prior
    assert selected["journey_id"] != prior
    session = result.cookies[PROVISIONING_SESSION_COOKIE_NAME]
    assert owner.browser.get(PATH, headers=headers).json() == {"state": "unconfirmed"}
    confirmed = {**headers, "Cookie": headers["Cookie"] + f"; {PROVISIONING_SESSION_COOKIE_NAME}={session}"}
    assert owner.browser.get(PATH, headers=confirmed).json() == selected
    assert renew(owner, prior, confirmed).status_code == 409
    assert count(owner.store, "onboarding_journeys") == count(owner.store, "provisioning_browser_sessions") == 1
    assert owner.journeys.require_current(selected["journey_id"])["expires_at"] == (INSTANT+timedelta(minutes=60)).isoformat()

    # Another explicitly issued native entry resolves the durable same mapping.
    _, retry_headers = entry(owner, targeted=prior, session=session)
    retried = renew(owner, prior, retry_headers)
    assert retried.status_code == 200 and retried.json() == selected
    assert retried.cookies[PROVISIONING_SESSION_COOKIE_NAME] == session
    assert count(owner.store, "onboarding_journeys") == count(owner.store, "provisioning_browser_sessions") == 1


def test_fresh_native_entries_without_a_cookie_reuse_one_durable_successor(owner):
    prior = owner.journeys.open()["journey_id"]
    owner.clock[0] += timedelta(minutes=31)
    _, first = entry(owner, targeted=prior)
    _, second = entry(owner, targeted=prior)
    a, b = renew(owner, prior, first), renew(owner, prior, second)
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    assert count(owner.store, "onboarding_journeys") == 1


def test_recovery_cannot_retarget_a_live_unrelated_session(owner):
    prior = owner.journeys.open()["journey_id"]
    unrelated = owner.journeys.open(str(uuid4()))["journey_id"]
    session = owner.sessions.redeem_handoff_code(owner.sessions.issue_handoff_code(
        "Provisioning " + TOKEN, journey_id=unrelated))
    native, headers = entry(owner, targeted=prior, session=session.identifier)
    result = renew(owner, prior, headers)
    assert result.status_code == 409 and "set-cookie" not in result.headers
    assert native.consumed is False
    assert owner.sessions.require_session(browser_request(session.identifier)).journey_id == unrelated
    assert count(owner.store, "onboarding_journeys") == 2


@pytest.mark.parametrize("change", ["wrong-target", "origin", "csrf", "expired", "integer", "false", "extra"])
def test_recovery_refusals_create_no_successor_or_session(owner, change):
    prior = owner.journeys.open()["journey_id"]
    owner.clock[0] += timedelta(minutes=31)
    native, headers = entry(owner, targeted=prior)
    body = {"journey_id": prior, "recover": True}
    if change == "wrong-target": body["journey_id"] = str(uuid4())
    if change == "origin": headers["Origin"] = "https://unrelated.example"
    if change == "csrf": headers["X-Provisioning-CSRF"] = "wrong"
    if change == "expired": owner.clock[0] += timedelta(minutes=5)
    if change == "integer": body["recover"] = 1
    if change == "false": body["recover"] = False
    if change == "extra": body["operation_id"] = "untrusted"
    result = owner.browser.post(PATH, json=body, headers=headers)
    assert result.status_code in {401, 403, 409, 422}
    assert "set-cookie" not in result.headers
    assert native.consumed is False
    assert count(owner.store, "provisioning_browser_sessions") == 0
    assert count(owner.store, "onboarding_journeys") == 1


@pytest.mark.parametrize("recover", [False, True])
def test_native_expiry_while_waiting_for_write_lock_commits_no_draft_or_session(owner, monkeypatch, recover):
    prior = owner.journeys.open()["journey_id"] if recover else str(uuid4())
    if recover: owner.clock[0] += timedelta(minutes=31)
    native, headers = entry(owner, targeted=prior)
    transaction = owner.store.database.transaction
    @contextmanager
    def delayed_transaction(*args, **kwargs):
        with transaction(*args, **kwargs) as connection:
            owner.clock[0] += timedelta(minutes=5)
            yield connection
    monkeypatch.setattr(owner.store.database, "transaction", delayed_transaction)
    body = {"journey_id": prior, **({"recover": True} if recover else {})}
    result = owner.browser.post(PATH, json=body, headers=headers)
    assert result.status_code == 401
    assert native.consumed is False
    assert count(owner.store, "provisioning_browser_sessions") == 0
    assert count(owner.store, "onboarding_journeys") == int(recover)
    assert count(owner.store, "onboarding_workspace_recovery") == 0


def test_cached_session_requires_surviving_durable_record(owner):
    session = owner.sessions.redeem_handoff_code(owner.sessions.issue_handoff_code("Provisioning " + TOKEN))
    assert owner.sessions.require_session(browser_request(session.identifier)) == session
    with owner.store.database.transaction() as connection:
        connection.execute("DELETE FROM provisioning_browser_sessions")
    with pytest.raises(HTTPException) as refused:
        owner.sessions.require_session(browser_request(session.identifier))
    assert refused.value.status_code == 401


def test_one_native_entry_serializes_competing_normal_and_recovery_selection(owner):
    prior = owner.journeys.open()["journey_id"]
    _, headers = entry(owner, targeted=prior)
    request = Request({"type": "http", "headers": [(b"host", b"bridge.localhost:17871"),
        *((name.lower().encode(), value.encode()) for name, value in headers.items())]})
    gate = Barrier(2)
    def select(recover):
        gate.wait(timeout=2)
        try:
            return owner.sessions.select_native_workspace(request, prior, recover=recover)
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(select, False), pool.submit(select, True)
        results = [a.result(timeout=5), b.result(timeout=5)]
    assert sum(value == 409 for value in results) == 1
    assert count(owner.store, "onboarding_journeys") == count(owner.store, "provisioning_browser_sessions") == 1


def test_recovery_deadline_crossed_at_final_commit_rolls_back_successor_and_session(owner, monkeypatch):
    prior = owner.journeys.open()["journey_id"]
    owner.clock[0] += timedelta(minutes=59)
    native, headers = entry(owner, targeted=prior)
    renew_session = owner.journeys.renew_native_session
    def crosses_deadline(**arguments):
        authorize = arguments.pop("authorize")
        checks = [0]
        def check():
            checks[0] += 1
            if checks[0] == 2:
                owner.clock[0] += timedelta(minutes=1)
            authorize()
        return renew_session(**arguments, authorize=check)
    monkeypatch.setattr(owner.journeys, "renew_native_session", crosses_deadline)
    result = renew(owner, prior, headers)
    assert result.status_code == 409 and native.consumed is False
    assert count(owner.store, "onboarding_journeys") == 1
    assert count(owner.store, "onboarding_workspace_recovery") == count(owner.store, "provisioning_browser_sessions") == 0


def test_missing_legacy_scope_never_chooses_an_unrelated_receipt(owner):
    with owner.store.database.transaction() as connection:
        connection.execute("INSERT INTO onboarding_uncertain_receipts VALUES (?,?,?,?,?)",
            ("legacy-installation", "legacy-operation", "{}", INSTANT.isoformat(),
             (INSTANT+timedelta(minutes=30)).isoformat()))
    with pytest.raises(OnboardingJourneyUnavailable):
        owner.journeys.open()
    prior = str(uuid4())
    _, headers = entry(owner, targeted=prior)
    assert renew(owner, prior, headers).status_code == 409
    assert count(owner.store, "onboarding_journeys") == count(owner.store, "provisioning_browser_sessions") == 0


@pytest.mark.parametrize("state", ["waiting", "revoked", "reauthentication-required", "completed", "transfer", "transfer-proof"])
def test_expired_unconfirmed_or_transfer_scope_cannot_become_fresh_setup(owner, state):
    row = owner.journeys.open()
    if state in {"transfer", "transfer-proof"}:
        with owner.store.database.transaction() as connection:
            connection.execute("INSERT INTO onboarding_transfer_intents (journey_id,request_json,previous_state,expires_at) VALUES (?,?,?,?)",
                (row["journey_id"], "{}", "new", (INSTANT+timedelta(minutes=30)).timestamp()))
            if state == "transfer-proof":
                connection.execute("INSERT INTO onboarding_transfer_proofs VALUES (?,?)", (row["journey_id"], "d" * 64))
                # Another transfer can prune an expired intent before the old
                # journey is retired. Its issued-proof trace still survives.
                connection.execute("DELETE FROM onboarding_transfer_intents WHERE journey_id=?", (row["journey_id"],))
    else:
        owner.journeys.update(row["journey_id"], state=state)
    owner.clock[0] += timedelta(minutes=31)
    assert owner.journeys.get(row["journey_id"]) is None
    with owner.store.database.read() as connection:
        saved = connection.execute("SELECT state FROM onboarding_workspace_recovery WHERE previous_journey_id=?",
                                   (row["journey_id"],)).fetchone()
    assert saved["state"] == ("transfer" if state == "transfer-proof" else state)
    _, headers = entry(owner, targeted=row["journey_id"])
    assert renew(owner, row["journey_id"], headers).status_code == 409
    assert count(owner.store, "onboarding_journeys") == count(owner.store, "provisioning_browser_sessions") == 0


@pytest.mark.parametrize("state", ["unknown", "completing"])
def test_exact_completion_receipt_survives_an_older_transfer_proof(owner, state):
    row = owner.journeys.open()
    operation = owner.journeys.new_operation()
    scope = json.dumps({"installation_id": row["installation_id"]})
    owner.journeys.update(row["journey_id"], state=state, operation_id=operation, scope_json=scope)
    with owner.store.database.transaction() as connection:
        connection.execute("INSERT INTO onboarding_transfer_proofs VALUES (?,?)", (row["journey_id"], "d" * 64))
    owner.clock[0] += timedelta(minutes=31)
    _, headers = entry(owner, targeted=row["journey_id"])
    result = renew(owner, row["journey_id"], headers)
    assert result.status_code == 200
    saved = owner.journeys.require_current(result.json()["journey_id"])
    assert saved["state"] == "unknown"
    assert saved["operation_id"] == operation and saved["scope_json"] == scope
    assert saved["installation_id"] == row["installation_id"]
    assert saved["handoff_reference"] is None and saved["prepare_json"] is None
    assert saved["recovery_deadline"] == (INSTANT + timedelta(minutes=60)).isoformat()


@pytest.mark.asyncio
async def test_expired_listener_is_not_focusable_before_next_keepalive(owner):
    session = owner.sessions.redeem_handoff_code(owner.sessions.issue_handoff_code("Provisioning " + TOKEN))
    request = browser_request(session.identifier)
    events = OnboardingEvents()
    stream = events.stream(lambda: events.snapshot(journey_id=session.journey_id, facts={}),
        lambda: owner.sessions.require_session(request), journey_id=session.journey_id,
        expires_in=lambda: owner.sessions.session_remaining(request))
    await anext(stream)
    assert events.request_focus(session.journey_id) == session.journey_id
    owner.clock[0] += timedelta(minutes=30)
    assert events.request_focus(session.journey_id) is None
    with pytest.raises(HTTPException):
        await asyncio.wait_for(anext(stream), 1)
    await stream.aclose()


def test_recovery_migration_is_packaged_at_its_exact_digest(owner):
    root = Path(__file__).parents[1]
    migration = root / "app/persistence/auth_sql/0022_onboarding_workspace_recovery.sql"
    policy = json.loads((root / "packaging/runtime-files.json").read_text())
    digest = hashlib.sha256(migration.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    assert digest in json.dumps(policy)
    with owner.store.database.read() as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(onboarding_workspace_recovery)")]
    assert columns == ["previous_journey_id", "installation_id", "operation_id", "state", "scope_json",
                       "prepare_json", "retired_at", "expires_at", "resolved_journey_id"]


def test_recovery_migration_preserves_existing_auth_ledger_and_journey_rows(tmp_path):
    catalog = tmp_path / "previous-auth-schema"
    catalog.mkdir()
    sources = Path(__file__).parents[1] / "app/persistence/auth_sql"
    for migration in sources.glob("*.sql"):
        if int(migration.name[:4]) <= 21:
            shutil.copy2(migration, catalog / migration.name)
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", migrations_dir=catalog, clock=lambda: INSTANT)
    states = ("new", "preparing", "prepare-unknown", "waiting", "completing", "unknown",
              "completed", "expired", "revoked", "reauthentication-required")
    with store.database.transaction() as connection:
        for state in states:
            connection.execute("INSERT INTO onboarding_journeys (journey_id,created_at,expires_at,installation_id,state) VALUES (?,?,?,?,?)",
                (str(uuid4()), INSTANT.isoformat(), (INSTANT + timedelta(minutes=30)).isoformat(), str(uuid4()), state))
        ledger = [tuple(row) for row in connection.execute("SELECT * FROM schema_migrations ORDER BY version")]
        journeys = [tuple(row) for row in connection.execute("SELECT * FROM onboarding_journeys ORDER BY journey_id")]
    current = SQLiteAuthenticationStore(store.database.path, clock=lambda: INSTANT)
    with current.database.read() as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM schema_migrations ORDER BY version")][:21] == ledger
        rows = connection.execute("SELECT * FROM onboarding_journeys ORDER BY journey_id").fetchall()
        assert [tuple(row)[:-1] for row in rows] == journeys
        assert {row["kind"] for row in rows} == {"initial-enrollment"}
        assert connection.execute("SELECT count(*) FROM onboarding_workspace_recovery").fetchone()[0] == 0
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 23
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # Every previously legal state can be retired without breaking other cleanup.
    current._clock = lambda: INSTANT + timedelta(minutes=31)
    assert OnboardingJourneyStore(current).get(journeys[0][0]) is None
    with current.database.read() as connection:
        assert {row[0] for row in connection.execute("SELECT state FROM onboarding_workspace_recovery")} == set(states)
