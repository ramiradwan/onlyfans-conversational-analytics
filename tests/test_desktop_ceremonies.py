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
        self.validator = BrowserCeremonyClient(None, None, clock=lambda: NOW)

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
        result = {"profile": PROFILE, "operation_id": request["operation_id"], "status": "found",
            "snapshot": {"operation_id": request["operation_id"], "ceremony_id": vector()["request"]["ceremony_id"],
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
