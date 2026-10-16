from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.onboarding.admission import AdmissionRefused, MAX_ADMISSIONS, ScopedAdmissions, csrf_token
from app.onboarding.admission_routes import admission_router, COOKIE_NAMES, CSRF, GENERATION, ORIGIN
from app.persistence.auth import SQLiteAuthenticationStore

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


@pytest.fixture
def setup(tmp_path):
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: now[0])
    admissions = ScopedAdmissions(store, stage="setup")
    return admissions, now


def desktop(admissions, journey=None):
    return admissions.consume(admissions.prepare_desktop(journey or str(uuid4())), role="desktop")


def task(admissions, owner, *, deadline=None):
    return admissions.prepare_task(owner.identifier, generation=owner.generation, journey_id=owner.journey_id,
        operation_id=str(uuid4()), purpose="browser.identity", scope_digest="a" * 64, source_revision=7,
        deadline=deadline or admissions._now() + 1000)


def test_browser_return_checks_current_presentation_without_retiring_its_task(setup):
    admissions, clock = setup
    native = desktop(admissions)
    _, entry = task(admissions, native)
    browser = admissions.consume(entry, role="browser_task")
    app = FastAPI()
    token = "x" * 32
    app.include_router(admission_router(admissions, launcher_token=token))
    body = {"generation": native.generation, "journey_id": native.journey_id}
    with TestClient(app, base_url=ORIGIN) as client:
        path = "/api/v1/desktop/presentation/current"
        assert client.post(path, json=body).status_code == 401
        headers = {"Authorization": "Desktop " + token}
        assert client.post(path, json=body, headers={**headers, "Origin": ORIGIN}).status_code == 401
        assert client.post(path, json=body, headers=headers).json() == {"current": True}
        assert admissions.require(browser.identifier, role="browser_task") == browser
        assert client.post(path, json={**body, "journey_id": str(uuid4())}, headers=headers).json() == {"current": False}
        clock[0] += timedelta(minutes=31)
        assert client.post(path, json=body, headers=headers).json() == {"current": False}


def test_native_refresh_replaces_expired_presentation_without_extending_accepted_task(setup):
    admissions, clock = setup
    native = desktop(admissions)
    task_id, _ = task(admissions, native)
    with admissions.authentication.database.transaction() as connection:
        connection.execute("UPDATE desktop_browser_tasks SET state='outcome_unknown' WHERE task_id=?", (task_id,))
        original = dict(connection.execute("SELECT * FROM desktop_browser_tasks WHERE task_id=?", (task_id,)).fetchone())
    clock[0] += timedelta(minutes=31)
    app = FastAPI()
    app.include_router(admission_router(admissions, launcher_token="x" * 32))
    with TestClient(app, base_url=ORIGIN) as client:
        path = "/api/v1/desktop/presentation/refresh"
        body = {"generation": native.generation}
        assert client.post(path, json=body).status_code == 401
        headers = {"Authorization": "Desktop " + "x" * 32}
        assert client.post(path, json=body, headers=headers | {"Origin": ORIGIN}).status_code == 401
        result = client.post(path, json=body, headers=headers)
        assert result.status_code == 200
        assert "set-cookie" not in result.headers
    replacement = admissions.consume(result.json()["entry_code"], role="desktop")
    assert replacement.journey_id == native.journey_id
    assert replacement.generation != native.generation
    with admissions.authentication.database.read() as connection:
        assert dict(connection.execute("SELECT * FROM desktop_browser_tasks WHERE task_id=?", (task_id,)).fetchone()) == original
    recovered = admissions.consume(admissions.prepare_desktop(replacing_generation=native.generation), role="desktop")
    assert recovered.generation == replacement.generation
    assert recovered.expires_at == replacement.expires_at
    with pytest.raises(AdmissionRefused):
        admissions.require(native.identifier, role="desktop")


def test_native_refresh_cannot_retarget_or_displace_a_newer_pending_owner(setup):
    admissions, _ = setup
    native = desktop(admissions)
    pending = admissions.prepare_desktop(native.journey_id)
    with pytest.raises(AdmissionRefused):
        admissions.prepare_desktop(replacing_generation=native.generation)
    assert admissions.consume(pending, role="desktop").journey_id == native.journey_id


def test_extension_entry_source_survives_a_fresh_native_presentation(setup):
    admissions, _ = setup
    native = desktop(admissions)
    with admissions.authentication.database.transaction() as connection:
        connection.execute("INSERT INTO desktop_setup_journeys (journey_id,revision,created_at,entry_source) VALUES (?,0,?,'extension')",
            (native.journey_id, admissions._now()))
    replacement = admissions.consume(admissions.prepare_desktop(), role="desktop")
    with admissions.authentication.database.read() as connection:
        assert connection.execute("SELECT entry_source FROM desktop_presentations WHERE generation=?", (replacement.generation,)).fetchone()[0] == "extension"


def test_entry_is_single_use_across_concurrent_consumers_and_process_restart(setup):
    admissions, _ = setup
    code = admissions.prepare_desktop(str(uuid4()))
    restarted = ScopedAdmissions(admissions.authentication, stage="setup")
    def consume(_):
        try:
            return restarted.consume(code, role="desktop")
        except AdmissionRefused:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(consume, range(4)))
    assert sum(item is not None for item in outcomes) == 1
    winner = next(item for item in outcomes if item is not None)
    assert restarted.require(winner.identifier, role="desktop") == winner
    with admissions.authentication.database.read() as connection:
        persisted = repr([tuple(row) for row in connection.execute("SELECT * FROM desktop_admission_sessions")])
    assert code not in persisted and winner.identifier not in persisted
    assert csrf_token(winner.identifier) not in persisted


def test_task_and_desktop_admissions_cannot_substitute_for_each_other(setup):
    admissions, _ = setup
    owner = desktop(admissions)
    _, entry = task(admissions, owner)
    with pytest.raises(AdmissionRefused):
        admissions.consume(entry, role="desktop")
    browser = admissions.consume(entry, role="browser_task")
    for identifier, role in [(owner.identifier, "browser_task"), (browser.identifier, "desktop")]:
        with pytest.raises(AdmissionRefused):
            admissions.require(identifier, role=role)
    runtime = ScopedAdmissions(admissions.authentication, stage="runtime")
    for identifier, role in [(owner.identifier, "desktop"), (browser.identifier, "browser_task")]:
        with pytest.raises(AdmissionRefused):
            runtime.require(identifier, role=role)


def test_wrong_profile_reissue_fences_entry_and_browser_without_changing_operation(setup):
    admissions, now = setup
    owner = desktop(admissions)
    task_id, entry = task(admissions, owner)
    browser = admissions.consume(entry, role="browser_task")
    original = admissions.task(browser.identifier, generation=browser.generation)
    now[0] += timedelta(seconds=100)
    replacement = admissions.reissue_task_entry(owner.identifier, generation=owner.generation, task_id=task_id)
    with pytest.raises(AdmissionRefused):
        admissions.require(browser.identifier, role="browser_task")
    with pytest.raises(AdmissionRefused):
        admissions.consume(entry, role="browser_task")
    browser2 = admissions.consume(replacement, role="browser_task")
    current = admissions.task(browser2.identifier, generation=browser2.generation)
    assert current["admission_generation"] != original["admission_generation"]
    for field in ("task_id", "operation_id", "journey_id", "scope_digest", "source_revision", "state", "expires_at"):
        assert current[field] == original[field]


def test_replacement_desktop_retires_presentations_but_preserves_accepted_work(setup):
    admissions, _ = setup
    owner = desktop(admissions)
    task_id, entry = task(admissions, owner)
    browser = admissions.consume(entry, role="browser_task")
    with admissions.authentication.database.transaction() as connection:
        connection.execute("UPDATE desktop_browser_tasks SET state='accepted' WHERE task_id=?", (task_id,))
    old_pending_entry = admissions.prepare_desktop(owner.journey_id)
    new_owner = desktop(admissions, owner.journey_id)
    for identifier, role in [(owner.identifier, "desktop"), (browser.identifier, "browser_task")]:
        with pytest.raises(AdmissionRefused):
            admissions.require(identifier, role=role)
    with pytest.raises(AdmissionRefused):
        admissions.consume(old_pending_entry, role="desktop")
    new_entry = admissions.reissue_task_entry(new_owner.identifier, generation=new_owner.generation, task_id=task_id)
    browser2 = admissions.consume(new_entry, role="browser_task")
    assert admissions.task(browser2.identifier, generation=browser2.generation)["state"] == "accepted"


def test_entry_expiry_does_not_slide_and_is_bounded_by_task_parent(setup):
    admissions, now = setup
    owner = desktop(admissions)
    _, entry = task(admissions, owner, deadline=admissions._now() + 20)
    now[0] += timedelta(seconds=20)
    with pytest.raises(AdmissionRefused):
        admissions.consume(entry, role="browser_task")
    later_entry = admissions.prepare_desktop(str(uuid4()))
    now[0] += timedelta(seconds=300)
    with pytest.raises(AdmissionRefused):
        admissions.consume(later_entry, role="desktop")
    assert admissions.require(owner.identifier, role="desktop").expires_at == owner.expires_at
    now[0] += timedelta(seconds=1480)
    with pytest.raises(AdmissionRefused):
        admissions.require(owner.identifier, role="desktop")


def test_task_scope_and_generation_are_exact_and_operation_cannot_be_reprepared(setup):
    admissions, _ = setup
    owner = desktop(admissions)
    args = dict(generation=owner.generation, journey_id=owner.journey_id, operation_id=str(uuid4()),
        purpose="browser.access", scope_digest="b" * 64, source_revision=1, deadline=admissions._now() + 30)
    for change in ({"generation": str(uuid4())}, {"journey_id": str(uuid4())}, {"purpose": "execute"},
                   {"deadline": float("inf")}, {"source_revision": True}):
        with pytest.raises(AdmissionRefused):
            admissions.prepare_task(owner.identifier, **(args | change))
    admissions.prepare_task(owner.identifier, **args)
    with pytest.raises(AdmissionRefused, match="task_already_exists"):
        admissions.prepare_task(owner.identifier, **args)


def test_full_unresolved_task_budget_cannot_block_native_recovery(setup):
    admissions, now = setup
    owner = desktop(admissions)
    with admissions.authentication.database.transaction() as connection:
        for _ in range(MAX_ADMISSIONS):
            connection.execute("INSERT INTO desktop_browser_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'outcome_unknown', ?)",
                (str(uuid4()), owner.journey_id, owner.generation, str(uuid4()), "browser.identity",
                 "a" * 64, 1, str(uuid4()), "setup", admissions._now() + 10))
    with pytest.raises(AdmissionRefused, match="admission_limit_reached"):
        task(admissions, owner)
    now[0] += timedelta(seconds=1800)
    replacement = desktop(admissions, owner.journey_id)
    assert admissions.require(replacement.identifier, role="desktop") == replacement
    with admissions.authentication.database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM desktop_browser_tasks WHERE state='outcome_unknown'").fetchone()[0] == MAX_ADMISSIONS


def test_cleanup_bounds_terminal_presentations_and_preserves_unknown_work(setup):
    admissions, now = setup
    owner = desktop(admissions)
    states = ("pending", "completed", "cancelled", "expired", "accepted", "outcome_unknown")
    for state in states:
        task_id, _ = task(admissions, owner, deadline=admissions._now() + 10)
        with admissions.authentication.database.transaction() as connection:
            connection.execute("UPDATE desktop_browser_tasks SET state=? WHERE task_id=?", (state, task_id))
    # Terminal records remain readable until the original admission deadline.
    admissions.prepare_desktop(owner.journey_id)
    with admissions.authentication.database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM desktop_browser_tasks").fetchone()[0] == len(states)
    now[0] += timedelta(seconds=1800)
    desktop(admissions, owner.journey_id)
    with admissions.authentication.database.read() as connection:
        assert {row[0] for row in connection.execute("SELECT state FROM desktop_browser_tasks")} == {"accepted", "outcome_unknown"}
        assert connection.execute("SELECT COUNT(*) FROM desktop_presentations").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM desktop_admission_entries").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM desktop_admission_sessions").fetchone()[0] == 1


def test_http_entry_requires_launcher_proof_and_browser_origin_then_separate_cookie(setup):
    admissions, _ = setup
    app = FastAPI()
    app.include_router(admission_router(admissions, launcher_token="t" * 43))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.post("/api/v1/desktop/entry", json={}).status_code == 401
        headers = {"Authorization": "Desktop " + "t" * 43}
        assert client.post("/api/v1/desktop/entry", headers=headers | {"Origin": ORIGIN}, json={}).status_code == 401
        issued = client.post("/api/v1/desktop/entry", headers=headers, json={"journey_id": str(uuid4())})
        assert "set-cookie" not in issued.headers
        code = issued.json()["entry_code"]
        for origin in (None, "https://outside.example"):
            assert client.post("/api/v1/desktop/entry/consume", headers={} if origin is None else {"Origin": origin}, json={"code": code}).status_code == 403
        admitted = client.post("/api/v1/desktop/entry/consume", headers={"Origin": ORIGIN}, json={"code": code})
        assert admitted.status_code == 200
        cookie = SimpleCookie(admitted.headers["set-cookie"])[COOKIE_NAMES["desktop"]]
        assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "strict"
        assert cookie["path"] == "/"
        assert "identifier" not in admitted.json()
        owner_headers = {"Cookie": f'{COOKIE_NAMES["desktop"]}={cookie.value}', GENERATION: admitted.json()["generation"]}
        assert client.get("/api/v1/desktop/session", headers=owner_headers).status_code == 200
        assert client.get("/api/v1/browser-tasks/session", headers=owner_headers).status_code == 401
        assert client.get("/api/v1/desktop/session", headers=owner_headers | {"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.get("/onboarding/entry?code=" + code).status_code == 400


def test_entry_stylesheet_is_available_before_admission_without_loosening_csp(setup):
    admissions, _ = setup
    app = FastAPI()
    app.include_router(admission_router(admissions, launcher_token="t" * 43))
    with TestClient(app, base_url=ORIGIN) as client:
        for path in ("/onboarding/entry", "/browser-tasks/entry"):
            landing = client.get(path)
            assert landing.status_code == 200
            assert '<link rel="stylesheet" href="/onboarding/entry.css">' in landing.text.split("</head>")[0]
            assert "style-src 'self'" in landing.headers["content-security-policy"]
            assert "'unsafe-inline'" not in landing.headers["content-security-policy"]
            assert "set-cookie" not in landing.headers
        stylesheet = client.get("/onboarding/entry.css")
        assert stylesheet.status_code == 200 and stylesheet.text
        assert stylesheet.headers["content-type"].startswith("text/css")
        assert stylesheet.headers["cache-control"] == "no-store"
        assert stylesheet.headers["x-content-type-options"] == "nosniff"
        assert "set-cookie" not in stylesheet.headers
        assert client.get("/onboarding/entry.css", headers={"Host": "outside.example"}).status_code == 421
        assert client.get("/api/v1/desktop/session").status_code == 401


def test_stale_document_cannot_discover_replacement_generation_through_shared_cookie(setup):
    admissions, _ = setup
    old = desktop(admissions)
    replacement = desktop(admissions, old.journey_id)
    app = FastAPI()
    app.include_router(admission_router(admissions, launcher_token="t" * 43))
    shared = {"Cookie": f'{COOKIE_NAMES["desktop"]}={replacement.identifier}'}
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/desktop/session", headers=shared).status_code == 409
        assert client.get("/api/v1/desktop/session", headers=shared | {GENERATION: old.generation}).status_code == 401
        assert client.get("/api/v1/desktop/session", headers=shared | {GENERATION: replacement.generation}).status_code == 200


def test_browser_reissue_http_requires_exact_generation_and_csrf(setup):
    admissions, _ = setup
    owner = desktop(admissions)
    task_id, _ = task(admissions, owner)
    app = FastAPI()
    app.include_router(admission_router(admissions, launcher_token="t" * 43))
    headers = {"Cookie": f'{COOKIE_NAMES["desktop"]}={owner.identifier}', "Origin": ORIGIN,
               GENERATION: owner.generation, CSRF: csrf_token(owner.identifier)}
    with TestClient(app, base_url=ORIGIN) as client:
        path = f"/api/v1/desktop/browser-tasks/{task_id}/entry"
        assert client.post(path, headers=headers | {CSRF: "wrong"}).status_code == 403
        assert client.post(path, headers=headers | {GENERATION: str(uuid4())}).status_code == 401
        assert client.post(path, headers=headers).status_code == 200


def test_refresh_lost_replies_recover_exact_successor_and_reject_unrelated_owner(setup):
    admissions, clock = setup
    original = desktop(admissions)
    lost = admissions.prepare_desktop(replacing_generation=original.generation)
    restarted = ScopedAdmissions(admissions.authentication, stage="setup")
    recovered = restarted.prepare_desktop(replacing_generation=original.generation)
    with pytest.raises(AdmissionRefused):
        restarted.consume(lost, role="desktop")
    consumed = restarted.consume(recovered, role="desktop")
    clock[0] += timedelta(seconds=5)
    replay = restarted.consume(restarted.prepare_desktop(replacing_generation=original.generation), role="desktop")
    assert replay.generation == consumed.generation
    assert replay.expires_at == consumed.expires_at
    pending = restarted.prepare_desktop()
    with pytest.raises(AdmissionRefused):
        restarted.prepare_desktop(replacing_generation=original.generation)
    assert restarted.consume(pending, role="desktop").generation != replay.generation
