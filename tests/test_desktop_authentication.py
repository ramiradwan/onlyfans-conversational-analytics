import copy
import json
from types import SimpleNamespace

import pytest

from app.onboarding.authentication import DesktopAuthentication
from app.onboarding.browser_tasks import BrowserTasks
from app.onboarding.coordinator import CommandRefused
from app.security.browser_authentication import PROFILE
from app.security.browser_ceremony import binding_digest
from app.security.hosted_grants import HostedGrantUnavailable
from test_desktop_ceremonies import authorized, setup as setup
from test_desktop_coordinator import command

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


class AuthenticationClient:
    transport = SimpleNamespace(close=lambda: None)

    def __init__(self):
        self.calls = []
        self.attempts = {}
        self.current = None
        self.lose_selection_reply = False
        self.preparation_recovery = False

    def read(self, request, *, before_send, expires_at):
        before_send()
        self.calls.append(("read", copy.deepcopy(request)))
        selected = request["authentication_attempt_id"] or self.current
        attempt = self.attempts.get(selected)
        state = "selection-required" if attempt is None else attempt["state"]
        return {"profile": PROFILE, "operation_id": request["operation_id"],
            "ceremony_id": request["ceremony_id"], "binding_digest": binding_digest(request["binding"]),
            "expires_at": expires_at, "state": state,
            "choices": dict.fromkeys(("google", "microsoft-personal", "default"), True),
            "actions": {"select": state in {"selection-required", "retired"}, "cancel": state == "selected",
                "open_browser": state == "selected" or state == "admitted" and self.preparation_recovery,
                "switch_account": state == "admitted"},
            "authentication_attempt": None if attempt is None else {name: attempt[name] for name in (
                "authentication_attempt_id", "provider", "mode")}}

    def execute(self, verb, request, *, before_send, expires_at):
        before_send()
        self.calls.append((verb, copy.deepcopy(request)))
        identifier = request["authentication_attempt_id"]
        if verb == "cancel":
            self.attempts[identifier]["state"] = "retired"
        else:
            self.attempts[identifier] = {"authentication_attempt_id": identifier, "provider": request["provider"],
                "mode": "switch-account" if verb == "switch" else "sign-in", "state": "selected"}
            self.current = identifier
            if self.lose_selection_reply:
                raise OSError("Unconfirmed selection")
        return self.read(request, before_send=before_send, expires_at=expires_at)


@pytest.fixture
async def auth(setup):
    coordinator, ceremonies, _, parent, binding, clock = setup
    await ceremonies.prepare(parent["operation_id"], binding, authorize=authorized)
    client = AuthenticationClient()
    authentication = DesktopAuthentication(ceremonies.store, ceremonies, client,
        providers=("google", "microsoft-personal", "default"))
    ceremonies.authentication = authentication
    native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    await authentication.refresh(parent["operation_id"], authorize=authorized)
    return coordinator, ceremonies, authentication, client, parent, native, clock


def accept(auth, action="auth.select", provider="google"):
    coordinator, _, authentication, _, parent, native, _ = auth
    coordinator.owner.scope["desktop_authentication"] = authentication.presentation(parent["journey_id"], "account.connect")
    value = command(coordinator.read(native), action)
    if action != "auth.cancel":
        value["provider"] = provider
    operation, fresh = coordinator._accept(native, value)
    assert fresh
    return operation, value


@pytest.mark.asyncio
async def test_choice_is_durable_before_network_and_immutable_on_duplicate(auth):
    coordinator, _, _, client, parent, native, _ = auth
    before = copy.deepcopy(client.calls)
    operation, request = accept(auth)
    intent = json.loads(operation["owner_scope_json"])["authentication_intent"]
    assert intent["provider"] == "google" and intent["parent_operation_id"] == parent["operation_id"]
    assert client.calls == before
    assert coordinator._accept(native, request) == (operation, False)
    with pytest.raises(CommandRefused) as refused:
        coordinator._accept(native, {**request, "provider": "microsoft-personal"})
    assert refused.value.reason == "operation_conflict"


@pytest.mark.asyncio
async def test_lost_selection_reply_restart_reads_exact_child_without_resending(auth):
    _, ceremonies, authentication, client, parent, _, _ = auth
    operation, _ = accept(auth)
    client.lose_selection_reply = True
    with pytest.raises(OSError):
        await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    reopened = DesktopAuthentication(ceremonies.store, ceremonies, client)
    result = await reopened.execute(operation, authorize=lambda: pytest.fail("mutation after restart"),
        read_guard=authorized, reconcile=True)
    assert result.state == "waiting"
    assert [verb for verb, _ in client.calls].count("select") == 1
    exact = json.loads(operation["owner_scope_json"])["authentication_intent"]["authentication_attempt_id"]
    assert any(verb == "read" and request["authentication_attempt_id"] == exact for verb, request in client.calls)
    assert reopened.parent_operation(operation["operation_id"]) == parent["operation_id"]


@pytest.mark.asyncio
async def test_cancellation_fences_old_browser_task_and_retry_keeps_original_ceremony(auth):
    coordinator, ceremonies, authentication, client, parent, _, _ = auth
    operation, _ = accept(auth)
    assert (await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)).state == "waiting"
    entry = ceremonies.browser_entry(operation["operation_id"])
    assert entry is not None and entry["ceremony_id"] == ceremonies.row(parent["operation_id"])["ceremony_id"]
    assert "?" not in entry["entry_url"]
    assert authentication.presentation(parent["journey_id"], "account.connect")["browser_operation_id"] == operation["operation_id"]
    cancellation, _ = accept(auth, "auth.cancel")
    assert (await authentication.execute(cancellation, authorize=authorized, read_guard=authorized, reconcile=False)).state == "completed"
    assert ceremonies.browser_entry(operation["operation_id"]) is None
    assert (await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=True)).state == "refused"
    with coordinator.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_setup_operations SET state='refused' WHERE operation_id=?", (operation["operation_id"],))
        connection.execute("UPDATE desktop_setup_operations SET state='completed' WHERE operation_id=?", (cancellation["operation_id"],))
    retry, _ = accept(auth, provider="microsoft-personal")
    await authentication.execute(retry, authorize=authorized, read_guard=authorized, reconcile=False)
    assert ceremonies.browser_entry(retry["operation_id"]) == entry
    assert ceremonies.browser_entry(operation["operation_id"]) is None
    assert client.attempts[client.current]["provider"] == "microsoft-personal"
    with coordinator.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM desktop_browser_ceremonies").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM desktop_setup_journeys").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_admission_requires_authoritative_read_and_switch_is_separate_command(auth):
    _, ceremonies, authentication, client, parent, _, _ = auth
    operation, _ = accept(auth)
    await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    assert (await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=True)).state == "waiting"
    client.attempts[client.current]["state"] = "admitted"
    assert (await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=True)).state == "completed"
    assert ceremonies.browser_entry(operation["operation_id"]) is None
    switching, _ = accept(auth, "auth.switch", "default")
    intent = json.loads(switching["owner_scope_json"])["authentication_intent"]
    assert intent["expected_previous_attempt_id"] == client.current
    await authentication.execute(switching, authorize=authorized, read_guard=authorized, reconcile=False)
    assert authentication.parent_operation(switching["operation_id"]) == parent["operation_id"]
    assert client.attempts[client.current]["mode"] == "switch-account"


@pytest.mark.asyncio
async def test_admitted_preparation_retry_keeps_exact_child_pending_for_browser_reopen(auth):
    coordinator, ceremonies, authentication, client, parent, native, _ = auth
    operation, _ = accept(auth)
    await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    client.attempts[client.current]["state"] = "admitted"
    client.preparation_recovery = True
    outcome = await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=True)
    assert outcome.state == "waiting"
    coordinator._settle(operation, outcome)
    coordinator.owner.scope["desktop_authentication"] = authentication.presentation(parent["journey_id"], "account.connect")
    snapshot = coordinator.read(native)
    browser_operation = snapshot["authentication"]["browser_operation_id"]
    assert browser_operation == operation["operation_id"]
    assert browser_operation in {value["operation_id"] for value in snapshot["pending_operations"]}
    assert ceremonies.browser_entry(browser_operation) is not None
    client.preparation_recovery = False
    assert (await authentication.execute(operation, authorize=authorized, read_guard=authorized,
        reconcile=True)).state == "completed"
    assert ceremonies.browser_entry(browser_operation) is None


async def close_task(auth):
    coordinator, _, authentication, _, _, native, _ = auth
    operation, _ = accept(auth)
    await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    tasks = BrowserTasks(coordinator.admissions, extension_id="a" * 32)
    task_id = tasks.prepare(operation, "browser.authentication")
    entry = coordinator.admissions.reissue_task_entry(native.identifier, generation=native.generation, task_id=task_id)
    browser = coordinator.admissions.consume(entry, role="browser_task")
    def admit(connection):
        coordinator.admissions._require(connection, browser.identifier, role="browser_task", generation=browser.generation)
    return operation, browser, admit


@pytest.mark.asyncio
async def test_close_is_durable_duplicate_safe_and_recovery_outlives_document(auth):
    coordinator, ceremonies, authentication, client, parent, _, _ = auth
    operation, browser, admitted = await close_task(auth)
    for _ in range(2):
        assert authentication.close_for_task(browser.task_id, browser.generation, authorize=admitted) == operation
    assert not any(verb == "cancel" for verb, _ in client.calls)
    coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    reopened = DesktopAuthentication(ceremonies.store, ceremonies, client)
    # Startup reconciliation finishes the persisted withdrawal without another
    # browser document or borrowing a native operator's general authority.
    result = await reopened.execute(operation, authorize=lambda: pytest.fail("new command"), read_guard=authorized, reconcile=True)
    assert (result.state, result.reason) == ("refused", "none")
    await reopened.refresh(parent["operation_id"], authorize=authorized)
    cancels = [request for verb, request in client.calls if verb == "cancel"]
    assert len(cancels) == 1 and cancels[0]["authentication_attempt_id"] == client.current
    assert set(cancels[0]) == {"profile", "operation_id", "ceremony_id", "binding", "authentication_attempt_id"}
    with coordinator.store.database.read() as connection:
        assert connection.execute("SELECT state FROM desktop_authentication_closures").fetchone()[0] == "settled"
        assert connection.execute("SELECT state FROM desktop_setup_operations WHERE operation_id=?", (parent["operation_id"],)).fetchone()[0] == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["before-send", "lost-reply", "callback-race"])
async def test_close_recovers_exact_child_without_overriding_admission(auth, fault):
    _, ceremonies, authentication, client, parent, _, _ = auth
    _, browser, admitted = await close_task(auth)
    authentication.close_for_task(browser.task_id, browser.generation, authorize=admitted)
    execute = client.execute
    def interrupted(verb, request, **kwargs):
        if verb == "cancel":
            if fault == "callback-race":
                client.attempts[client.current]["state"] = "admitted"
                return client.read(request, **kwargs)
            if fault == "before-send":
                raise OSError("No send")
            execute(verb, request, **kwargs)
            raise OSError("Lost reply")
        return execute(verb, request, **kwargs)
    client.execute = interrupted
    if fault == "callback-race":
        result = await authentication.refresh(parent["operation_id"], authorize=authorized)
        assert result["state"] == "admitted"
    else:
        with pytest.raises(OSError):
            await authentication.refresh(parent["operation_id"], authorize=authorized)
        client.execute = execute
        reopened = DesktopAuthentication(ceremonies.store, ceremonies, client)
        result = await reopened.refresh(parent["operation_id"], authorize=authorized)
        assert result["state"] == "retired"
        assert sum(verb == "cancel" for verb, _ in client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["admitted", "successor", "expired", "unavailable"])
async def test_old_closure_never_cancels_admitted_successor_expired_or_unknown_work(auth, state):
    from datetime import timedelta
    from uuid import uuid4
    _, _, authentication, client, parent, _, clock = auth
    _, browser, admitted = await close_task(auth)
    authentication.close_for_task(browser.task_id, browser.generation, authorize=admitted)
    if state == "successor":
        original = client.current
        client.current = str(uuid4())
        client.attempts[client.current] = {**client.attempts[original], "authentication_attempt_id": client.current}
    elif state == "expired":
        clock[0] += timedelta(hours=1)
        client.attempts[client.current]["state"] = state
    else:
        client.attempts[client.current]["state"] = state
    await authentication.refresh(parent["operation_id"], authorize=authorized)
    assert not any(verb == "cancel" for verb, _ in client.calls)


@pytest.mark.asyncio
async def test_close_requires_exact_live_task_admission_and_retained_key_guard(auth):
    from uuid import uuid4
    _, _, authentication, client, parent, _, _ = auth
    _, browser, admitted = await close_task(auth)
    with pytest.raises(HostedGrantUnavailable):
        authentication.close_for_task(browser.task_id, str(uuid4()), authorize=admitted)
    def refused(*_):
        raise HostedGrantUnavailable("Key changed")
    with pytest.raises(HostedGrantUnavailable):
        authentication.close_for_task(browser.task_id, browser.generation, authorize=refused)
    authentication.close_for_task(browser.task_id, browser.generation, authorize=admitted)
    with pytest.raises(HostedGrantUnavailable):
        await authentication.refresh(parent["operation_id"], authorize=refused)
    assert not any(verb == "cancel" for verb, _ in client.calls)


@pytest.mark.asyncio
async def test_browser_http_close_accepts_only_its_own_task_and_empty_body(auth):
    from unittest.mock import Mock
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.onboarding.admission_routes import ORIGIN
    from app.onboarding.browser_routes import browser_router
    from test_desktop_browser_routes import headers
    coordinator, ceremonies, authentication, client, parent, native, _ = auth
    operation, browser, _ = await close_task(auth)
    coordinator._start = Mock()
    app = FastAPI()
    app.include_router(browser_router(coordinator, None, ceremonies))
    with TestClient(app, base_url=ORIGIN) as http:
        path = "/api/v1/browser-tasks/closed"
        assert http.post(path, json={}, headers=headers(native)).status_code == 401
        assert http.post(path, json={"operation_id": parent["operation_id"]}, headers=headers(browser)).status_code == 400
        missing_csrf = headers(browser)
        missing_csrf.pop("X-Desktop-CSRF")
        assert http.post(path, json={}, headers=missing_csrf).status_code == 403
        assert http.post(path, json={}, headers=headers(browser)).status_code == 202
        assert http.post(path, json={}, headers=headers(browser)).status_code == 202
    coordinator._start.assert_called_with(operation, reconcile=True, actor=None)
    assert not any(verb == "cancel" for verb, _ in client.calls)
    await authentication.refresh(parent["operation_id"], authorize=authorized)
    assert client.attempts[client.current]["state"] == "retired"


@pytest.mark.asyncio
async def test_only_admitted_activation_preparation_emits_closed_task_recovery(tmp_path):
    from app.onboarding.admission import ScopedAdmissions
    from app.onboarding.ceremonies import BrowserCeremonies
    from app.onboarding.coordinator import DesktopOnboardingCoordinator
    from app.persistence.auth import SQLiteAuthenticationStore
    from test_desktop_ceremonies import Client, NOW, vector
    from test_desktop_coordinator import Owner
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: NOW)
    admissions = ScopedAdmissions(store, stage="runtime")
    native = admissions.consume(admissions.prepare_desktop(), role="desktop")
    owner = Owner()
    owner.facts = dict.fromkeys(owner.facts, "verified")
    owner.facts["activation"] = "missing"
    coordinator = DesktopOnboardingCoordinator(admissions, owner)
    parent, _ = coordinator._accept(native, command(coordinator.read(native), "activation.review"))
    ceremonies = BrowserCeremonies(store, Client(), hosted_origin="https://provider.example")
    await ceremonies.prepare(parent["operation_id"], copy.deepcopy(vector("prepare", "activation")["request"]["binding"]), authorize=authorized)
    client = AuthenticationClient()
    authentication = DesktopAuthentication(store, ceremonies, client, providers=("google", "default"))
    ceremonies.authentication = authentication
    await authentication.refresh(parent["operation_id"], authorize=authorized)
    owner.scope["desktop_authentication"] = authentication.presentation(parent["journey_id"], "activation.review")
    request = {**command(coordinator.read(native), "auth.select"), "provider": "google"}
    operation, _ = coordinator._accept(native, request)
    await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    entry = ceremonies.browser_entry(operation["operation_id"])
    assert "preparation_recovery" not in entry
    client.attempts[client.current]["state"] = "admitted"
    await authentication.refresh(parent["operation_id"], authorize=authorized)
    assert ceremonies.browser_entry(operation["operation_id"]) is None
    client.preparation_recovery = True
    await authentication.refresh(parent["operation_id"], authorize=authorized)
    assert ceremonies.browser_entry(operation["operation_id"]) == {**entry, "preparation_recovery": True}
