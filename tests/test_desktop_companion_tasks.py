"""Desktop admission, operator and operation fencing for companion task dispatch."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.onboarding.admission import AdmissionRefused, ScopedAdmissions, csrf_token
from app.onboarding.admission_routes import COOKIE_NAMES, ORIGIN
from app.onboarding.browser_tasks import BrowserTasks
from app.onboarding.companion import companion_task_router
from app.onboarding.coordinator import DesktopOnboardingCoordinator
from app.persistence.auth import SQLiteAuthenticationStore
from app.security.hosted_grants import HostedGrantUnavailable
from test_desktop_coordinator import Owner, command

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


@pytest.fixture
def setup(tmp_path):
    now = [datetime.now(timezone.utc)]
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: now[0])
    admissions = ScopedAdmissions(store, stage="runtime")
    native = admissions.consume(admissions.prepare_desktop(), role="desktop")
    owner = Owner()
    owner.facts = dict.fromkeys(owner.facts, "verified")
    owner.facts["activation"] = "missing"
    owner.scope = {"creator_account_id": "creator-test"}
    policy = SimpleNamespace(identity=SimpleNamespace(creator_account_id="creator-test"))

    def authorize(_operation, operator):
        if operator != "current-operator" or policy.identity is None:
            raise HostedGrantUnavailable("Operator changed")
        return policy

    owner.authorize = Mock(side_effect=authorize)
    coordinator = DesktopOnboardingCoordinator(admissions, owner)
    operation, _ = coordinator._accept(native, command(coordinator.read(native), "activation.review"))
    # Binding the first operation associates the already-admitted presentation.
    native = admissions.require(native.identifier, role="desktop", generation=native.generation)
    tasks = BrowserTasks(admissions, extension_id="a" * 32)
    task_id = tasks.prepare(operation, "browser.authentication")
    entry = admissions.reissue_task_entry(native.identifier, generation=native.generation, task_id=task_id)
    helper = admissions.consume(entry, role="browser_task")
    ceremony = {"entry_url": "https://provider.example/onboarding/browser-ceremonies/" + "A" * 43,
        "ceremony_id": "0199b234-5678-7000-8000-000000000002", "attempt_id": "A" * 43,
        "completion": {"mode": "extension-auth-flow", "return_uri": "https://" + "a" * 32 + ".chromiumapp.org/browser-ceremony/v1/complete"},
        "expires_at": (now[0] + timedelta(minutes=5)).isoformat()}
    delivered = []

    async def send(account, target, payload, *, authorize, receive):
        authorize()
        delivered.append((account, target, payload, receive))
        return "dispatched"

    manager = SimpleNamespace(browser_task_target=Mock(return_value=17), send_browser_task=send)

    def actor(request, mutation):
        assert mutation
        if request.headers.get("X-CSRF-Token") != "operator-csrf":
            raise HTTPException(403, "Operator request context is unavailable")
        return "current-operator"

    app = FastAPI()
    ceremonies = SimpleNamespace(browser_entry=lambda _: ceremony, authentication=None,
        task_row=lambda _: {"ceremony_id": ceremony["ceremony_id"],
            "binding_json": json.dumps({"attempt_id": ceremony["attempt_id"]})})
    app.include_router(companion_task_router(coordinator, tasks, ceremonies, manager=manager, actor=actor))
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set(COOKIE_NAMES["desktop"], native.identifier)
    headers = {"Origin": ORIGIN, "X-Desktop-Generation": native.generation, "X-Desktop-CSRF": csrf_token(native.identifier),
        "X-CSRF-Token": "operator-csrf"}
    return SimpleNamespace(client=client, headers=headers, path=f"/api/v1/desktop/browser-tasks/{task_id}/dispatch",
        coordinator=coordinator, manager=manager, tasks=tasks, native=native, helper=helper, policy=policy,
        operation=operation, delivered=delivered, now=now, task_id=task_id, ceremonies=ceremonies)


def test_dispatch_fences_helper_without_changing_the_operation_or_issuing_a_browser_credential(setup):
    h = setup
    original = h.tasks.current_native(h.native)[0]
    response = h.client.post(h.path, json={}, headers=h.headers)
    assert response.status_code == 200 and response.json() == {"state": "dispatched"}
    account, target, payload, _ = h.delivered[0]
    assert (account, target) == ("creator-test", 17)
    assert payload["creator_account_id"] == account and payload["task"]["operation_id"] == h.operation["operation_id"]
    for field in ("task_id", "scope_digest", "expires_at", "source_revision"):
        assert payload["task"][field] == original[field]
    assert payload["task"]["admission_generation"] != original["admission_generation"]
    with pytest.raises(AdmissionRefused):
        h.coordinator.admissions.require(h.helper.identifier, role="browser_task", generation=h.helper.generation)
    with h.coordinator.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM desktop_admission_entries").fetchone()[0] == 0
    assert h.coordinator.owner.authorize.call_count == 3


@pytest.mark.parametrize("fault", ["desktop-csrf", "operator-csrf", "generation", "body", "query", "account", "operator", "expired", "task"])
def test_dispatch_refuses_stale_or_unscoped_requests(setup, fault):
    h = setup
    headers, path, body = dict(h.headers), h.path, {}
    if fault == "desktop-csrf":
        headers.pop("X-Desktop-CSRF")
    elif fault == "operator-csrf":
        headers.pop("X-CSRF-Token")
    elif fault == "generation":
        headers["X-Desktop-Generation"] = str(uuid4())
    elif fault == "body":
        body = {"account": "other"}
    elif fault == "query":
        path += "?account=other"
    elif fault == "account":
        h.policy.identity.creator_account_id = "other"
    elif fault == "operator":
        h.policy.identity = None
    elif fault == "expired":
        h.now[0] += timedelta(minutes=31)
    else:
        path = path.replace(h.task_id, str(uuid4()))
    assert h.client.post(path, json=body, headers=headers).status_code in {400, 401, 403, 409}
    assert not h.delivered


def test_unreachable_browser_preserves_the_current_helper_and_task(setup):
    h = setup
    original = h.tasks.current_native(h.native)[0]
    h.manager.browser_task_target.return_value = None
    assert h.client.post(h.path, json={}, headers=h.headers).json() == {"state": "unreachable"}
    assert h.tasks.current_native(h.native)[0] == original
    assert h.coordinator.admissions.require(h.helper.identifier, role="browser_task", generation=h.helper.generation)
    assert not h.delivered


@pytest.mark.asyncio
async def test_notifications_read_only_the_original_operation_and_ignore_replaced_or_expired_tasks(setup):
    h = setup
    h.coordinator._start = Mock()
    assert h.client.post(h.path, json={}, headers=h.headers).status_code == 200
    report = h.delivered[-1][3]
    notification = {name: h.delivered[-1][2]["ceremony"][name] for name in ("ceremony_id", "attempt_id")}
    notification["notification"] = "changed"
    await report({"state": "opening"})
    h.coordinator._start.assert_not_called()
    await report({"state": "checking", "notification": notification})
    args, kwargs = h.coordinator._start.call_args
    assert args[0]["operation_id"] == h.operation["operation_id"] and kwargs == {"reconcile": True, "actor": None}
    h.coordinator._start.reset_mock()
    assert h.client.post(h.path, json={}, headers=h.headers).status_code == 200
    await report({"state": "checking", "notification": notification})
    h.coordinator._start.assert_not_called()
    h.now[0] += timedelta(minutes=31)
    await h.delivered[-1][3]({"state": "checking", "notification": notification})
    h.coordinator._start.assert_not_called()


@pytest.mark.asyncio
async def test_paired_close_uses_exact_task_owner_without_borrowing_operator_authority(setup):
    h = setup
    h.coordinator._start = Mock()
    accepted = []
    def close(task_id, generation, *, authorize):
        with h.coordinator.store.database.read() as connection:
            operation = authorize(connection)
        accepted.append((task_id, generation))
        return dict(operation)
    h.ceremonies.authentication = SimpleNamespace(close_for_task=close)
    assert h.client.post(h.path, json={}, headers=h.headers).status_code == 200
    report = h.delivered[-1][3]
    payload = h.delivered[-1][2]
    h.policy.identity = None
    await report({"state": "closed", "notification": None})
    assert accepted == [(h.task_id, payload["task"]["admission_generation"])]
    h.coordinator._start.assert_called_once_with(h.operation, reconcile=True, actor=None)
    h.coordinator._start.reset_mock()
    h.coordinator.admissions.consume(h.coordinator.admissions.prepare_desktop(), role="desktop")
    await report({"state": "closed", "notification": None})
    assert len(accepted) == 1
    h.coordinator._start.assert_not_called()
