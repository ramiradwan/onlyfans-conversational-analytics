import asyncio
import copy
import json
import re
from types import SimpleNamespace

import pytest

from app.onboarding.authentication import DesktopAuthentication
from app.onboarding.browser_tasks import BrowserTasks
from app.onboarding.coordinator import CommandRefused, OperationResult
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
            "choices": dict.fromkeys(("google", "microsoft-personal", "telegram", "tiktok", "default"), True),
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
async def test_a_fresh_selection_opens_the_store_once_before_it_is_sent(auth):
    _, ceremonies, authentication, client, _, _, _ = auth
    operation, _ = accept(auth)
    database = ceremonies.store.database
    opened, connect, original = [], database.connect, client.execute

    def counted():
        opened.append(1)
        return connect()

    def execute(verb, *args, **kwargs):
        before.append(len(opened))
        return original(verb, *args, **kwargs)

    before = []
    database.connect, client.execute = counted, execute
    assert (await authentication.execute(operation, authorize=authorized, read_guard=authorized,
                                         reconcile=False)).state == "waiting"
    assert before == [1]


@pytest.mark.asyncio
async def test_a_helper_payload_reads_its_browser_step_through_one_store_opening(auth):
    _, ceremonies, authentication, _, _, _, _ = auth
    operation, _ = accept(auth)
    await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    database = ceremonies.store.database
    opened, connect = [], database.connect

    def counted():
        opened.append(1)
        return connect()

    database.connect = counted
    assert ceremonies.browser_entry(operation["operation_id"]) is not None
    assert len(opened) == 1


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


@pytest.mark.asyncio
async def test_open_sign_in_probe_only_reads_and_reports_each_change_once(auth):
    coordinator, _, authentication, client, parent, _, _ = auth
    operation, _ = accept(auth)
    await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    identifier = operation["operation_id"]
    assert authentication.probe_interval(identifier) is None
    tasks = BrowserTasks(coordinator.admissions, extension_id="a" * 32)
    tasks.prepare(operation, "browser.authentication")
    assert authentication.probe_interval(identifier) == 2

    def saved():
        with coordinator.store.database.read() as connection:
            return [tuple(row) for row in connection.execute(
                "SELECT operation_id,projection_json,revision FROM desktop_authentication_projections").fetchall()]

    before, calls = saved(), len(client.calls)
    assert await authentication.probe(identifier, authorize=authorized) is True
    assert await authentication.probe(identifier, authorize=authorized) is False
    client.attempts[client.current]["state"] = "admitted"
    assert await authentication.probe(identifier, authorize=authorized) is True
    assert await authentication.probe(identifier, authorize=authorized) is False
    # A probe reads only the parent's current projection. It never mutates,
    # cancels, stores a projection or reads under a borrowed child scope.
    probes = client.calls[calls:]
    assert len(probes) == 4
    assert all(verb == "read" and request["authentication_attempt_id"] is None
        and request["operation_id"] == client.calls[0][1]["operation_id"] for verb, request in probes)
    assert saved() == before
    with pytest.raises(HostedGrantUnavailable):
        await authentication.probe(identifier, authorize=lambda: (_ for _ in ()).throw(HostedGrantUnavailable("key changed")))
    assert len(client.calls) == calls + 4
    tasks.complete(identifier)
    assert authentication.probe_interval(identifier) is None
    assert authentication.probe_interval(parent["operation_id"]) is None


@pytest.mark.asyncio
async def test_admission_ends_the_open_sign_in_task_after_one_probe_not_the_slow_retry(auth, monkeypatch):
    monkeypatch.setattr("app.onboarding.authentication.PROBE_INTERVAL_SECONDS", 0.02, raising=False)
    monkeypatch.setattr("app.onboarding.coordinator.RECOVERY_INTERVAL_SECONDS", 3600)
    coordinator, _, authentication, client, _, _, _ = auth
    operation, _ = accept(auth)
    tasks = BrowserTasks(coordinator.admissions, extension_id="a" * 32)
    owner = coordinator.owner
    unchanged, completed = asyncio.Event(), asyncio.Event()
    probes = []

    async def execute(current, *, reconcile, actor=None):
        if current["operation_id"] != operation["operation_id"]:
            return OperationResult("waiting")
        owner.calls.append((dict(current), reconcile))
        return await authentication.execute(current, authorize=authorized, read_guard=authorized, reconcile=reconcile)

    async def probe(current):
        changed = await authentication.probe(current["operation_id"], authorize=authorized)
        probes.append(changed)
        if not changed:
            unchanged.set()
        return changed

    def settled(current, result):
        if result.state == "completed":
            tasks.complete(current["operation_id"])
            completed.set()

    owner.execute = execute
    owner.automatic_recovery = lambda current: current["operation_id"] == operation["operation_id"]
    owner.probe_interval = lambda current: authentication.probe_interval(current["operation_id"])
    owner.probe = probe
    owner.settled = settled

    def task_state():
        with coordinator.store.database.read() as connection:
            return connection.execute("SELECT state FROM desktop_browser_tasks WHERE operation_id=?",
                (operation["operation_id"],)).fetchone()[0]

    coordinator._start(operation, reconcile=False, actor=None)
    await coordinator.drain()
    tasks.prepare(operation, "browser.authentication")
    coordinator.start()
    try:
        await asyncio.wait_for(unchanged.wait(), timeout=60)
        assert [reconcile for _, reconcile in owner.calls] == [False, True, True]
        assert task_state() == "pending"
        probes.clear()
        client.attempts[client.current]["state"] = "admitted"
        await asyncio.wait_for(completed.wait(), timeout=60)
        assert task_state() == "completed"
        assert probes == [True]
        assert [reconcile for _, reconcile in owner.calls] == [False, True, True, True]
        assert [verb for verb, _ in client.calls if verb != "read"] == ["select"]
    finally:
        await coordinator.stop()


class ProbeOwner:
    def __init__(self):
        self.calls = []

    def parent_operation(self, operation_id):
        return "parent-of-" + operation_id

    def probe_interval(self, operation_id):
        self.calls.append(("interval", operation_id))
        return 2

    async def probe(self, operation_id, *, authorize):
        authorize()
        self.calls.append(("probe", operation_id))
        return True


@pytest.mark.asyncio
async def test_setup_owners_probe_only_through_the_operation_read_guard():
    from app.onboarding.runtime import RuntimeSetupOwner
    from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
    guards = []
    probe = ProbeOwner()
    setup = SimpleNamespace(ceremonies=SimpleNamespace(authentication=probe),
        _read_guard=lambda operation: guards.append(("setup", operation["operation_id"])))
    setup.probe_interval = lambda operation: ProvisioningSetupOwner.probe_interval(setup, operation)
    setup.probe = lambda operation: ProvisioningSetupOwner.probe(setup, operation)
    child = {"operation_id": "child", "action": "auth.select", "owner_scope_json": "{}"}
    assert ProvisioningSetupOwner.probe_interval(setup, child) == 2
    assert await ProvisioningSetupOwner.probe(setup, child) is True
    assert guards == [("setup", "parent-of-child")]
    assert ProvisioningSetupOwner.probe_interval(SimpleNamespace(ceremonies=None), child) is None
    assert ProvisioningSetupOwner.probe_interval(SimpleNamespace(ceremonies=SimpleNamespace(authentication=None)), child) is None

    activation = SimpleNamespace(ceremonies=SimpleNamespace(authentication=probe),
        _proof_read_guard=lambda operation: guards.append(("activation", operation["operation_id"])))
    def runtime_owner(**fields):
        value = SimpleNamespace(**fields)
        value._probe_owner = lambda operation: RuntimeSetupOwner._probe_owner(value, operation)
        return value
    runtime = runtime_owner(activation=activation, setup_owner=setup)
    runtime_child = {"operation_id": "runtime-child", "action": "auth.select", "owner_scope_json": json.dumps({"actor": {}})}
    review = {"operation_id": "review", "journey_id": "review-journey", "action": "activation.review", "owner_scope_json": "{}"}
    connect = {"operation_id": "connect", "action": "account.connect", "owner_scope_json": "{}"}
    for operation in (runtime_child, review, child, connect):
        assert RuntimeSetupOwner.probe_interval(runtime, operation) == 2
        assert await RuntimeSetupOwner.probe(runtime, operation) is True
    assert guards[1:] == [("activation", "runtime-child"), ("activation", "review"),
        ("setup", "parent-of-child"), ("setup", "parent-of-connect")]
    for action in ("browser.access", "browser.pair", "setup.finalize", "activation.confirm"):
        assert RuntimeSetupOwner.probe_interval(runtime, {**review, "action": action}) is None
    assert RuntimeSetupOwner.probe_interval(runtime_owner(activation=None, setup_owner=None), runtime_child) is None


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
@pytest.mark.parametrize("ended", ["closed", "completed", "cancelled"])
async def test_ended_unadmitted_popup_projects_choices_without_effects_and_click_selects(auth, ended):
    coordinator, _, authentication, client, parent, native, _ = auth
    operation, browser, admitted = await close_task(auth)
    if ended == "closed":
        authentication.close_for_task(browser.task_id, browser.generation, authorize=admitted)
    else:
        with coordinator.store.database.transaction() as connection:
            connection.execute("UPDATE desktop_browser_tasks SET state=? WHERE task_id=?", (ended, browser.task_id))
    before = copy.deepcopy(client.calls)
    with coordinator.store.database.read() as connection:
        saved = connection.execute("SELECT projection_json FROM desktop_authentication_projections").fetchone()[0]
    for _ in range(2):
        presentation = authentication.presentation(parent["journey_id"], "account.connect")
        assert presentation["state"] == "reauthentication-required"
        assert presentation["actions"] == dict(select=True, cancel=False, open_browser=False, switch_account=False)
        coordinator.owner.scope["desktop_authentication"] = presentation
        snapshot = coordinator.read(native)
        assert "auth.select" in snapshot["allowed_actions"]
        assert snapshot["decision"] is None
    assert client.calls == before
    with coordinator.store.database.read() as connection:
        assert connection.execute("SELECT projection_json FROM desktop_authentication_projections").fetchone()[0] == saved
        assert connection.execute("SELECT state FROM desktop_setup_operations WHERE operation_id=?",
            (operation["operation_id"],)).fetchone()[0] == "accepted"
    previous = client.current
    fresh, _ = accept(auth, provider="default")
    intent = json.loads(fresh["owner_scope_json"])["authentication_intent"]
    assert intent["expected_previous_attempt_id"] == previous
    assert client.calls == before
    assert (await authentication.execute(fresh, authorize=authorized, read_guard=authorized, reconcile=False)).state == "waiting"
    assert client.current != previous
    assert client.attempts[client.current]["provider"] == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted", [False, True])
async def test_open_popup_waits_and_admitted_ended_popup_keeps_confirmation_semantics(auth, admitted):
    coordinator, _, authentication, client, parent, _, _ = auth
    _, browser, _ = await close_task(auth)
    if admitted:
        client.attempts[client.current]["state"] = "admitted"
        await authentication.refresh(parent["operation_id"], authorize=authorized)
        with coordinator.store.database.transaction() as connection:
            connection.execute("UPDATE desktop_browser_tasks SET state='completed' WHERE task_id=?", (browser.task_id,))
    value = authentication.presentation(parent["journey_id"], "account.connect")
    assert value["state"] == ("admitted" if admitted else "selected")
    assert value["actions"]["select"] is False


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


@pytest.mark.asyncio
async def test_an_openable_fresh_provider_dispatch_uses_only_prepare_and_select(setup, caplog):
    from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
    coordinator, ceremonies, ceremony_client, parent, binding, _ = setup
    client = AuthenticationClient()
    original = client.execute
    order = []

    def select(*args, **kwargs):
        order.append("select")
        result = original(*args, **kwargs)
        client.calls.pop()  # The fake builds its mutation response using its read helper.
        return result

    client.execute = select
    authentication = DesktopAuthentication(ceremonies.store, ceremonies, client)
    ceremonies.authentication = authentication
    with coordinator.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_setup_operations SET state='refused' WHERE operation_id=?", (parent["operation_id"],))
    native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    coordinator.owner.scope.update(journey_kind="initial-enrollment", source_journey_id=parent["journey_id"],
        desktop_authentication=authentication.presentation(parent["journey_id"], "account.connect"))
    parent, fresh = coordinator._accept(native, {**command(coordinator.read(native), "account.connect"), "provider": "default"})
    assert fresh
    tasks = BrowserTasks(coordinator.admissions, extension_id="a" * 32)
    reads = []

    async def decision(*args, **kwargs):
        reads.append("decision")
        return {"state": "awaiting-authentication"}

    async def prepare(*args, **kwargs):
        order.append(("prepare", kwargs))

    real_prepare = ceremony_client.prepare

    def prepare_ceremony(*args, **kwargs):
        order.append("ceremony")
        return real_prepare(*args, **kwargs)

    ceremony_client.prepare = prepare_ceremony
    owner = ProvisioningSetupOwner.__new__(ProvisioningSetupOwner)
    owner.store, owner.ceremonies, owner.browser_tasks = coordinator.store, ceremonies, tasks
    owner.initial = owner.registered = SimpleNamespace(prepare=prepare, resume=lambda *_: order.append("resume"))
    owner._guard, owner._binding = lambda *a, **k: None, lambda *_: binding
    owner._read_guard, owner.observe = lambda *_: None, lambda *_: None
    ceremonies.decisions = SimpleNamespace(refresh=decision)
    with caplog.at_level("INFO", logger="app.onboarding.timing"):
        result = await owner._connect(parent, reconcile=False)
    assert result.state == "waiting"
    assert order == [("prepare", {"resume": False}), "ceremony", "select"]
    owner.entry_served(parent["operation_id"])
    assert order[-1] == "resume"
    phases = re.findall(r"operation=(\S+) phase=(\S+)", caplog.text)
    for point in (("ceremony-prepare", "persist"), ("authentication-select", "lookup"), ("authentication-select", "reserve"),
                  ("authentication-select", "guard")):
        assert point in phases
    # The browser task is saved before any key or handoff work starts.
    assert phases.index(("worker", "task")) < phases.index(("source", "prepare"))
    assert [verb for verb, _ in ceremony_client.calls] == ["prepare"]
    assert [verb for verb, _ in client.calls] == ["select"]
    assert reads == []
    with coordinator.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM desktop_browser_tasks").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_a_hint_completes_its_task_with_two_store_openings(auth):
    coordinator, ceremonies, authentication, client, _, _, _ = auth
    operation, _ = accept(auth)
    await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    BrowserTasks(coordinator.admissions, extension_id="a" * 32).prepare(operation, "browser.authentication")
    client.attempts[client.current]["state"] = "admitted"
    database = ceremonies.store.database
    opened, connect = [], database.connect

    def counted():
        opened.append(1)
        return connect()

    database.connect = counted
    assert await authentication.reconcile_task(operation["operation_id"], authorize=authorized) is True
    # One read for the task and its scope, one transaction that completes it.
    assert len(opened) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "projection", "stale", "closure"])
async def test_the_refresh_after_a_completing_hint_uses_its_saved_projection_once(auth, change):
    import app.onboarding.authentication as module
    coordinator, ceremonies, authentication, client, parent, _, _ = auth
    operation, _ = accept(auth)
    await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    task_id = BrowserTasks(coordinator.admissions, extension_id="a" * 32).prepare(operation, "browser.authentication")
    client.attempts[client.current]["state"] = "admitted"
    assert await authentication.reconcile_task(operation["operation_id"], authorize=authorized) is True
    if change == "projection":
        client.attempts[client.current]["state"] = "selected"
        await authentication._read(parent["operation_id"], authorize=authorized)
    elif change == "stale":
        authentication._settled[parent["operation_id"]] = (
            authentication._settled[parent["operation_id"]][0] - module.SETTLED_SECONDS - 1,
            *authentication._settled[parent["operation_id"]][1:])
    elif change == "closure":
        with coordinator.store.database.transaction() as connection:
            connection.execute("INSERT INTO desktop_authentication_closures VALUES (?,?,?,'pending')",
                (operation["operation_id"], task_id, 2 ** 40))
    before = len(client.calls)
    result = await authentication.refresh(parent["operation_id"], authorize=authorized)
    assert (len(client.calls) == before) == (change == "none")
    if change == "none":
        assert result["state"] == "admitted"
        await authentication.refresh(parent["operation_id"], authorize=authorized)
        assert len(client.calls) == before + 1


@pytest.mark.asyncio
async def test_a_proof_read_guard_checks_its_ceremony_and_key_through_one_store_connection(auth):
    from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
    coordinator, ceremonies, _, _, parent, _, _ = auth
    binding = json.loads(ceremonies.row(parent["operation_id"])["binding_json"])
    reference = SimpleNamespace(installation_key_id=binding["installation_key_id"],
                                installation_key_jkt=binding["installation_key_jkt"])
    seen = []

    def installation_key_reference(*, connection=None):
        seen.append(connection)
        return reference

    owner = ProvisioningSetupOwner.__new__(ProvisioningSetupOwner)
    owner.ceremonies = ceremonies
    owner.store = SimpleNamespace(database=coordinator.store.database,
                                  installation_key_reference=installation_key_reference)
    database = coordinator.store.database
    opened, connect = [], database.connect

    def counted():
        opened.append(1)
        return connect()

    database.connect = counted
    owner._read_guard({"operation_id": parent["operation_id"]})
    assert len(opened) == 1 and len(seen) == 1 and seen[0] is not None


@pytest.mark.asyncio
async def test_a_hinted_read_guard_checks_through_one_store_connection(auth):
    from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
    coordinator, ceremonies, authentication, _, parent, _, _ = auth
    operation, _ = accept(auth)
    await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    binding = json.loads(ceremonies.row(parent["operation_id"])["binding_json"])
    reference = SimpleNamespace(installation_key_id=binding["installation_key_id"],
                                installation_key_jkt=binding["installation_key_jkt"])
    owner = ProvisioningSetupOwner.__new__(ProvisioningSetupOwner)
    owner.ceremonies = ceremonies
    owner.store = SimpleNamespace(database=coordinator.store.database, installation_key_reference=lambda **_: reference)
    shared = []
    owner._guard = lambda current, *, connection=None: shared.append(connection is not None)
    owner.hold_reads = lambda operation_id: None

    async def reconcile_task(operation_id, *, authorize):
        authorize()
        return None

    authentication.reconcile_task = reconcile_task
    database = coordinator.store.database
    opened, connect = [], database.connect

    def counted():
        opened.append(1)
        return connect()

    database.connect = counted
    await owner.reconcile_hint(operation)
    assert shared == [True] and len(opened) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "successor", "cancel", "generation", "expired", "key", "source", "preparation"])
async def test_hint_closes_only_current_task_after_one_current_read(auth, change):
    coordinator, ceremonies, authentication, client, parent, _, _ = auth
    operation, _ = accept(auth)
    await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    tasks = BrowserTasks(coordinator.admissions, extension_id="a" * 32)
    task_id = tasks.prepare(operation, "browser.authentication")
    client.attempts[client.current]["state"] = "admitted"
    if change == "successor":
        client.current = "0199b234-5678-7000-8000-000000000098"
        client.attempts[client.current] = {"authentication_attempt_id": client.current,
            "provider": "default", "mode": "sign-in", "state": "admitted"}
    if change == "preparation":
        client.preparation_recovery = True
    before = len(client.calls)
    real_read = client.read

    def read(*args, **kwargs):
        result = real_read(*args, **kwargs)
        if change in {"cancel", "generation", "expired"}:
            with coordinator.store.database.transaction() as connection:
                if change == "cancel":
                    connection.execute("UPDATE desktop_browser_tasks SET state='cancelled' WHERE task_id=?", (task_id,))
                elif change == "generation":
                    from uuid import uuid4
                    connection.execute("UPDATE desktop_browser_tasks SET admission_generation=? WHERE task_id=?", (str(uuid4()), task_id))
                elif change == "expired":
                    connection.execute("UPDATE desktop_browser_tasks SET expires_at=0 WHERE task_id=?", (task_id,))
        return result

    client.read = read
    guards = []

    def guard():
        from threading import get_ident
        guards.append(get_ident())
        if change in {"key", "source"} and len(guards) > 1:
            raise HostedGrantUnavailable("Changed scope")

    try:
        closed = await authentication.reconcile_task(operation["operation_id"], authorize=guard)
    except HostedGrantUnavailable:
        assert change in {"key", "source"}
        closed = False
    assert len(client.calls) - before == 1
    assert client.calls[-1][1]["authentication_attempt_id"] is None
    with coordinator.store.database.read() as connection:
        task = connection.execute("SELECT * FROM desktop_browser_tasks WHERE task_id=?", (task_id,)).fetchone()
        assert (task["state"] == "completed") == (change == "none")
        if change == "none":
            projection = json.loads(connection.execute("SELECT projection_json FROM desktop_authentication_projections WHERE operation_id=?",
                (parent["operation_id"],)).fetchone()[0])
            assert projection["state"] == "admitted"
    assert bool(closed) == (change == "none")
    from threading import get_ident
    assert all(thread != get_ident() for thread in guards)


@pytest.mark.asyncio
async def test_guards_and_binding_check_the_persisted_key_without_provider_work(setup):
    from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
    _, ceremonies, _, parent, binding, _ = setup
    await ceremonies.prepare(parent["operation_id"], binding, authorize=authorized)
    current = SimpleNamespace(installation_key_id=binding["installation_key_id"],
                              installation_key_jkt=binding["installation_key_jkt"])
    reference = [current]
    owner = ProvisioningSetupOwner.__new__(ProvisioningSetupOwner)
    owner.store = SimpleNamespace(database=ceremonies.store.database, installation_key_reference=lambda **_: reference[0])
    owner.ceremonies = ceremonies
    from app.persistence.source_replacement_reservations import SourceReplacementReservations
    owner.source_replacements = SimpleNamespace(reservations=SourceReplacementReservations(ceremonies.store))
    owner.key = SimpleNamespace(reopen_existing=lambda: pytest.fail("provider key work in a guard"))
    owner._read_guard({"operation_id": parent["operation_id"]})
    journey = SimpleNamespace(kind="initial-enrollment", installation_id="installation", operation_id="source-operation",
        preparation=SimpleNamespace(profile="profile", request_digest="digest"), handoff_reference="reference",
        handoff_expires_at="2026-10-12T13:00:00.000Z")
    owner._guard = lambda operation: journey
    selected = {"creator_account_id": "creator", "browser": None}
    made = owner._binding({"owner_scope_json": json.dumps(selected)})
    assert (made["installation_key_id"], made["installation_key_jkt"]) == (
        current.installation_key_id, current.installation_key_jkt)
    for changed in (SimpleNamespace(installation_key_id=current.installation_key_id, installation_key_jkt="changed"), None):
        reference[0] = changed
        with pytest.raises(HostedGrantUnavailable):
            owner._read_guard({"operation_id": parent["operation_id"]})
    with pytest.raises(HostedGrantUnavailable):
        owner._binding({"owner_scope_json": json.dumps(selected)})


@pytest.mark.asyncio
async def test_prefetch_renews_a_due_or_consumed_challenge_and_keeps_a_fresh_one(auth):
    _, ceremonies, authentication, client, parent, _, _ = auth
    tickets = []

    def prefetch_read(request, *, before_send):
        before_send()
        assert request["authentication_attempt_id"] is None
        tickets.append(SimpleNamespace(delay=40.0))
        return tickets[-1]

    client.prefetch_read = prefetch_read
    client.renewal_delay = lambda ticket: ticket.delay
    guards = []
    operation_id = parent["operation_id"]
    hosted_id = ceremonies.row(operation_id)["hosted_operation_id"]
    assert await authentication.prefetch(operation_id, authorize=lambda: guards.append(True)) == 40
    assert await authentication.prefetch(operation_id, authorize=lambda: guards.append(True)) == 40
    assert len(tickets) == 1 and authentication._prefetched[hosted_id] is tickets[0]
    tickets[0].delay = 0.0
    assert await authentication.prefetch(operation_id, authorize=lambda: guards.append(True)) == 40
    assert len(tickets) == 2 and authentication._prefetched[hosted_id] is tickets[1]
    authentication._prefetched.pop(hosted_id)
    assert await authentication.prefetch(operation_id, authorize=lambda: guards.append(True)) == 40
    assert len(tickets) == 3 and authentication._prefetched[hosted_id] is tickets[2]
    assert len(guards) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["selected", "closure", "fenced", "retired", "expired", "unavailable", "successor"])
async def test_hint_falls_through_on_pending_closure_fenced_task_or_non_open_state(auth, case):
    from uuid import uuid4
    coordinator, _, authentication, client, _, _, _ = auth
    operation, browser, admitted = await close_task(auth)
    coordinator._settle(operation, OperationResult("waiting"))
    owner = coordinator.owner
    owner.calls.clear()
    hint_outcomes = []

    async def execute(current, *, reconcile, actor=None):
        owner.calls.append((dict(current), reconcile))
        return await authentication.execute(current, authorize=authorized, read_guard=authorized, reconcile=reconcile)

    async def reconcile_hint(current):
        outcome = await authentication.reconcile_task(current["operation_id"], authorize=authorized)
        hint_outcomes.append(outcome)
        return outcome

    owner.execute = execute
    owner.reconcile_hint = reconcile_hint
    if case == "closure":
        authentication.close_for_task(browser.task_id, browser.generation, authorize=admitted)
    elif case == "fenced":
        client.attempts[client.current]["state"] = "retired"
        real_read = client.read

        def read(*args, **kwargs):
            result = real_read(*args, **kwargs)
            with coordinator.store.database.transaction() as connection:
                connection.execute("UPDATE desktop_browser_tasks SET admission_generation=? WHERE task_id=?",
                    (str(uuid4()), browser.task_id))
            return result

        client.read = read
    elif case in {"retired", "expired", "unavailable"}:
        client.attempts[client.current]["state"] = case
    elif case == "successor":
        client.attempts[client.current]["state"] = "retired"
        client.current = str(uuid4())
        client.attempts[client.current] = {"authentication_attempt_id": client.current,
            "provider": "default", "mode": "sign-in", "state": "selected"}

    coordinator._start(operation, reconcile=True, actor=None, hint=True)
    await coordinator.drain()
    with coordinator.store.database.read() as connection:
        state = connection.execute("SELECT state FROM desktop_setup_operations WHERE operation_id=?",
            (operation["operation_id"],)).fetchone()[0]
    if case == "selected":
        assert hint_outcomes == [False]
        assert owner.calls == []
        assert state == "waiting"
    else:
        assert hint_outcomes == [None]
        assert len(owner.calls) == 1 and owner.calls[0][1] is True
        assert state == "refused"
        if case == "closure":
            assert any(verb == "cancel" for verb, _ in client.calls)
            with coordinator.store.database.read() as connection:
                assert connection.execute("SELECT state FROM desktop_authentication_closures WHERE operation_id=?",
                    (operation["operation_id"],)).fetchone()[0] == "settled"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["expired", "unavailable", "missing"])
async def test_fresh_provider_dispatch_reports_unconfirmed_when_expired_or_unavailable(setup, state):
    from datetime import datetime, timezone
    from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
    from app.security.browser_authentication import BrowserAuthenticationRefused
    coordinator, ceremonies, ceremony_client, parent, binding, clock = setup
    client = AuthenticationClient()
    authentication = DesktopAuthentication(ceremonies.store, ceremonies, client)
    ceremonies.authentication = authentication
    with coordinator.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_setup_operations SET state='refused' WHERE operation_id=?", (parent["operation_id"],))
    native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    coordinator.owner.scope.update(journey_kind="initial-enrollment", source_journey_id=parent["journey_id"],
        desktop_authentication=authentication.presentation(parent["journey_id"], "account.connect"))
    parent, fresh = coordinator._accept(native, {**command(coordinator.read(native), "account.connect"), "provider": "default"})
    assert fresh
    if state != "missing":
        real_read = client.read

        def refused(*args, **kwargs):
            raise BrowserAuthenticationRefused("selection refused")

        def read_state(*args, **kwargs):
            result = real_read(*args, **kwargs)
            result["state"] = state
            result["actions"] = dict.fromkeys(result["actions"], False)
            return result

        client.execute = refused
        client.read = read_state

    async def prepare(*args, **kwargs):
        pass

    real_prepare = ceremony_client.prepare

    def prepare_ceremony(*args, **kwargs):
        result = real_prepare(*args, **kwargs)
        if state == "missing":
            # The operation expires between the ceremony and the selection.
            clock[0] = datetime.fromtimestamp(parent["expires_at"], tz=timezone.utc)
        return result

    ceremony_client.prepare = prepare_ceremony
    owner = ProvisioningSetupOwner.__new__(ProvisioningSetupOwner)
    owner.store, owner.ceremonies, owner.browser_tasks = coordinator.store, ceremonies, BrowserTasks(coordinator.admissions, extension_id="a" * 32)
    owner.initial = owner.registered = SimpleNamespace(prepare=prepare, resume=lambda *_: None)
    owner._guard, owner._binding = lambda *a, **k: None, lambda *_: binding
    owner._read_guard, owner.observe = lambda *_: None, lambda *_: None
    result = await owner._connect(parent, reconcile=False)
    assert result == OperationResult("outcome_unknown", "operation_unconfirmed")


async def _fresh_initial_connect(setup, *, fail=False, closed=0):
    from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
    coordinator, ceremonies, ceremony_client, parent, binding, _ = setup
    client = AuthenticationClient()
    original, original_read = client.execute, client.read
    remaining = [closed]

    def read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        if remaining[0]:
            # A selection can name the attempt before its browser step is open.
            remaining[0] -= 1
            result = {**result, "actions": {**result["actions"], "open_browser": False}}
        return result

    def select(*args, **kwargs):
        result = original(*args, **kwargs)
        client.calls.pop()
        return result

    client.read, client.execute = read, select
    authentication = DesktopAuthentication(ceremonies.store, ceremonies, client)
    ceremonies.authentication = authentication
    with coordinator.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_setup_operations SET state='refused' WHERE operation_id=?", (parent["operation_id"],))
    native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    coordinator.owner.scope.update(journey_kind="initial-enrollment", source_journey_id=parent["journey_id"],
        desktop_authentication=authentication.presentation(parent["journey_id"], "account.connect"))
    parent, fresh = coordinator._accept(native, {**command(coordinator.read(native), "account.connect"), "provider": "default"})
    assert fresh
    tasks = BrowserTasks(coordinator.admissions, extension_id="a" * 32)
    during = []

    def task_states():
        with coordinator.store.database.read() as connection:
            return [row[0] for row in connection.execute("SELECT state FROM desktop_browser_tasks WHERE operation_id=?",
                                                         (parent["operation_id"],))]

    async def prepare(*args, **kwargs):
        during.append(task_states())
        if fail:
            raise HostedGrantUnavailable("Source unavailable")

    async def decision(*args, **kwargs):
        return {"state": "awaiting-authentication"}

    owner = ProvisioningSetupOwner.__new__(ProvisioningSetupOwner)
    owner.store, owner.ceremonies, owner.browser_tasks = coordinator.store, ceremonies, tasks
    owner.initial = owner.registered = SimpleNamespace(prepare=prepare, resume=lambda *_: None)
    owner._guard, owner._binding = lambda *a, **k: None, lambda *_: binding
    owner._read_guard, owner.observe = lambda *_: None, lambda *_: None
    ceremonies.decisions = SimpleNamespace(refresh=decision)
    owner.client_calls = client.calls
    return owner, parent, during, task_states


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", [False, True])
async def test_preparation_without_a_ceremony_does_not_start_a_wait_or_dirty_the_worker(setup, monkeypatch, reserved):
    from app.provisioning.events import events
    from test_desktop_coordinator import _until

    monkeypatch.setattr("app.onboarding.coordinator.RECOVERY_INTERVAL_SECONDS", 3600)
    coordinator, ceremonies, _, _, binding, _ = setup
    owner, operation, _, _ = await _fresh_initial_connect(setup)
    entered, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    authentication = ceremonies.authentication

    async def prepare(*args, **kwargs):
        if reserved:
            ceremonies._reserve(operation["operation_id"], binding, authorized)
        entered.set()
        await release.wait()

    class Waits:
        def envelope(self, request, *, before_send):
            before_send()
            waiting.set()
            return request

        async def hints(self, envelope):
            await asyncio.Event().wait()
            yield

    owner.initial.prepare = prepare
    owner.automatic_recovery = lambda _: True
    authentication.waits = Waits()
    calls = []

    async def counted(current, **kwargs):
        calls.append(kwargs["reconcile"])
        return await owner._connect(current, reconcile=kwargs["reconcile"])

    owner.execute = counted
    coordinator.owner = owner
    coordinator._watcher = asyncio.create_task(coordinator._watch())
    coordinator._start(operation, reconcile=False, actor=None)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        events.publish()
        await asyncio.sleep(0.15)
        assert not coordinator._dirty
        assert not coordinator._waits
        assert not waiting.is_set()
        release.set()
        await asyncio.wait_for(waiting.wait(), 2)
        await _until(lambda: not coordinator._workers)
        assert ceremonies.browser_entry(operation["operation_id"]) is not None
        assert calls == [False]
        assert [verb for verb, _ in owner.client_calls] == ["select"]
    finally:
        release.set()
        await coordinator.stop()


@pytest.mark.asyncio
async def test_a_fresh_sign_in_task_exists_before_preparation_so_a_browser_launch_overlaps_it(setup):
    owner, parent, during, task_states = await _fresh_initial_connect(setup)
    result = await owner._connect(parent, reconcile=False)
    assert result.state == "waiting"
    assert during == [["pending"]]
    assert task_states() == ["pending"]
    assert owner.ceremonies.browser_entry(parent["operation_id"]) is not None


@pytest.mark.asyncio
async def test_a_failed_fresh_preparation_withdraws_its_task_without_a_ceremony(setup):
    owner, parent, during, task_states = await _fresh_initial_connect(setup, fail=True)
    with pytest.raises(HostedGrantUnavailable):
        await owner._connect(parent, reconcile=False)
    assert during == [["pending"]]
    assert task_states() == ["cancelled"]


@pytest.mark.parametrize(("case", "expected"), [
    ("signing-in", None), ("connected", "completed"), ("other-creator", None), ("installation-missing", None),
    ("grant-refused", None), ("journey-expired", None), ("journey-unavailable", None)])
def test_an_account_connection_is_observed_without_building_the_whole_view(monkeypatch, case, expected):
    from app.persistence.onboarding import OnboardingJourneyUnavailable
    from app.provisioning import desktop_onboarding
    from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
    from app.provisioning.finalize import FinalizationRefused

    owner = ProvisioningSetupOwner.__new__(ProvisioningSetupOwner)
    owner.sources = SimpleNamespace(resolve=lambda _: None)
    owner.browser_tasks = SimpleNamespace(read=lambda _: None)
    owner.store = None
    owner._source = lambda _: "source-1"

    def journey(_):
        if case == "journey-unavailable":
            raise OnboardingJourneyUnavailable("journey_unavailable")
        return SimpleNamespace(journey_id="source-1", kind="initial-enrollment",
                               state="expired" if case == "journey-expired" else "active")

    def grants(**_):
        if case == "grant-refused":
            raise FinalizationRefused("grant_unavailable")

    owner._journey = journey
    owner.progress = lambda _: {"stage": "registration_required" if case == "signing-in" else "finalization_ready",
        "association_request_id": "request-1", "creator_account_id": "creator-2" if case == "other-creator" else "creator-1"}

    def whole_view(*args, **kwargs):
        raise AssertionError("whole view built")

    owner.read = whole_view
    monkeypatch.setattr(desktop_onboarding, "verified_grant_bindings", grants)
    monkeypatch.setattr(desktop_onboarding, "local_setup_facts", lambda *_, **__: {
        "installation": "missing" if case == "installation-missing" else "verified"})
    operation = {"action": "account.connect", "journey_id": "journey-1", "owner_scope_json": json.dumps(
        {"source_journey_id": "source-1", "installation_id": "installation-1", "creator_account_id": "creator-1",
         "association_request_id": "request-1"})}
    result = owner.observe(operation)
    assert (None if result is None else result.state) == expected


def _select_owner(auth):
    from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
    coordinator, ceremonies, _, client, _, _, _ = auth
    operation, _ = accept(auth)
    tasks = BrowserTasks(coordinator.admissions, extension_id="a" * 32)
    owner = ProvisioningSetupOwner.__new__(ProvisioningSetupOwner)
    owner.store, owner.ceremonies, owner.browser_tasks = coordinator.store, ceremonies, tasks
    owner._guard, owner._read_guard, owner.observe = lambda *_: None, lambda *_: None, lambda *_: None
    during = []

    def task_states():
        with coordinator.store.database.read() as connection:
            return [row[0] for row in connection.execute("SELECT state FROM desktop_browser_tasks WHERE operation_id=?",
                                                         (operation["operation_id"],))]

    original = client.execute

    def execute(verb, *args, **kwargs):
        if verb == "select":
            during.append(task_states())
        return original(verb, *args, **kwargs)

    client.execute = execute
    return owner, operation, during, task_states


@pytest.mark.asyncio
async def test_a_fresh_provider_selection_task_exists_before_the_selection_is_sent(auth):
    owner, operation, during, task_states = _select_owner(auth)
    assert (await owner.execute(operation, reconcile=False)).state == "waiting"
    assert during == [["pending"]]
    assert task_states() == ["pending"]


@pytest.mark.asyncio
async def test_an_unconfirmed_provider_selection_withdraws_its_task_without_a_ceremony(auth):
    owner, operation, during, task_states = _select_owner(auth)
    auth[3].lose_selection_reply = True
    with pytest.raises(OSError):
        await owner.execute(operation, reconcile=False)
    assert during == [["pending"]]
    assert task_states() == ["cancelled"]


@pytest.mark.asyncio
async def test_a_selection_not_yet_openable_is_read_once_so_its_early_task_gets_the_ceremony_at_once(setup):
    owner, parent, _, task_states = await _fresh_initial_connect(setup, closed=1)
    result = await owner._connect(parent, reconcile=False)
    assert result.state == "waiting"
    assert [verb for verb, _ in owner.client_calls] == ["select", "read"]
    assert owner.ceremonies.browser_entry(parent["operation_id"]) is not None
    assert task_states() == ["pending"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("closed", "expected"), [
    (0, [("authentication-select", "entry-open")]),
    (1, [("authentication-select", "entry-pending"), ("authentication-read", "entry-open")]),
    (2, [("authentication-select", "entry-pending"), ("authentication-read", "entry-pending")])])
async def test_a_fresh_selection_records_whether_its_browser_step_was_open(setup, caplog, closed, expected):
    owner, parent, _, _ = await _fresh_initial_connect(setup, closed=closed)
    caplog.set_level("INFO", logger="app.onboarding.timing")
    await owner._connect(parent, reconcile=False)
    marks = [re.search(r"operation=(\S+) phase=(entry-\S+)", message).groups()
             for message in caplog.messages if "phase=entry-" in message]
    assert marks == expected


@pytest.mark.asyncio
async def test_a_selection_not_yet_openable_keeps_its_early_task_until_a_read_adds_the_ceremony(setup):
    owner, parent, _, task_states = await _fresh_initial_connect(setup, closed=2)
    result = await owner._connect(parent, reconcile=False)
    assert result.state == "waiting"
    assert [verb for verb, _ in owner.client_calls] == ["select", "read"]
    assert owner.ceremonies.browser_entry(parent["operation_id"]) is None
    assert task_states() == ["pending"]
    with owner.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_setup_operations SET state='waiting' WHERE operation_id=?", (parent["operation_id"],))
        task = dict(connection.execute("SELECT * FROM desktop_browser_tasks WHERE operation_id=?", (parent["operation_id"],)).fetchone())
    # The helper keeps waiting rather than ending while the operation waits for its ceremony.
    assert owner.browser_tasks.preparing(task)
    await owner._connect({**parent, "state": "waiting"}, reconcile=True)
    assert owner.ceremonies.browser_entry(parent["operation_id"]) is not None
    with owner.store.database.read() as connection:
        after = dict(connection.execute("SELECT * FROM desktop_browser_tasks WHERE operation_id=?", (parent["operation_id"],)).fetchone())
    assert after == task

@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["select", "switch"])
async def test_an_admission_frame_reads_the_child_once_and_settles_the_same_end_state(auth, verb):
    coordinator, _, authentication, client, parent, _, _ = auth
    tasks = BrowserTasks(coordinator.admissions, extension_id="a" * 32)
    owner = coordinator.owner

    async def execute(current, *, reconcile, actor=None):
        owner.calls.append((current["operation_id"], reconcile))
        if current["operation_id"] == parent["operation_id"]:
            projection = await authentication.refresh(parent["operation_id"], authorize=authorized)
            await authentication.ensure_initial(current, authorize=authorized, read_guard=authorized, current=projection)
            return OperationResult("waiting")
        return await authentication.execute(current, authorize=authorized, read_guard=authorized, reconcile=reconcile)

    async def reconcile_hint(current):
        return await authentication.reconcile_task(current["operation_id"], authorize=authorized)

    owner.execute, owner.reconcile_hint = execute, reconcile_hint
    owner.automatic_recovery = lambda current: True
    operation, _ = accept(auth)
    coordinator._settle(operation, await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False))
    if verb == "switch":
        client.attempts[client.current]["state"] = "admitted"
        coordinator._settle(operation, await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=True))
        operation, _ = accept(auth, "auth.switch", "default")
        coordinator._settle(operation, await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False))
    owner.settled = lambda current, result: tasks.complete(current["operation_id"]) if result.state == "completed" else None
    tasks.prepare(operation, "browser.authentication")
    client.attempts[client.current]["state"] = "admitted"
    before = len(client.calls)
    coordinator._reconcile_soon(operation, frame=(4, "active", "awaiting-confirmation"))
    await coordinator.drain()
    await coordinator.drain()
    # From the admission frame to the parent's final projection: one protected read.
    assert [kind for kind, _ in client.calls[before:]] == ["read"]
    with coordinator.store.database.read() as connection:
        state = connection.execute("SELECT state FROM desktop_setup_operations WHERE operation_id=?",
            (operation["operation_id"],)).fetchone()[0]
        task = connection.execute("SELECT state FROM desktop_browser_tasks WHERE operation_id=?",
            (operation["operation_id"],)).fetchone()[0]
        projection = json.loads(connection.execute("SELECT projection_json FROM desktop_authentication_projections WHERE operation_id=?",
            (parent["operation_id"],)).fetchone()[0])
    assert state == "completed" and task == "completed"
    assert projection["state"] == "admitted"
    assert projection["authentication_attempt"]["authentication_attempt_id"] == client.current
    assert (parent["operation_id"], True) in owner.calls
    await coordinator.stop()


@pytest.mark.asyncio
async def test_ensure_initial_returns_the_projection_its_caller_just_read(auth):
    _, _, authentication, client, parent, _, _ = auth
    projection = await authentication.refresh(parent["operation_id"], authorize=authorized)
    before = len(client.calls)
    operation = {"operation_id": parent["operation_id"], "owner_scope_json": json.dumps({})}
    assert await authentication.ensure_initial(operation, authorize=authorized, read_guard=authorized,
                                               current=projection) == projection
    assert len(client.calls) == before
    await authentication.ensure_initial(operation, authorize=authorized, read_guard=authorized)
    assert len(client.calls) == before + 1

@pytest.mark.asyncio
async def test_store_work_between_a_selection_reply_and_its_task_runs_off_the_event_loop(setup):
    import threading
    owner, parent, _, _ = await _fresh_initial_connect(setup)
    client = owner.ceremonies.authentication.client
    database, loop_thread = owner.store.database, threading.get_ident()
    window, on_loop = [False], []
    connect, select, prepare = database.connect, client.execute, owner.browser_tasks.prepare

    def counted():
        if window[0] and threading.get_ident() == loop_thread:
            on_loop.append(1)
        return connect()

    def replied(*args, **kwargs):
        result = select(*args, **kwargs)
        window[0] = True
        return result

    def task(*args, **kwargs):
        window[0] = False
        return prepare(*args, **kwargs)

    database.connect, client.execute, owner.browser_tasks.prepare = counted, replied, task
    assert (await owner._connect(parent, reconcile=False)).state == "waiting"
    assert window == [False] and on_loop == []

@pytest.mark.asyncio
async def test_a_fresh_sign_in_writes_its_reservations_off_the_event_loop_before_each_send(setup):
    import threading
    owner, parent, _, _ = await _fresh_initial_connect(setup)
    database, loop_thread = owner.store.database, threading.get_ident()
    transaction, on_loop, order = database.transaction, [], []
    client = owner.ceremonies.authentication.client
    prepare, select = owner.ceremonies.client.prepare, client.execute

    def counted(*args, **kwargs):
        if threading.get_ident() == loop_thread:
            on_loop.append(1)
        order.append("write")
        return transaction(*args, **kwargs)

    def prepared(*args, **kwargs):
        order.append("prepare-send")
        return prepare(*args, **kwargs)

    def selected(*args, **kwargs):
        order.append("select-send")
        return select(*args, **kwargs)

    database.transaction, owner.ceremonies.client.prepare, client.execute = counted, prepared, selected
    assert (await owner._connect(parent, reconcile=False)).state == "waiting"
    assert on_loop == []
    # The ceremony and the command are each recorded before their send.
    assert order.index("write") < order.index("prepare-send") < order.index("select-send")
    assert "write" in order[order.index("prepare-send"):order.index("select-send")]


@pytest.mark.asyncio
async def test_replacement_fences_old_authentication_task_command_projection_and_held_read(auth):
    from test_desktop_ceremonies import ReplacementClient, replacement_predecessor
    coordinator, ceremonies, authentication, client, parent, _, _ = auth
    operation, browser, admitted = await close_task(auth)
    local_id = parent["operation_id"]
    old = ceremonies.row(local_id)
    old_projection = await authentication.refresh(local_id, authorize=authorized)
    ordinary = ceremonies.client
    ordinary.state, ordinary.revision = "retired", 2
    await ceremonies.reconcile(local_id, authorize=authorized)
    predecessor = replacement_predecessor(ceremonies, parent)
    replacement = ReplacementClient(ordinary)
    ceremonies.replacement_client = replacement
    # Keep a held challenge for the predecessor; a successor read must not use it.
    authentication._prefetched[old["hosted_operation_id"]] = object()
    await ceremonies.replace(local_id, predecessor, authorize=authorized)
    successor = ceremonies.row(local_id)
    assert successor["hosted_operation_id"] != old["hosted_operation_id"]
    assert authentication.presentation(parent["journey_id"], "account.connect") is None
    assert ceremonies.browser_entry(operation["operation_id"]) is None
    assert ceremonies.task_row(operation["operation_id"]) is None
    with coordinator.store.database.read() as connection:
        assert connection.execute("SELECT state FROM desktop_browser_tasks WHERE task_id=?",
            (browser.task_id,)).fetchone()[0] == "cancelled"
        assert connection.execute("SELECT state FROM desktop_setup_operations WHERE operation_id=?",
            (operation["operation_id"],)).fetchone()[0] == "refused"
    with pytest.raises(HostedGrantUnavailable):
        authentication._save(old, old_projection)
    before = copy.deepcopy(client.calls)
    with pytest.raises(HostedGrantUnavailable):
        await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    assert client.calls == before
    # Simulate the Provider's fresh selection state on the verified successor.
    client.current = None
    fresh = await authentication.refresh(local_id, authorize=authorized)
    assert fresh["operation_id"] == successor["hosted_operation_id"]
    assert fresh["state"] == "selection-required"
    assert authentication._prefetched[old["hosted_operation_id"]] is not None
    assert authentication.presentation(parent["journey_id"], "account.connect")["actions"]["select"]
    command, _ = accept(auth, provider="default")
    assert (await authentication.execute(command, authorize=authorized, read_guard=authorized, reconcile=False)).state == "waiting"
    assert client.calls[-1][1]["operation_id"] == successor["hosted_operation_id"]


@pytest.mark.asyncio
async def test_replacement_fences_predecessor_auth_command_before_first_dispatch(auth):
    from test_desktop_ceremonies import ReplacementClient, replacement_predecessor
    coordinator, ceremonies, authentication, client, parent, _, _ = auth
    operation, _ = accept(auth)
    local_id = parent["operation_id"]
    ordinary = ceremonies.client
    ordinary.state, ordinary.revision = "retired", 2
    await ceremonies.reconcile(local_id, authorize=authorized)
    ceremonies.replacement_client = ReplacementClient(ordinary)
    await ceremonies.replace(local_id, replacement_predecessor(ceremonies, parent), authorize=authorized)
    before = copy.deepcopy(client.calls)
    with pytest.raises(HostedGrantUnavailable):
        await authentication.execute(operation, authorize=authorized, read_guard=authorized, reconcile=False)
    assert client.calls == before
    with coordinator.store.database.read() as connection:
        assert connection.execute("SELECT 1 FROM desktop_authentication_commands WHERE operation_id=?",
            (operation["operation_id"],)).fetchone() is None
