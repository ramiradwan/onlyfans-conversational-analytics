from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.persistence.auth import SQLiteAuthenticationStore
from app.security.activation_delivery_recovery import ActivationDeliveryJournal, ActivationDeliveryRecovery
from app.security.capability_license_composition import CapabilityLicenseDeliveryReceipt
from app.security.capability_license_redemption_journal import SQLiteRedemptionJournal, RedemptionJournalError
from app.security.delivery_recovery_contract import (
    AUDIENCE, CHALLENGE_PATH, ORDER, PREFIX, PROFILE, PROOF_PROFILE, DeliveryRecoveryClient,
    RecoveryUnavailable, canonical, decode, proof_bytes, unbase64, validate,
)
from app.security.hosted_grants import TransportResponse
from app.security.installation_key import InstallationProof, verify_installation_proof
from test_capability_license_opaque_redemption import NOW, ProofAuthority, _key

pytestmark = [pytest.mark.ci_tier("fast")]
FAMILY = Path(__file__).resolve().parents[1] / "contracts/capability-license-delivery-recovery-v1"
OPERATION = "019a00b0-0000-7000-8000-000000000034"
ORIGIN = "https://control-plane.example.invalid"
BINDING = {"organization_id": "org.acme", "installation_id": "install.primary",
    "installation_key_id": _key().installation_key_id, "installation_key_jkt": _key().installation_key_jkt,
    "creator_account_id": "creator.fixture"}


def cases(name):
    return json.loads((FAMILY / (name + "-cases.json")).read_text("utf-8"))


@pytest.mark.parametrize("case", cases("parsing"), ids=lambda row: row["case_id"])
def test_product_exact_numeric_parser(case):
    try:
        valid = canonical(decode(case["document"].encode("utf-8"), limit=case["limit"])).hex() == case.get("canonical_hex")
    except (ValueError, UnicodeError):
        valid = False
    assert valid is case["valid"]


@pytest.mark.parametrize("case", cases("schema"), ids=lambda row: row["case_id"])
def test_product_closed_recovery_schemas(case):
    name = Path(case["schema"]).name.removeprefix("capability-license-delivery-recovery-").removesuffix(".schema.json")
    try:
        validate(name, case["value"])
        valid = True
    except (ValueError, UnicodeError):
        valid = False
    assert valid is case["valid"]


@pytest.mark.parametrize("case", cases("proof"), ids=lambda row: row["case_id"])
def test_product_independent_recovery_proof_bytes(case):
    try:
        action = case["operation"].removeprefix("capability-license-delivery-")
        validate("proof-challenge-request", {"profile": PROOF_PROFILE, "purpose": case["operation"], "request": case["request"]})
        raw = proof_bytes(action, case["request"], case["challenge"])
        signature = unbase64(case["signature"], 64)
        jwk = case["public_key"]
        thumbprint = base64.urlsafe_b64encode(hashlib.sha256(canonical(jwk)).digest()).rstrip(b"=").decode()
        key_id = case["request"]["binding"]["installation_key_id"]
        valid = (0 < int.from_bytes(signature[32:], "big") <= ORDER // 2
            and case["request"]["binding"]["installation_key_jkt"] == thumbprint
            and hashlib.sha256(canonical(case["request"])).hexdigest() == case["request_digest"]
            and verify_installation_proof(json.dumps({**jwk, "kid": key_id}), raw,
                InstallationProof(key_id, "ES256", signature)))
        if valid:
            assert raw.hex() == case["proof_bytes_hex"]
    except (ValueError, UnicodeError, KeyError):
        valid = False
    assert valid is case["valid"]


def stamp(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def response(value, status=200):
    return TransportResponse(status, canonical(value), "application/json")


class Provider:
    """Public-contract peer with deterministic loss before or after commit."""
    def __init__(self, clock):
        self.clock, self.generation, self.accepted = clock, 0, None
        self.expires = NOW
        self.calls, self.challenges, self.used = [], 0, set()
        self.loss = None
        self.before_recover = lambda _: None

    def handoff(self):
        return {"profile": "urn:bridge-clean:capability-license-redemption-continuation:v1",
            "continuation": "clr1." + base64.urlsafe_b64encode(bytes([self.generation]) * 32).rstrip(b"=").decode(),
            "expires_at": stamp(self.expires)}

    def request(self, method, path, *, json_body, headers=None):
        assert method == "POST" and headers is None
        self.calls.append((path, json_body))
        if path == CHALLENGE_PATH:
            self.challenges += 1
            return response({"profile": PROOF_PROFILE, "purpose": json_body["purpose"],
                "proof_challenge_id": "019a00b0-0000-7000-8000-000000000035",
                "challenge": base64.urlsafe_b64encode(self.challenges.to_bytes(32, "big")).rstrip(b"=").decode(),
                "audience": AUDIENCE, "request_digest": hashlib.sha256(canonical(json_body["request"])).hexdigest(),
                "issued_at": stamp(self.clock[0]), "expires_at": stamp(self.clock[0] + timedelta(seconds=60))}, 201)
        request = json_body["request"]
        requested = request["delivery_generation"]
        if path == PREFIX + "recover" and self.accepted is not None and requested == self.generation:
            return response(self.accepted)
        challenge = json_body["proof"]["challenge"]
        if challenge in self.used:
            return response({"profile": PROFILE, "reason": "proof_invalid"}, 401)
        self.used.add(challenge)
        if path == PREFIX + "read":
            result = {"profile": PROFILE, "activation_operation_id": OPERATION, "binding": BINDING,
                "requested_generation": requested, "current_generation": self.generation}
            if requested is not None and requested > self.generation:
                return response({**result, "state": "not-issued"})
            if requested is not None and requested < self.generation:
                return response({**result, "state": "superseded"})
            if self.clock[0] < self.expires:
                return response({**result, "state": "authorization-available", "redemption": self.handoff()})
            return response({**result, "state": "authorization-expired", "expires_at": stamp(self.expires),
                "retry_at": stamp(self.expires)})
        assert path == PREFIX + "recover" and requested == self.generation + 1
        self.before_recover(json_body)
        if self.loss == "before":
            self.loss = None
            raise OSError("result unavailable")
        self.generation += 1
        self.expires = self.clock[0] + timedelta(seconds=600)
        self.accepted = {"profile": PROFILE, "activation_operation_id": OPERATION, "binding": BINDING,
            "delivery_generation": self.generation, "authorized_at": stamp(self.clock[0]), "redemption": self.handoff()}
        if self.loss == "after":
            self.loss = None
            raise OSError("result unavailable")
        return response(self.accepted, 201)


@pytest.mark.parametrize("skew", [-300, -5, -0.35, 0.35, 5, 300])
def test_fresh_challenge_does_not_require_synchronized_desktop_clock(monkeypatch, skew):
    import app.security.delivery_recovery_contract as contract

    class DesktopClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(seconds=skew)

    monkeypatch.setattr(contract, "datetime", DesktopClock)
    provider = Provider([NOW])
    client = DeliveryRecoveryClient(provider, ProofAuthority(_key()))
    result = client.read(OPERATION, BINDING)
    assert result["state"] == "authorization-expired"
    assert [path for path, _ in provider.calls] == [CHALLENGE_PATH, PREFIX + "read"]


@pytest.mark.parametrize("expired_during", ["transport", "signing"])
def test_challenge_lifetime_includes_round_trip_and_signing(expired_during):
    ticks = iter([100, 160] if expired_during == "transport" else [100, 159.9, 160])
    provider = Provider([NOW])
    signed = []

    class Key:
        def sign_challenge(self, value):
            signed.append(value)
            return ProofAuthority(_key()).sign_challenge(value)

    client = DeliveryRecoveryClient(provider, Key(), monotonic=lambda: next(ticks))
    with pytest.raises(RecoveryUnavailable, match="proof expired"):
        client.read(OPERATION, BINDING)
    assert bool(signed) is (expired_during == "signing")
    assert [path for path, _ in provider.calls] == [CHALLENGE_PATH]


@pytest.mark.parametrize("change", ["purpose", "digest", "zero_lifetime", "long_lifetime"])
def test_clock_independent_challenge_retains_binding_and_lifetime_checks(change):
    class ChangedProvider(Provider):
        def request(self, method, path, **kwargs):
            original = super().request(method, path, **kwargs)
            value = json.loads(original.body)
            if path == CHALLENGE_PATH:
                if change == "purpose":
                    value["purpose"] = "capability-license-delivery-recover"
                elif change == "digest":
                    value["request_digest"] = "a" * 64
                else:
                    value["expires_at"] = stamp(NOW + timedelta(seconds=0 if change == "zero_lifetime" else 61))
            return response(value, original.status_code)

    provider = ChangedProvider([NOW])
    client = DeliveryRecoveryClient(provider, ProofAuthority(_key()))
    with pytest.raises(RecoveryUnavailable):
        client.read(OPERATION, BINDING)
    assert [path for path, _ in provider.calls] == [CHALLENGE_PATH]


@pytest.fixture
def workflow(tmp_path):
    clock = [NOW]
    database_path = tmp_path / "auth.sqlite3"
    store = SQLiteAuthenticationStore(database_path)
    journal = ActivationDeliveryJournal(store.database, ORIGIN)
    journal.reserve(OPERATION, BINDING)
    provider = Provider(clock)
    receipt = CapabilityLicenseDeliveryReceipt("ref.fixture", "license.fixture", "issuance.fixture")

    class Redeemer:
        def __init__(self):
            self.calls = []
            self.result = receipt

        def redeem(self, **values):
            row = journal.require(OPERATION, BINDING)
            assert row["redemption_json"]["continuation"] == values["continuation"]
            self.calls.append(values)
            return self.result

    redeem = Redeemer()
    def build():
        reopened = SQLiteAuthenticationStore(database_path)
        retained = ActivationDeliveryJournal(reopened.database, ORIGIN)
        client = DeliveryRecoveryClient(provider, ProofAuthority(_key()))
        return ActivationDeliveryRecovery(retained, client, redeem, now=lambda: clock[0])

    provider.before_recover = lambda body: body == journal.require(OPERATION, BINDING)["recover_request_json"] or pytest.fail("send preceded journal")
    return build, journal, provider, redeem, clock, receipt


def test_expired_handoff_recovers_automatically_on_the_original_operation(workflow):
    build, journal, provider, redeem, _, receipt = workflow
    assert journal.require(OPERATION, BINDING)["activation_committed"] == 0
    assert build().resume(OPERATION, BINDING, provider.handoff()) == receipt
    row = journal.require(OPERATION, BINDING)
    assert row["activation_committed"] == 1
    assert row["current_generation"] == 1 and row["completed_reference_id"] == receipt.reference_id
    assert len(redeem.calls) == 1
    assert {body["request"]["activation_operation_id"] for _, body in provider.calls} == {OPERATION}


@pytest.mark.parametrize("loss", ["before", "after"])
def test_restart_reconciles_the_same_recovery_generation(workflow, loss):
    build, journal, provider, redeem, clock, receipt = workflow
    provider.loss = loss
    assert build().resume(OPERATION, BINDING, provider.handoff()) == "hosted_unavailable"
    pending = journal.require(OPERATION, BINDING)["recover_request_json"]
    assert pending["request"]["delivery_generation"] == 1 and not redeem.calls
    clock[0] += timedelta(seconds=61)
    assert build().resume(OPERATION, BINDING) == receipt
    recoveries = [body for path, body in provider.calls if path == PREFIX + "recover"]
    assert recoveries[:2] == [pending, pending]
    assert all(body["request"]["delivery_generation"] == 1 for body in recoveries)
    if loss == "before":
        reads = [body["request"]["delivery_generation"] for path, body in provider.calls if path == PREFIX + "read"]
        assert reads == [None, 1, None]
    assert provider.generation == 1


def test_missing_journal_or_changed_binding_cannot_create_delivery(workflow):
    build, journal, provider, _, _, _ = workflow
    with journal.database.transaction() as connection:
        connection.execute("DELETE FROM capability_license_activation_deliveries")
    assert build().resume(OPERATION, BINDING) == "durable_store_unavailable"
    assert provider.calls == []
    journal.reserve(OPERATION, BINDING)
    assert build().resume(OPERATION, {**BINDING, "creator_account_id": "different"}) == "durable_store_unavailable"
    assert provider.calls == []
    with pytest.raises(RedemptionJournalError):
        ActivationDeliveryJournal(journal.database, "https://different.example").require(OPERATION, BINDING)


def test_original_pending_redemption_precedes_expiry_and_generation_recovery(workflow):
    build, journal, provider, redeem, _, _ = workflow
    handoff = provider.handoff()
    journal.handoff(OPERATION, BINDING, 0, handoff)
    scope = {key: value for key, value in BINDING.items() if key != "creator_account_id"}
    SQLiteRedemptionJournal(journal.database, ORIGIN).prepare(handoff["continuation"], scope,
        {"request": {"continuation": handoff["continuation"]}, "proof": {}}, "original.idempotency.key")
    redeem.result = "hosted_unavailable"
    assert build().resume(OPERATION, BINDING) == "hosted_unavailable"
    assert len(redeem.calls) == 1 and provider.calls == []


def test_rate_wait_is_durable_and_does_not_prompt_or_create_another_operation(workflow):
    build, journal, provider, redeem, clock, receipt = workflow
    journal.defer(OPERATION, BINDING, clock[0].timestamp(), (clock[0] + timedelta(seconds=600)).timestamp())
    assert build().resume(OPERATION, BINDING) == "hosted_unavailable"
    assert provider.calls == [] and redeem.calls == []
    clock[0] += timedelta(seconds=600)
    assert build().resume(OPERATION, BINDING) == receipt


def test_authoritative_read_preserves_the_accepted_recovery_result(workflow):
    build, journal, provider, redeem, _, _ = workflow
    redeem.result = "hosted_unavailable"
    assert build().resume(OPERATION, BINDING, provider.handoff()) == "hosted_unavailable"
    accepted = journal.require(OPERATION, BINDING)["accepted_result_json"]
    assert accepted is not None
    journal.handoff(OPERATION, BINDING, 1, accepted["redemption"])
    assert journal.require(OPERATION, BINDING)["accepted_result_json"] == accepted
    with pytest.raises(RedemptionJournalError):
        journal.handoff(OPERATION, BINDING, 0, accepted["redemption"])
    with pytest.raises(RedemptionJournalError):
        journal.handoff(OPERATION, BINDING, 1, {**accepted["redemption"], "continuation": "clr1." + "A" * 43})
