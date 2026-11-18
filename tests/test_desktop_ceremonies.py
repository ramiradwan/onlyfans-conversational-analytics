import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.onboarding.admission import ScopedAdmissions
from app.onboarding.ceremonies import BrowserCeremonies
from app.onboarding.coordinator import DesktopOnboardingCoordinator, OperationResult
from app.persistence.auth import SQLiteAuthenticationStore
from app.security.browser_ceremony import PROFILE, BrowserCeremonyClient, _contract, binding_digest, expected_completion
from app.security.hosted_grants import HostedGrantUnavailable
from test_browser_ceremony_client import NOW, vector
from test_desktop_coordinator import Owner, command

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


class Client:
    contract = _contract()
    transport = SimpleNamespace(close=lambda: None)

    def __init__(self):
        self.calls = []
        self.lost = False
        self.state = "active"
        self.revision = 1
        self.result = None
        self.successors = {}
        self.unknown = set()
        self.validator = BrowserCeremonyClient(None, None, clock=lambda: NOW)

    def validate_result(self, operation, request, result, *, expires_at=None):
        self.validator.validate_result(operation, request, result, expires_at=expires_at)

    def prepare(self, request, *, before_send):
        before_send()
        self.calls.append(("prepare", copy.deepcopy(request)))
        if self.lost:
            raise OSError("Lost response")
        result = {"profile": PROFILE, "operation_id": request["operation_id"],
            "ceremony_id": vector()["request"]["ceremony_id"], "binding_digest": binding_digest(request["binding"]),
            "expires_at": request["binding"]["source_expires_at"],
            "entry_reference": request["binding"]["source"]["reference"], "completion": expected_completion(request["binding"])}
        self.validator.validate_result("prepare", request, result)
        return result

    def read(self, request, *, before_send, expires_at=None):
        before_send()
        self.calls.append(("read", copy.deepcopy(request)))
        if request["operation_id"] in self.unknown:
            return {"profile": PROFILE, "operation_id": request["operation_id"], "status": "unavailable"}
        successor = self.successors.get(request["operation_id"])
        result = {"profile": PROFILE, "operation_id": request["operation_id"], "status": "found",
            "snapshot": {"operation_id": request["operation_id"], "ceremony_id": vector()["request"]["ceremony_id"] if successor is None else successor["ceremony_id"],
                "attempt_id": request["binding"]["attempt_id"], "binding_digest": binding_digest(request["binding"]),
                "expires_at": request["binding"]["source_expires_at"], "revision": self.revision,
                "presentation_state": self.state, "domain_status": "pending"}}
        if self.result is not None:
            result["snapshot"].update(domain_status="committed", result=self.result)
        if self.state == "active":
            result["entry"] = {"entry_reference": request["binding"]["source"]["reference"],
                "completion": expected_completion(request["binding"])}
        self.validator.validate_result("read", request, result, expires_at=expires_at)
        return result


@pytest.fixture
def setup(tmp_path):
    clock = [NOW]
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: clock[0])
    admissions = ScopedAdmissions(store, stage="setup")
    native = admissions.consume(admissions.prepare_desktop(), role="desktop")
    owner = Owner()
    owner.facts["browser_access"] = "verified"
    coordinator = DesktopOnboardingCoordinator(admissions, owner)
    operation, fresh = coordinator._accept(native, command(coordinator.read(native), "account.connect"))
    assert fresh
    client = Client()
    ceremonies = BrowserCeremonies(store, client, hosted_origin="https://provider.example")
    binding = copy.deepcopy(vector("prepare")["request"]["binding"])
    return coordinator, ceremonies, client, operation, binding, clock


def authorized(connection=None):
    pass


@pytest.mark.asyncio
async def test_lost_prepare_and_restart_only_read_the_exact_original_operation(setup):
    _, ceremonies, client, operation, binding, _ = setup
    client.lost = True
    with pytest.raises(OSError):
        await ceremonies.prepare(operation["operation_id"], binding, authorize=authorized)
    saved = ceremonies.row(operation["operation_id"])
    assert saved["ceremony_id"] is None
    reopened = BrowserCeremonies(ceremonies.store, client, hosted_origin=ceremonies.hosted_origin)
    result = await reopened.prepare(operation["operation_id"], binding, authorize=authorized)
    assert result["status"] == "found"
    assert [kind for kind, _ in client.calls] == ["prepare", "read"]
    assert client.calls[0][1] == client.calls[1][1]
    assert reopened.row(operation["operation_id"])["hosted_operation_id"] == saved["hosted_operation_id"]
    assert reopened.browser_entry(operation["operation_id"])["entry_url"].startswith(
        "https://provider.example/onboarding/browser-ceremonies/")


@pytest.mark.asyncio
async def test_scope_replacement_and_stale_revision_cannot_change_saved_ceremony(setup):
    _, ceremonies, client, operation, binding, _ = setup
    await ceremonies.prepare(operation["operation_id"], binding, authorize=authorized)
    client.revision = 3
    await ceremonies.reconcile(operation["operation_id"], authorize=authorized)
    saved = ceremonies.row(operation["operation_id"])
    changed = {**binding, "intended_creator_id": "another-creator"}
    with pytest.raises(HostedGrantUnavailable):
        await ceremonies.prepare(operation["operation_id"], changed, authorize=authorized)
    client.revision = 2
    with pytest.raises(HostedGrantUnavailable):
        await ceremonies.reconcile(operation["operation_id"], authorize=authorized)
    assert ceremonies.row(operation["operation_id"]) == saved


@pytest.mark.asyncio
async def test_local_expiry_hides_entry_but_keeps_proof_recovery(setup):
    _, ceremonies, client, operation, binding, clock = setup
    await ceremonies.prepare(operation["operation_id"], binding, authorize=authorized)
    await ceremonies.reconcile(operation["operation_id"], authorize=authorized)
    clock[0] += timedelta(hours=1)
    assert ceremonies.browser_entry(operation["operation_id"]) is None
    client.state = "expired"
    response = await ceremonies.reconcile(operation["operation_id"], authorize=authorized)
    assert response["status"] == "found"
    assert [kind for kind, _ in client.calls] == ["prepare", "read", "read"]


@pytest.mark.asyncio
@pytest.mark.parametrize("retired", [False, True])
async def test_commercial_result_is_delegated_without_persisting_clr1(tmp_path, retired):
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: NOW)
    admissions = ScopedAdmissions(store, stage="runtime")
    native = admissions.consume(admissions.prepare_desktop(), role="desktop")
    owner = Owner()
    owner.facts = dict.fromkeys(owner.facts, "verified")
    owner.facts["activation"] = "missing"
    coordinator = DesktopOnboardingCoordinator(admissions, owner)
    operation, _ = coordinator._accept(native, command(coordinator.read(native), "activation.review"))
    binding = copy.deepcopy(vector("prepare", "activation")["request"]["binding"])
    client = Client()
    ceremonies = BrowserCeremonies(store, client, hosted_origin="https://provider.example")
    await ceremonies.prepare(operation["operation_id"], binding, authorize=authorized)
    await ceremonies.reconcile(operation["operation_id"], authorize=authorized)
    # The commercial owner can commit without changing the ceremony revision.
    # Explicit presentation retirement is a separate ceremony write.
    if retired:
        client.state = "retired"
        client.revision += 1
    client.result = {"kind": "activation-redemption", "activation_operation_id": binding["activation_operation_id"],
        "redemption": {"profile": "urn:bridge-clean:capability-license-redemption-continuation:v1",
            "continuation": "clr1." + "A" * 43, "expires_at": binding["source_expires_at"]}}
    result = await ceremonies.reconcile(operation["operation_id"], authorize=authorized)
    assert result["snapshot"]["result"] == client.result
    saved = json.dumps(ceremonies.row(operation["operation_id"]))
    assert "clr1." not in saved and "redemption" not in saved
    assert (ceremonies.browser_entry(operation["operation_id"]) is None) == retired


@pytest.mark.asyncio
async def test_terminal_journal_retention_does_not_delete_uncertain_ceremony(setup):
    coordinator, ceremonies, _, operation, binding, clock = setup
    await ceremonies.prepare(operation["operation_id"], binding, authorize=authorized)
    clock[0] += timedelta(minutes=31)
    native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    with coordinator.store.database.transaction() as connection:
        # This is the same bounded terminal cleanup used by command admission.
        connection.execute("DELETE FROM desktop_setup_operations WHERE state IN ('completed','refused') AND expires_at<=?",
            (clock[0].timestamp(),))
    assert ceremonies.row(operation["operation_id"]) is not None
    coordinator._settle(operation, OperationResult("completed"))
    coordinator._accept(native, command(coordinator.read(native), "account.connect"))
    assert ceremonies.row(operation["operation_id"]) is None


@pytest.mark.asyncio
async def test_sign_in_proof_clients_share_one_long_lived_connection_pool(tmp_path):
    from app.onboarding.composition import SIGN_IN_KEEPALIVE_SECONDS, build_desktop_setup
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3")
    core = build_desktop_setup(store=store, stage="setup", extension_id=None, hosted_origin="https://provider.example",
        authentication_providers=("default",), finalize=lambda **kwargs: None, completion_ready=lambda: False)
    ceremonies = core.ceremonies
    shared = ceremonies.client.transport
    assert all(client.transport is shared for client in (
        ceremonies.decisions.client, ceremonies.authentication.client, ceremonies.authentication.waits,
        ceremonies.replacement_client))
    assert shared._client._transport._pool._keepalive_expiry == SIGN_IN_KEEPALIVE_SECONDS == 60
    await core.close()
    assert shared._client.is_closed


@pytest.mark.asyncio
async def test_shared_proof_transport_closes_only_after_every_owner_drains(setup):
    import asyncio
    import threading
    from app.onboarding.authentication import DesktopAuthentication
    _, ceremonies, client, _, _, _ = setup
    closed, entered, release = [], threading.Event(), threading.Event()
    shared = SimpleNamespace(close=lambda: closed.append(True))
    client.transport = shared
    ceremonies.authentication = DesktopAuthentication(ceremonies.store, ceremonies, SimpleNamespace(transport=shared))

    def blocking():
        entered.set()
        release.wait(5)

    effect = asyncio.create_task(ceremonies._effects.run(blocking))
    await asyncio.to_thread(entered.wait, 5)
    stopping = asyncio.create_task(ceremonies.stop())
    await asyncio.sleep(0.05)
    assert not closed
    release.set()
    await asyncio.wait_for(stopping, 5)
    await effect
    assert closed


def replacement_predecessor(ceremonies, operation):
    row = ceremonies.row(operation["operation_id"])
    return {"predecessor_operation_id": row["hosted_operation_id"],
        "predecessor_ceremony_id": row["ceremony_id"],
        "predecessor_binding_digest": binding_digest(json.loads(row["binding_json"])),
        "predecessor_revision": row["provider_revision"]}


@pytest.mark.asyncio
async def test_replacement_reservation_survives_restart_without_renewing_or_sending(setup):
    _, ceremonies, client, operation, binding, _ = setup
    local_id = operation["operation_id"]
    await ceremonies.prepare(local_id, binding, authorize=authorized)
    client.state = "retired"
    client.revision = 2
    await ceremonies.reconcile(local_id, authorize=authorized)
    predecessor = replacement_predecessor(ceremonies, operation)
    previous = ceremonies.row(local_id)
    request = ceremonies.reserve_replacement(local_id, predecessor, authorize=authorized)
    assert request["predecessor_operation_id"] == previous["hosted_operation_id"]
    assert request["successor_operation_id"] != previous["hosted_operation_id"]
    assert request["successor_attempt_id"] != binding["attempt_id"]
    reopened = BrowserCeremonies(ceremonies.store, client, hosted_origin=ceremonies.hosted_origin)
    assert reopened.replacement_reservation(local_id, previous["hosted_operation_id"]) == request
    assert reopened.reserve_replacement(local_id, predecessor, authorize=authorized) == request
    assert reopened.row(local_id) == previous
    assert reopened.browser_entry(local_id) is None
    assert [kind for kind, _ in client.calls] == ["prepare", "read"]
    with reopened.store.database.read() as connection:
        row = dict(connection.execute("SELECT * FROM desktop_browser_ceremony_replacements").fetchone())
        assert row["source_expires_at"] == binding["source_expires_at"]
        assert json.loads(row["request_json"]) == request
        assert "entry_reference" not in row["request_json"] and "proof" not in row["request_json"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["active", "stale-revision", "wrong-digest", "wrong-ceremony",
    "source-expired", "committed", "unavailable", "revoked", "authorization", "parent-refused", "unread"])
async def test_replacement_reservation_refuses_noncurrent_or_unsafe_predecessor(setup, change):
    coordinator, ceremonies, client, operation, binding, clock = setup
    local_id = operation["operation_id"]
    await ceremonies.prepare(local_id, binding, authorize=authorized)
    if change != "unread":
        client.state = "active" if change == "active" else "retired"
        client.revision = 2
        await ceremonies.reconcile(local_id, authorize=authorized)
    predecessor = replacement_predecessor(ceremonies, operation)
    guard = authorized
    if change == "stale-revision":
        predecessor["predecessor_revision"] -= 1
    elif change == "wrong-digest":
        predecessor["predecessor_binding_digest"] = "0" * 64
    elif change == "wrong-ceremony":
        predecessor["predecessor_ceremony_id"] = predecessor["predecessor_operation_id"]
    elif change == "source-expired":
        clock[0] += timedelta(hours=1)
    elif change in {"committed", "unavailable", "revoked"}:
        with ceremonies.store.database.transaction() as connection:
            connection.execute("UPDATE desktop_browser_ceremonies SET domain_status=? WHERE operation_id=?",
                (change, local_id))
    elif change == "authorization":
        def guard(connection=None):
            raise HostedGrantUnavailable("Denied")
    elif change == "parent-refused":
        coordinator._settle(operation, OperationResult("refused", "operation_expired"))
    with pytest.raises(HostedGrantUnavailable):
        ceremonies.reserve_replacement(local_id, predecessor, authorize=guard)
    with ceremonies.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM desktop_browser_ceremony_replacements").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_replacement_reservation_cannot_select_new_successor_after_predecessor_revision_changes(setup):
    _, ceremonies, client, operation, binding, _ = setup
    local_id = operation["operation_id"]
    await ceremonies.prepare(local_id, binding, authorize=authorized)
    client.state, client.revision = "retired", 2
    await ceremonies.reconcile(local_id, authorize=authorized)
    predecessor = replacement_predecessor(ceremonies, operation)
    first = ceremonies.reserve_replacement(local_id, predecessor, authorize=authorized)
    client.revision += 1
    await ceremonies.reconcile(local_id, authorize=authorized)
    with pytest.raises(HostedGrantUnavailable):
        ceremonies.reserve_replacement(local_id, replacement_predecessor(ceremonies, operation), authorize=authorized)
    assert ceremonies.replacement_reservation(local_id, predecessor["predecessor_operation_id"]) == first


class ReplacementClient:
    def __init__(self, ordinary, *, clock=lambda: NOW):
        from app.security.browser_ceremony_replacement import BrowserCeremonyReplacementClient
        self.ordinary, self.clock = ordinary, clock
        self.validator = BrowserCeremonyReplacementClient(ordinary.validator, clock=clock)
        self.calls = []
        self.lost = False
        self.refused = False
        self.tampered = False

    def validate_result(self, request, predecessor, result):
        self.validator.validate_result(request, predecessor, result)

    def replace(self, request, predecessor, *, before_send):
        from app.persistence.auth import _new_uuid7
        from app.security.browser_ceremony_replacement import PROFILE as REPLACEMENT_PROFILE
        from app.security.browser_ceremony import BrowserCeremonyRefused
        before_send()
        self.calls.append(copy.deepcopy(request))
        if self.refused:
            self.ordinary.unknown.add(request["successor_operation_id"])
            raise BrowserCeremonyRefused("not_ready")
        binding = {**predecessor, "attempt_id": request["successor_attempt_id"]}
        successor = {"profile": PROFILE, "operation_id": request["successor_operation_id"],
            "ceremony_id": _new_uuid7(self.clock()), "binding_digest": binding_digest(binding),
            "expires_at": binding["source_expires_at"], "entry_reference": binding["source"]["reference"],
            "completion": expected_completion(binding)}
        self.ordinary.successors[request["successor_operation_id"]] = copy.deepcopy(successor)
        self.ordinary.state = "active"
        if self.lost:
            raise OSError("Lost replacement response")
        if self.tampered:
            successor["operation_id"] = request["predecessor_operation_id"]
        return {"profile": REPLACEMENT_PROFILE, "predecessor_operation_id": request["predecessor_operation_id"],
            "successor_binding": binding, "successor": successor}


async def retired_predecessor(setup):
    _, ceremonies, client, operation, binding, _ = setup
    operation_id = operation["operation_id"]
    await ceremonies.prepare(operation_id, binding, authorize=authorized)
    client.state, client.revision = "retired", 2
    await ceremonies.reconcile(operation_id, authorize=authorized)
    predecessor = replacement_predecessor(ceremonies, operation)
    replacement = ReplacementClient(client)
    ceremonies.replacement_client = replacement
    return ceremonies, client, operation_id, predecessor, replacement


@pytest.mark.asyncio
async def test_replacement_adopts_distinct_successor_and_retains_original_lineage(setup):
    ceremonies, client, operation_id, predecessor, replacement = await retired_predecessor(setup)
    original = ceremonies.row(operation_id)
    result = await ceremonies.replace(operation_id, predecessor, authorize=authorized)
    successor = ceremonies.row(operation_id)
    assert successor["hosted_operation_id"] == result["successor"]["operation_id"]
    assert json.loads(successor["binding_json"]) == result["successor_binding"]
    assert successor["provider_expires_at"] == original["provider_expires_at"]
    assert successor["ceremony_id"] != original["ceremony_id"]
    assert len(replacement.calls) == 1
    with ceremonies.store.database.read() as connection:
        root = dict(connection.execute("SELECT * FROM desktop_browser_ceremonies WHERE operation_id=?", (operation_id,)).fetchone())
        assert root == original
        assert connection.execute("SELECT count(*) FROM desktop_browser_ceremony_successors").fetchone()[0] == 1
    assert (await ceremonies.reconcile(operation_id, authorize=authorized))["status"] == "found"
    assert client.calls[-1][1]["operation_id"] == successor["hosted_operation_id"]
    with pytest.raises(HostedGrantUnavailable):
        ceremonies.require_current(original)
    with pytest.raises(HostedGrantUnavailable):
        await ceremonies.replace(operation_id, predecessor, authorize=authorized)
    assert len(replacement.calls) == 1


@pytest.mark.asyncio
async def test_lost_replacement_response_reconciles_exact_successor_on_restart_without_send(setup):
    ceremonies, client, operation_id, predecessor, replacement = await retired_predecessor(setup)
    replacement.lost = True
    original = ceremonies.row(operation_id)
    with pytest.raises(OSError):
        await ceremonies.replace(operation_id, predecessor, authorize=authorized)
    request = ceremonies.replacement_reservation(operation_id, original["hosted_operation_id"])
    assert ceremonies.row(operation_id) == original
    reopened = BrowserCeremonies(ceremonies.store, client, hosted_origin=ceremonies.hosted_origin,
        replacement_client=replacement)
    result = await reopened.reconcile(operation_id, authorize=authorized)
    assert result["status"] == "found"
    assert reopened.row(operation_id)["hosted_operation_id"] == request["successor_operation_id"]
    assert client.calls[-1][1]["operation_id"] == request["successor_operation_id"]
    assert "ceremony_id" not in client.calls[-1][1]
    assert len(replacement.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["refused", "tampered"])
async def test_replacement_refusal_and_invalid_result_do_not_install_successor(setup, failure):
    from app.security.browser_ceremony import BrowserCeremonyRefused
    ceremonies, client, operation_id, predecessor, replacement = await retired_predecessor(setup)
    original = ceremonies.row(operation_id)
    setattr(replacement, failure, True)
    with pytest.raises((BrowserCeremonyRefused, HostedGrantUnavailable)):
        await ceremonies.replace(operation_id, predecessor, authorize=authorized)
    assert ceremonies.row(operation_id) == original
    assert ceremonies.browser_entry(operation_id) is None
    assert len(replacement.calls) == 1
    if failure == "refused":
        result = await ceremonies.reconcile_replacement(operation_id, authorize=authorized)
        assert result["status"] == "unavailable"
        assert ceremonies.row(operation_id) == original
        assert len(replacement.calls) == 1


@pytest.mark.asyncio
async def test_adoption_cannot_overwrite_committed_predecessor_or_reuse_ceremony(setup):
    ceremonies, client, operation_id, predecessor, replacement = await retired_predecessor(setup)
    request = ceremonies.reserve_replacement(operation_id, predecessor, authorize=authorized)
    original = ceremonies.row(operation_id)
    result = replacement.replace(request, json.loads(original["binding_json"]), before_send=authorized)
    with ceremonies.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_browser_ceremonies SET domain_status='committed' WHERE operation_id=?",
            (operation_id,))
    with pytest.raises(HostedGrantUnavailable):
        ceremonies._adopt_replacement(operation_id, request, result["successor"], preparing=True, authorize=authorized)
    assert ceremonies.row(operation_id)["hosted_operation_id"] == original["hosted_operation_id"]
    with ceremonies.store.database.transaction() as connection:
        connection.execute("UPDATE desktop_browser_ceremonies SET domain_status='pending' WHERE operation_id=?",
            (operation_id,))
    reused = {**result["successor"], "ceremony_id": original["ceremony_id"]}
    with pytest.raises(HostedGrantUnavailable):
        ceremonies._adopt_replacement(operation_id, request, reused, preparing=True, authorize=authorized)
    assert ceremonies.row(operation_id)["hosted_operation_id"] == original["hosted_operation_id"]


@pytest.mark.asyncio
async def test_replacement_chain_preserves_each_predecessor(setup):
    ceremonies, client, operation_id, predecessor, replacement = await retired_predecessor(setup)
    root = ceremonies.row(operation_id)
    await ceremonies.replace(operation_id, predecessor, authorize=authorized)
    first = ceremonies.row(operation_id)
    client.state, client.revision = "retired", 3
    await ceremonies.reconcile(operation_id, authorize=authorized)
    second_predecessor = replacement_predecessor(ceremonies, {"operation_id": operation_id})
    await ceremonies.replace(operation_id, second_predecessor, authorize=authorized)
    second = ceremonies.row(operation_id)
    assert len({root["ceremony_id"], first["ceremony_id"], second["ceremony_id"]}) == 3
    assert root["provider_expires_at"] == first["provider_expires_at"] == second["provider_expires_at"]
    assert len(replacement.calls) == 2
    with ceremonies.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM desktop_browser_ceremony_successors").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM desktop_browser_ceremony_current").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_unknown_successor_does_not_hide_committed_activation(tmp_path):
    from app.security.browser_ceremony import BrowserCeremonyRefused
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: NOW)
    admissions = ScopedAdmissions(store, stage="runtime")
    native = admissions.consume(admissions.prepare_desktop(), role="desktop")
    owner = Owner()
    owner.facts = dict.fromkeys(owner.facts, "verified")
    owner.facts["activation"] = "missing"
    coordinator = DesktopOnboardingCoordinator(admissions, owner)
    operation, _ = coordinator._accept(native, command(coordinator.read(native), "activation.review"))
    binding = copy.deepcopy(vector("prepare", "activation")["request"]["binding"])
    client = Client()
    replacement = ReplacementClient(client)
    ceremonies = BrowserCeremonies(store, client, hosted_origin="https://provider.example", replacement_client=replacement)
    operation_id = operation["operation_id"]
    await ceremonies.prepare(operation_id, binding, authorize=authorized)
    client.state, client.revision = "retired", 2
    await ceremonies.reconcile(operation_id, authorize=authorized)
    original = ceremonies.row(operation_id)
    replacement.refused = True
    with pytest.raises(BrowserCeremonyRefused):
        await ceremonies.replace(operation_id, replacement_predecessor(ceremonies, operation), authorize=authorized)
    client.result = {"kind": "activation-redemption", "activation_operation_id": binding["activation_operation_id"],
        "redemption": {"profile": "urn:bridge-clean:capability-license-redemption-continuation:v1",
            "continuation": "clr1." + "A" * 43, "expires_at": binding["source_expires_at"]}}
    result = await ceremonies.reconcile(operation_id, authorize=authorized)
    assert result["snapshot"]["result"] == client.result
    assert ceremonies.row(operation_id)["hosted_operation_id"] == original["hosted_operation_id"]
    assert ceremonies.row(operation_id)["domain_status"] == "committed"
    assert len(replacement.calls) == 1
    assert "clr1." not in json.dumps(ceremonies.row(operation_id))


@pytest.mark.asyncio
async def test_terminal_lineage_retained_until_source_deadline(setup):
    coordinator, ceremonies, client, operation, binding, clock = setup
    binding["source_expires_at"] = (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    operation_id = operation["operation_id"]
    await ceremonies.prepare(operation_id, binding, authorize=authorized)
    client.state, client.revision = "retired", 2
    await ceremonies.reconcile(operation_id, authorize=authorized)
    ceremonies.replacement_client = ReplacementClient(client)
    await ceremonies.replace(operation_id, replacement_predecessor(ceremonies, operation), authorize=authorized)
    coordinator._settle(operation, OperationResult("completed"))
    clock[0] += timedelta(minutes=31)
    native = coordinator.admissions.consume(coordinator.admissions.prepare_desktop(), role="desktop")
    coordinator._accept(native, command(coordinator.read(native), "account.connect"))
    assert ceremonies.row(operation_id) is not None
