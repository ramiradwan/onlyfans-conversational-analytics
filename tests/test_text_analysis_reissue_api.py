"""Real local sessions, durable reissue adoption and bounded browser handoff."""
from datetime import datetime, timezone
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.endpoints import text_analysis_reissue
from app.api.security import webauthn_session_token
from app.core.config import settings
from app.persistence.auth import RevocationKey, RevocationScopeType
from app.security.activation_gate import ActivationDecision, record_activation, reset_activation
from app.services.text_analysis_reissue import configure_reissue_service, reissue_service
from app.services.text_analysis_reissue_navigation import ReissueNavigationEntries
from tests.test_text_analysis_authority import local_signer  # noqa: F401
from tests.text_analysis_reissue_fixture import make_reissue_fixture

pytestmark = pytest.mark.ci_tier('integration')
ORIGIN = 'http://bridge.localhost:17871'
PATH = '/api/v1/nlp/license-reissue'


@pytest.fixture
def http(tmp_path, monkeypatch, local_signer):
    x = make_reissue_fixture(tmp_path, local_signer, now=datetime.now(timezone.utc))
    monkeypatch.setattr(settings, 'auth_database_path', x.auth.database.path)
    monkeypatch.setattr(settings, 'websocket_auth_mode', 'local_session')
    monkeypatch.setattr(settings, 'bridge_origin', ORIGIN)
    monkeypatch.setattr(text_analysis_reissue, '_entries', ReissueNavigationEntries())
    previous = reissue_service()
    configure_reissue_service(x.service)
    reset_activation()
    app = FastAPI()
    app.include_router(text_analysis_reissue.router)
    try:
        assert not app.dependency_overrides
        with TestClient(app, base_url=ORIGIN) as client:
            client.cookies.set(settings.bridge_session_cookie_name,
                webauthn_session_token(x.http_policy().identity, x.identity.session.session_value))
            client.headers.update({'Origin': ORIGIN, 'X-CSRF-Token': x.identity.session.csrf_value})
            yield x, client
    finally:
        configure_reissue_service(previous)
        reset_activation()
        x.client.transport.close()


def _count(x):
    with x.auth.database.read() as db:
        return db.execute('SELECT count(*) FROM text_analysis_reissue_operations').fetchone()[0]


def make_acquisition_fixture(http, monkeypatch, *, kind="preview", schemas=None):
    """Use the stock app session, real installation signatures and fictional Provider replies."""
    from datetime import timedelta
    from hashlib import sha256
    from types import SimpleNamespace

    import httpx

    from app.api.endpoints import text_analysis_native_acquisition as endpoint
    from app.security.text_analysis_acquisition import PAID_TERMS
    from app.security.text_analysis_acquisition_transport import NativeAcquisitionClient, NativeAcquisitionTransport, ROUTES
    from app.security.text_analysis_contract import OUTPUT_POLICY, PREFIX, REQUIRED_LICENSE, REQUIRED_PREVIEW, canonical
    from app.security.text_analysis_proof import ProofContext, prepare_request, verify_proof, _operation
    from app.services.text_analysis_native_acquisition import AcquisitionSelection, TextAnalysisNativeAcquisition
    from tests.test_text_analysis_reissue_transport import response

    source, client = http
    client.app.include_router(endpoint.router)
    schemas = schemas or source.client.schemas
    authority = "preview" if kind == "preview" else "license"
    binding = source.context.binding
    scope = dict(seat_id="fictional-seat", seat_scope="organization-installation-seat",
                 capability="text-analysis", compatible_artifact_family="text-analysis-artifact",
                 licensed_major_version=1, update_rights=True, fallback_major_versions=[])
    if kind == "preview":
        scope.update(execution_limit=1, output_policy=OUTPUT_POLICY)
    prepare = json.loads(source.context.original_prepare)
    release = canonical(prepare["release_identity"])
    profiles = tuple(sorted(REQUIRED_PREVIEW if kind == "preview" else REQUIRED_LICENSE))
    selected = AcquisitionSelection(release, canonical(scope), profiles)
    state = SimpleNamespace(paid_ready=False, consented=False, lose_prepare=False, lose_submit=False,
                            lose_issue=False, issuance=None, adopted=None, calls=[], challenges={},
                            basis_ready=False, basis_calls=[], refuse_basis=False, retained_calls=[],
                            current_release=release, retained_releases={release}, retained_scope=selected.expected_scope,
                            retained_profiles=profiles, prepares={})
    fixed_delivery = b'{"test_delivery":"opaque fixture"}'

    def stamp(seconds=0):
        return (source.clock[0] + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def reply(value, status=200):
        return response(canonical(value), status=status)

    def handle(request):
        path = request.url.path
        value = json.loads(request.content)
        assert not {"cookie", "authorization", "origin"} & set(request.headers)
        if path.endswith("proof-challenges"):
            name = next(name for name, route in ROUTES.items()
                        if route.challenge == path and _operation(name).purpose == value["purpose"])
            proof = ProofContext(name, **binding, public_key_jwk=source.context.public_key_jwk,
                                 expected_request=canonical(value["request"]))
            prepared = prepare_request(proof.expected_request, context=proof, schemas=schemas)
            challenge_id = "019a1957-0000-7000-8000-" + str(len(state.challenges) + 1).zfill(12)
            challenge = dict(profile=value["profile"], purpose=value["purpose"], audience=_operation(name).audience,
                             challenge_id=challenge_id, challenge="A" * 43, request_digest=prepared.request_digest.hex(),
                             issued_at=stamp(), expires_at=stamp(60))
            state.challenges[challenge_id] = (name, proof, prepared, challenge)
            return reply(challenge)
        supplied = value["proof"]
        name, proof, signed_request, challenge = state.challenges.pop(supplied["challenge_id"])
        assert path == ROUTES[name].operation and canonical(value["request"]) == proof.expected_request
        assert verify_proof(supplied["signature"], prepared=signed_request, challenge_id=challenge["challenge_id"],
                            challenge=challenge["challenge"], context=proof, schemas=schemas)
        inner = value["request"]
        state.calls.append((name, inner))
        if name == "authority-recover":
            assert inner["issuance_request_digest"] == sha256(state.issuance).hexdigest()
            return response(fixed_delivery)
        if name.endswith("-issue"):
            state.issuance = canonical(inner)
            if state.lose_issue:
                raise httpx.ReadError("fixture disconnect after issuance")
            return response(fixed_delivery)
        outer = dict(profile=inner["profile"], operation_id=inner["operation_id"], binding=binding)
        if kind == "paid":
            outer["terms"] = PAID_TERMS
        if name.endswith("-prepare"):
            assert state.prepares.setdefault(inner["operation_id"], inner) == inner
        if name.endswith("-prepare") or not state.paid_ready:
            outer.update(state="awaiting-authentication" if kind == "preview" else "awaiting-payment",
                         expires_at=stamp(1800))
            if name.endswith("-prepare"):
                outer["entry_reference"] = "A" * 43
                if state.lose_prepare:
                    raise httpx.ReadError("fixture disconnect after prepare")
            return reply(outer)
        expiry = stamp(300)
        original = state.prepares[inner["operation_id"]]
        confirmation = dict(operation_id=inner["operation_id"],
            issuance_authorization_id="019a1957-0000-7000-8000-000000000042",
            binding=binding, authority_profile=PREFIX + authority + ":v1", scope=scope,
            supported_profiles=original["supported_profiles"], release_identity=original["release_identity"])
        outer.update(expires_at=expiry, confirmation=confirmation, confirmation_digest=sha256(canonical(confirmation)).hexdigest())
        action = "acquire-text-analysis-preview" if kind == "preview" else "activate-paid-text-analysis"
        terms = PAID_TERMS if kind == "paid" else {k: v for k, v in scope.items() if k not in ("seat_id", "seat_scope")}
        decision = dict(action=action, expires_at=expiry, terms=terms)
        if name.endswith("-submit"):
            assert inner["consent"] == action and inner["decision_digest"] == sha256(canonical(decision)).hexdigest()
            assert inner["confirmation_digest"] == outer["confirmation_digest"]
            state.consented = True
            if state.lose_submit:
                raise httpx.ReadError("fixture disconnect after submit")
        if state.consented:
            return reply(outer | {"state": "authorized"})
        return reply(outer | dict(state="confirmation-required", decision=decision,
                    decision_digest=sha256(canonical(decision)).hexdigest(), decision_reference="A" * 43))

    class AuthorityOwner:
        def read_current(self, policy, *, kind):
            if kind == authority and policy.identity.creator_account_id == "account":
                return state.adopted
            return None

        def adopt(self, policy, *, issued_from, original_issuance_request, delivery):
            assert policy.identity.creator_account_id == "account"
            assert issued_from.context.authority_kind == authority
            assert delivery == fixed_delivery and state.issuance == original_issuance_request
            state.adopted = issued_from

    transport = NativeAcquisitionTransport("https://provider.example", transport=httpx.MockTransport(handle))
    wire = NativeAcquisitionClient(transport, source.client.key_authority, schemas, clock=lambda: source.clock[0])

    def select(policy, selected_kind, registered, *, refresh):
        assert registered == binding
        state.basis_calls.append(refresh)
        if refresh:
            if state.refuse_basis:
                raise OSError("fictional descriptor unavailable")
            state.basis_ready = True
            state.retained_releases.add(state.current_release)
        assert state.basis_ready
        return AcquisitionSelection(state.current_release, selected.expected_scope, profiles) if kind == selected_kind else None

    def select_retained(policy, selected_kind, registered, original):
        assert registered == binding
        state.retained_calls.append(original)
        if selected_kind != kind or original not in state.retained_releases:
            raise ValueError("fictional retained descriptor unavailable")
        return AcquisitionSelection(original, state.retained_scope, state.retained_profiles)

    def reopen():
        return TextAnalysisNativeAcquisition(source.auth, wire, AuthorityOwner(), select=select,
                select_retained=select_retained, eligible=(
                lambda _policy, selected_kind, registered: selected_kind == kind and registered == binding),
                                             payment_mode="test", clock=lambda: source.clock[0])

    monkeypatch.setattr(endpoint, "native_text_analysis_acquisition", reopen)
    return source, client, state


@pytest.fixture(params=("preview", "paid"))
def native_acquisition_case(request, http):
    from app.security.text_analysis_acquisition import _schema

    kind = request.param
    schemas = http[0].client.schemas
    if not schemas.has_schema(_schema(kind, "prepare-response")):
        pytest.skip("requires the pinned paid acquisition schema family")
    return kind, schemas


def test_native_acquisition_recovers_issued_delivery_across_compatible_update(http, monkeypatch, native_acquisition_case):
    from datetime import timedelta
    from hashlib import sha256

    from app.security.text_analysis_contract import canonical

    kind, schemas = native_acquisition_case
    source, client, provider = make_acquisition_fixture(http, monkeypatch, kind=kind, schemas=schemas)
    route = f"/api/v1/nlp/text-analysis-acquisition/{kind}"
    original = provider.current_release
    prepared = client.post(route + "/prepare", json={}).json()["current"]
    request = route + "/" + prepared["flow_id"]
    provider.paid_ready = True
    review = client.post(request + "/read", json={}).json()["current"]["review"]
    assert review["terms"] == ({"execution_limit": 1} if kind == "preview" else {
        "payment_mode": "test", "amount_minor": 999, "currency": "EUR", "licensed_major_version": 1,
        "update_rights": True,
    })
    provider.lose_issue = True
    assert client.post(request + "/submit", json={"review_id": review["review_id"]}).status_code == 503
    assert client.get(route).json()["current"]["state"] == "authorized"
    assert provider.adopted is None
    assert canonical(json.loads(provider.issuance)["release_identity"]) == original
    assert json.loads(provider.issuance)["supported_profiles"] == provider.calls[0][1]["supported_profiles"]
    source.clock[0] += timedelta(minutes=6)
    provider.current_release = canonical(json.loads(original) | {"artifact_sha256": "e" * 64})
    provider.basis_calls.clear()
    provider.refuse_basis = True

    recovery = client.post(request + "/read", json={})
    assert recovery.status_code == 200 and recovery.json()["current"]["state"] == "adopted"
    assert provider.calls[-1][1]["issuance_request_digest"] == sha256(provider.issuance).hexdigest()
    assert [name for name, _ in provider.calls] == [
        kind + "-prepare", kind + "-read", kind + "-submit",
        ("license" if kind == "paid" else "preview") + "-issue", "authority-recover",
    ]
    assert provider.adopted.context.release_identity == original
    assert provider.basis_calls == [] and set(provider.retained_calls) == {original}

    # Verified ownership takes priority even if the old acquisition journal
    # can no longer be used for a new exchange.
    provider.retained_releases.clear()
    provider.retained_calls.clear()
    assert client.get(route).json() == {"eligible": False, "reason": "already_acquired", "current": None}
    assert client.post(route + "/prepare", json={}).status_code == 409
    assert provider.basis_calls == provider.retained_calls == []
    provider.retained_releases.add(original)
    provider.adopted = None
    assert client.get(route).json() == {"eligible": False, "reason": "account_unavailable", "current": None}
    assert client.post(request + "/read", json={}).status_code == 409


def test_native_acquisition_refuses_unrelated_release_seat_profiles_and_account(http, monkeypatch, native_acquisition_case):
    from app.security.text_analysis_contract import SUPPORTED, canonical
    from tests.test_text_analysis_identity import make_identity_fixture

    kind, schemas = native_acquisition_case
    source, client, provider = make_acquisition_fixture(http, monkeypatch, kind=kind, schemas=schemas)
    route = f"/api/v1/nlp/text-analysis-acquisition/{kind}"
    original = provider.current_release
    provider.current_release = canonical(json.loads(original) | {"artifact_sha256": "e" * 64})
    prepared = client.post(route + "/prepare", json={}).json()["current"]
    assert canonical(provider.calls[0][1]["release_identity"]) == provider.current_release
    request = route + "/" + prepared["flow_id"]

    provider.retained_releases.remove(provider.current_release)
    assert client.post(request + "/read", json={}).status_code == 409
    provider.retained_releases.add(provider.current_release)
    expected_scope = provider.retained_scope
    provider.retained_scope = canonical(json.loads(expected_scope) | {"seat_id": "unrelated-seat"})
    assert client.post(request + "/read", json={}).status_code == 409
    provider.retained_scope = expected_scope
    expected_profiles = provider.retained_profiles
    provider.retained_profiles = SUPPORTED
    assert client.post(request + "/read", json={}).status_code == 409
    provider.retained_profiles = expected_profiles

    source.auth.revoke(RevocationKey(RevocationScopeType.WEBAUTHN_CREDENTIAL, "identity-credential-person"))
    other = make_identity_fixture(source.auth.database.path, authentication=source.auth, scope=source.context.binding,
                                  principal_id="other-person", creator_account_id="other-account", now=source.clock[0],
                                  grant_id_offset=10)
    client.cookies.set(settings.bridge_session_cookie_name,
                       webauthn_session_token(other.policy.identity, other.session.session_value))
    client.headers["X-CSRF-Token"] = other.session.csrf_value
    assert client.get(route).json() == {"eligible": True, "reason": None, "current": None}
    assert client.post(request + "/read", json={}).status_code == 409
    assert len(provider.calls) == 1 and provider.adopted is None


def test_native_preview_reconciles_lost_prepare_submit_and_issue_without_new_consent(http, monkeypatch):
    from hashlib import sha256

    from app.security.text_analysis_contract import canonical

    source, client, provider = make_acquisition_fixture(http, monkeypatch)
    route = "/api/v1/nlp/text-analysis-acquisition/preview"
    initial = client.get(route)
    assert initial.status_code == 200 and initial.json() == {"eligible": True, "reason": None, "current": None}
    assert provider.calls == [] and provider.basis_calls == []
    provider.lose_prepare = True
    assert client.post(route + "/prepare", json={}).status_code == 503
    pending = client.get(route).json()["current"]
    assert pending["state"] == "prepared"
    same = route + "/" + pending["flow_id"]
    assert client.post(same + "/read", json={}).json()["current"]["state"] == "awaiting_browser"
    provider.current_release = canonical(json.loads(provider.current_release) | {"artifact_sha256": "e" * 64})
    provider.basis_calls.clear()
    provider.lose_prepare = False
    prepared = client.post(route + "/prepare", json={}).json()["current"]
    assert prepared["flow_id"] == pending["flow_id"] and prepared["navigation_available"]
    assert provider.calls[0][1] == provider.calls[2][1]
    assert provider.basis_calls == []
    code = client.post(same + "/browser-entry", json={}).json()["entry_code"]
    with TestClient(client.app, base_url=ORIGIN) as browser:
        assert browser.get("/text-analysis-acquisition/preview/entry").status_code == 200
        consumed = browser.post(route + "/browser-entry/consume", json={"code": code}, headers={"Origin": ORIGIN})
        assert consumed.json()["url"] == "https://provider.example/onboarding/text-analysis-preview-acquisitions/" + "A" * 43
        assert browser.post(route + "/browser-entry/consume", json={"code": code}, headers={"Origin": ORIGIN}).status_code == 401
    assert len(provider.calls) == 3
    provider.paid_ready = True
    reviewed = client.post(same + "/read", json={})
    assert reviewed.status_code == 200, reviewed.text
    review = reviewed.json()["current"]["review"]
    assert review["terms"] == {"execution_limit": 1} and "seat_id" not in reviewed.text
    assert client.post(same + "/submit", json={"review_id": "forged"}).status_code == 422
    assert provider.calls[-1][0] == "preview-read"
    provider.lose_submit = provider.lose_issue = True
    assert client.post(same + "/submit", json={"review_id": review["review_id"]}).status_code == 503
    assert client.get(route).json()["current"]["state"] == "confirming"
    assert client.post(same + "/read", json={}).status_code == 503
    assert client.get(route).json()["current"]["state"] == "authorized"
    final = client.post(same + "/read", json={})
    assert final.status_code == 200 and final.json()["current"]["state"] == "adopted"
    assert [name for name, _ in provider.calls[-4:]] == ["preview-submit", "preview-read", "preview-issue", "authority-recover"]
    assert provider.calls[-1][1]["issuance_request_digest"] == sha256(provider.issuance).hexdigest()
    with source.auth.database.read() as db:
        assert db.execute("SELECT count(*) FROM text_analysis_native_acquisitions").fetchone()[0] == 1
    assert client.get(route).json()["reason"] == "already_acquired"


def test_native_acquisition_requires_same_origin_and_keeps_paid_disabled_without_signed_schema(http, monkeypatch):
    _, client, provider = make_acquisition_fixture(http, monkeypatch)
    preview = "/api/v1/nlp/text-analysis-acquisition/preview"
    paid = "/api/v1/nlp/text-analysis-acquisition/paid"
    assert client.get(paid).json()["eligible"] is False
    assert client.get(preview + "?mode=test").status_code == 422
    assert client.post(preview + "/prepare", json={}, headers={"Origin": "https://wrong.example"}).status_code == 403
    assert client.post(preview + "/prepare", json={"conversation": "synthetic"}).status_code == 422
    assert provider.calls == []


def test_native_acquisition_descriptor_failure_and_external_authorization_never_issue(http, monkeypatch):
    _, client, provider = make_acquisition_fixture(http, monkeypatch)
    route = "/api/v1/nlp/text-analysis-acquisition/preview"
    provider.refuse_basis = True
    assert client.get(route).json()["eligible"] and provider.basis_calls == []
    assert client.post(route + "/prepare", json={}).status_code == 503
    assert client.get(route).json()["current"] is None and provider.calls == []
    provider.refuse_basis = False
    prepared = client.post(route + "/prepare", json={}).json()["current"]
    request = route + "/" + prepared["flow_id"]
    provider.paid_ready = True
    assert client.post(request + "/read", json={}).status_code == 200
    provider.consented = True
    assert client.post(request + "/read", json={}).status_code == 409
    assert provider.issuance is None and provider.adopted is None
    assert client.get(route).json()["current"]["state"] == "confirmation_required"


def test_inert_status_and_explicit_actual_client_adoption_without_module_authority(http):
    x, client = http
    assert x.http_policy().text_analysis_authority is None
    ensures = x.client.key_authority.ensure_calls
    assert client.get(PATH).json() == {'current': None}
    assert client.get(PATH).json() == {'current': None}
    assert _count(x) == 0 and x.requests == []
    assert x.client.key_authority.ensure_calls == ensures
    first = client.post(PATH, json={})
    assert first.status_code == 200, first.text
    current = first.json()['current']
    assert set(current) == {'flow_id', 'state', 'navigation_available'}
    assert current['state'] == 'awaiting_authorization' and current['navigation_available']
    assert len(x.requests) == 2 and _count(x) == 1
    assert client.get(PATH).json() == first.json()
    assert len(x.requests) == 2
    finished = client.post(PATH + '/' + current['flow_id'] + '/resume', json={})
    assert finished.status_code == 200, finished.text
    assert finished.json()['current'] == dict(current, state='adopted', navigation_available=False)
    with x.auth.database.read() as db:
        row = db.execute('SELECT result,reference_id FROM text_analysis_reissue_operations').fetchone()
        assert bytes(row['result']).startswith(b' ') and bytes(row['result']).endswith(b'\n')
        assert json.loads(row['result'])['signed_authority']
        assert row['reference_id']
    requests = len(x.requests)
    assert client.post(PATH + '/' + current['flow_id'] + '/resume', json={}).json() == finished.json()
    assert len(x.requests) == requests
    assert all(used for _, used in x.challenges.values())
    assert 'signed_authority' not in finished.text and 'seat_id' not in finished.text
    assert finished.headers['cache-control'] == 'no-store'


def test_browser_entry_requires_activation_without_consuming_its_ticket(http, monkeypatch):
    x, client = http
    monkeypatch.setattr(text_analysis_reissue, '_entries', ReissueNavigationEntries(clock=lambda: 1.0))
    flow = client.post(PATH, json={}).json()['current']['flow_id']
    code = client.post(PATH + '/' + flow + '/browser-entry', json={}).json()['entry_code']
    before = len(x.requests)
    record_activation(ActivationDecision(activated=False, production=True))

    with TestClient(client.app, base_url=ORIGIN) as browser:
        for path in ('/text-analysis-reissue/entry', '/text-analysis-reissue/entry.js'):
            assert browser.get(path).status_code == 503
        refused = browser.post(PATH + '/browser-entry/consume', json={'code': code}, headers={'Origin': ORIGIN})
        assert refused.status_code == 503
        assert len(x.requests) == before and _count(x) == 1

        record_activation(ActivationDecision(activated=True, production=True))
        accepted = browser.post(PATH + '/browser-entry/consume', json={'code': code}, headers={'Origin': ORIGIN})
        assert accepted.status_code == 200
        assert accepted.json() == {'url': 'https://example.test/v2/onboarding/text-analysis-license-reissues/' + 'A'*43}
        assert not accepted.cookies and len(x.requests) == before


def test_one_use_browser_entry_has_no_authority_or_hidden_coordinates(http):
    x, client = http
    flow = client.post(PATH, json={}).json()['current']['flow_id']
    entry = client.post(PATH + '/' + flow + '/browser-entry', json={})
    assert entry.status_code == 200, entry.text
    assert set(entry.json()) == {'entry_code'}
    assert len(entry.json()['entry_code']) == 43
    before = len(x.requests)
    with TestClient(client.app, base_url=ORIGIN) as browser:
        landing = browser.get('/text-analysis-reissue/entry')
        assert landing.status_code == 200 and 'entry_code' not in landing.text
        assert "frame-ancestors 'none'" in landing.headers['content-security-policy']
        script = browser.get('/text-analysis-reissue/entry.js')
        assert script.status_code == 200 and script.headers['content-type'].startswith('text/javascript')
        assert script.text == (text_analysis_reissue._assets / 'reissue_entry.js').read_text(encoding='utf-8')
        opened = browser.post(PATH + '/browser-entry/consume', json={'code': entry.json()['entry_code']}, headers={'Origin': ORIGIN})
        assert opened.status_code == 200, opened.text
        assert opened.json() == {'url': 'https://example.test/v2/onboarding/text-analysis-license-reissues/' + 'A'*43}
        assert not opened.cookies
        assert browser.post(PATH + '/browser-entry/consume', json={'code': entry.json()['entry_code']}, headers={'Origin': ORIGIN}).status_code == 401
    assert len(x.requests) == before
    assert client.get(PATH).json()['current']['state'] == 'awaiting_authorization'


@pytest.mark.parametrize('fault', ['session', 'origin', 'host', 'csrf', 'extra', 'duplicate', 'encoding', 'large', 'query'])
def test_mutation_refuses_before_new_intent_or_hosted_request(http, fault):
    x, client = http
    headers = {'Content-Type': 'application/json'}
    raw, path = b'{}', PATH
    if fault == 'session':
        client.cookies.clear()
    if fault == 'origin':
        headers['Origin'] = 'https://foreign.invalid'
    if fault == 'host':
        headers['Host'] = 'foreign.invalid'
    if fault == 'csrf':
        headers['X-CSRF-Token'] = 'invalid'
    if fault == 'extra':
        raw = b'{"release_identity":{}}'
    if fault == 'duplicate':
        raw = b'{"x":1,"x":2}'
    if fault == 'encoding':
        headers['Content-Encoding'] = 'gzip'
    if fault == 'large':
        raw = b' '*257
    if fault == 'query':
        path += '?scope=untrusted'
    response = client.post(path, content=raw, headers=headers)
    assert response.status_code == (401 if fault == 'session' else 403 if fault in ('origin','host','csrf')
        else 415 if fault in ('encoding','query') else 413 if fault == 'large' else 422)
    assert _count(x) == 0 and x.requests == []


def test_revoked_local_session_cannot_discover_or_resume_reissue(http):
    x, client = http
    flow = client.post(PATH, json={}).json()['current']['flow_id']
    x.auth.revoke(RevocationKey(RevocationScopeType.BRIDGE_SESSION, x.http_policy().identity.session_id))
    before = len(x.requests)
    assert client.get(PATH).status_code == 401
    assert client.post(PATH + '/' + flow + '/resume', json={}).status_code == 401
    assert len(x.requests) == before and _count(x) == 1


def test_unconfigured_provider_refuses_without_creating_key_intent_or_request(http):
    x, client = http
    configure_reissue_service(None)
    before = x.client.key_authority.ensure_calls
    assert client.get(PATH).status_code == 503
    assert client.post(PATH, json={}).status_code == 503
    assert _count(x) == 0 and x.requests == [] and x.client.key_authority.ensure_calls == before


def test_browser_entry_requires_fixed_origin_and_closed_bounded_body(http):
    _, client = http
    assert client.get('/text-analysis-reissue/entry?code=private').status_code == 400
    assert client.get('/text-analysis-reissue/entry', headers={'Host': 'foreign.invalid'}).status_code == 421
    for raw, headers, expected in (
        (b'{"code":"' + b'A'*43 + b'"}', {'Origin': 'https://foreign.invalid'}, 403),
        (b'{"code":"A","code":"B"}', {'Origin': ORIGIN}, 422),
        (b' '*257, {'Origin': ORIGIN}, 413),
    ):
        response = client.post(PATH + '/browser-entry/consume', content=raw, headers={'Content-Type': 'application/json', **headers})
        assert response.status_code == expected


def test_entry_expiry_replacement_and_capacity_preserve_prepared_operations():
    now = [0.0]
    entries = ReissueNavigationEntries(clock=lambda: now[0])
    url = 'https://example.test/v2/onboarding/text-analysis-license-reissues/' + 'A'*43
    first = entries.issue(('owner', 'one'), url)
    replacement = entries.issue(('owner', 'one'), url)
    with pytest.raises(ValueError):
        entries.consume(first)
    now[0] = 30
    with pytest.raises(ValueError):
        entries.consume(replacement)
    codes = [entries.issue(('owner', str(index)), url) for index in range(64)]
    with pytest.raises(ValueError):
        entries.issue(('owner', 'overflow'), url)
    assert entries.consume(codes[0]) == url
    entries.issue(('owner', 'replacement'), url)
    for invalid in ('http://example.test/v2/onboarding/text-analysis-license-reissues/' + 'A'*43,
                    url + '?scope=foreign', url + '#private', 'https://user@example.test/v2/onboarding/text-analysis-license-reissues/' + 'A'*43):
        with pytest.raises(ValueError):
            entries.issue(('owner', 'bad'), invalid)
