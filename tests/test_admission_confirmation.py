"""Exact-cost confirmation binding, minting, and the reserve entry point."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.security import confirm_admission, csrf_token
from app.core.config import settings
from app.security.admission_confirmation import (
    AdmissionConfirmationError,
    ConfirmationSubject,
    displayed_credit_cost,
    issue_admission_confirmation,
    open_admission_confirmation,
)
from app.security.local_sessions import (
    issue_local_session_token,
    sign_local_document,
    verify_csrf_document,
)
from app.security.permit_reserve_port import (
    ReserveRequest,
    reserve_admission,
)
from app.security.runtime_policy import AuthContext, AuthorizationEpoch, RuntimePolicy


CONFIRMED_AT = 1_760_000_000
CARRIED_COST = 250_000
REPRICED_COST = 250_001


def _identity(
    *,
    principal_id: str = "principal-fixture-001",
    creator_account_id: str = "account-fixture-001",
    session_id: str = "session-fixture-001",
) -> AuthContext:
    return AuthContext(
        principal_id=principal_id,
        creator_account_id=creator_account_id,
        role="creator",
        platform_creator_id="platform-creator-fixture-001",
        session_id=session_id,
        session_expires_at=CONFIRMED_AT + 3_600,
    )


def _policy(identity: AuthContext | None = None) -> RuntimePolicy:
    return RuntimePolicy(
        identity=identity if identity is not None else _identity(),
        authorization_epoch=AuthorizationEpoch(7),
    )


def _subject(**overrides: object) -> ConfirmationSubject:
    fields: dict[str, object] = {
        "organization_id": "organization-fixture-001",
        "installation_id": "installation-fixture-001",
        "request_idempotency_key": "9f1c5a2e-0000-4000-8000-000000000001",
        "permit_id": "01920000-0000-7000-8000-000000000001",
        "job_id": "job-fixture-001",
        "promised_generation_id": "generation-fixture-001",
        "capability": "analysis-run",
        "parameter_digest": "sha256:" + "a" * 64,
        "input_revision": 41,
        "input_selection_digest": "sha256:" + "b" * 64,
        "credit_cost": CARRIED_COST,
    }
    fields.update(overrides)
    return ConfirmationSubject(**fields)  # type: ignore[arg-type]


def _reserve_request(**overrides: object) -> ReserveRequest:
    fields: dict[str, object] = {
        "permit_id": "01920000-0000-7000-8000-000000000001",
        "job_id": "job-fixture-001",
        "capability": "analysis-run",
        "parameter_digest": "sha256:" + "a" * 64,
        "input_revision": 41,
        "input_selection_digest": "sha256:" + "b" * 64,
        "request_idempotency_key": "9f1c5a2e-0000-4000-8000-000000000001",
        "permit_credit_cost": CARRIED_COST,
        "installation_matches": True,
        "permit_state": "available",
        "reserved_job_id": None,
        "durable_output_committed": False,
    }
    fields.update(overrides)
    return ReserveRequest(**fields)  # type: ignore[arg-type]


def _confirmed(subject: ConfirmationSubject | None = None) -> tuple[RuntimePolicy, str]:
    policy = _policy()
    _, token = issue_admission_confirmation(
        policy,
        subject if subject is not None else _subject(),
        issued_at=CONFIRMED_AT,
    )
    return policy, token


def _http_request(
    *,
    host: str = "bridge.localhost:17871",
    origin: str | None = "http://bridge.localhost:17871",
    session_cookie: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = [(b"host", host.encode("ascii"))]
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    if session_cookie is not None:
        headers.append(
            (
                b"cookie",
                f"{settings.bridge_session_cookie_name}={session_cookie}".encode("ascii"),
            )
        )
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": headers,
            "scheme": "http",
            "server": ("127.0.0.1", 17871),
            "client": ("127.0.0.1", 51000),
        }
    )


def test_confirmation_binds_user_permit_job_capability_input_cost_and_time() -> None:
    subject = _subject()
    confirmation, _ = issue_admission_confirmation(
        _policy(), subject, issued_at=CONFIRMED_AT
    )

    record = confirmation.durable_record()

    assert record == {
        "account": "account-fixture-001",
        "capability": "analysis-run",
        "confirmed_at": CONFIRMED_AT,
        "credit_cost": CARRIED_COST,
        "input_revision": 41,
        "input_selection": "sha256:" + "b" * 64,
        "installation": "installation-fixture-001",
        "job": "job-fixture-001",
        "organization": "organization-fixture-001",
        "parameter_digest": "sha256:" + "a" * 64,
        "permit": "01920000-0000-7000-8000-000000000001",
        "principal": "principal-fixture-001",
        "promised_generation": "generation-fixture-001",
        "purpose": "single-execution-admission-confirmation",
        "request_idempotency_key": "9f1c5a2e-0000-4000-8000-000000000001",
        "session": "session-fixture-001",
        "v": 1,
    }


def test_carried_cost_is_displayed_and_recorded_verbatim() -> None:
    subject = _subject()
    confirmation, _ = issue_admission_confirmation(
        _policy(), subject, issued_at=CONFIRMED_AT
    )

    assert displayed_credit_cost(subject) is subject.credit_cost
    assert confirmation.durable_record()["credit_cost"] is subject.credit_cost


def test_confirmed_cost_admits_the_reservation() -> None:
    policy, token = _confirmed()

    outcome = reserve_admission(
        policy, token, request=_reserve_request(), now=CONFIRMED_AT + 30
    )

    assert (outcome.reserved, outcome.result) == (True, "reserved")


def test_repriced_cost_is_refused_rather_than_reserved() -> None:
    policy, token = _confirmed()

    outcome = reserve_admission(
        policy,
        token,
        request=_reserve_request(permit_credit_cost=REPRICED_COST),
        now=CONFIRMED_AT + 30,
    )

    assert (outcome.reserved, outcome.result) == (False, "confirmation_cost_mismatch")


@pytest.mark.parametrize("presented", [True, 250_000.0, "250000"])
def test_cost_of_another_type_is_not_the_confirmed_cost(presented: object) -> None:
    policy, token = _confirmed(_subject(credit_cost=1))

    outcome = reserve_admission(
        policy,
        token,
        request=_reserve_request(permit_credit_cost=presented),
        now=CONFIRMED_AT + 30,
    )

    assert outcome.result == "confirmation_cost_mismatch"


def test_capability_mismatch_is_refused() -> None:
    policy, token = _confirmed()

    outcome = reserve_admission(
        policy,
        token,
        request=_reserve_request(capability="graph-overlay"),
        now=CONFIRMED_AT + 30,
    )

    assert outcome.result == "confirmation_capability_mismatch"


@pytest.mark.parametrize(
    "override",
    [
        {"job_id": "job-fixture-002"},
        {"permit_id": "01920000-0000-7000-8000-000000000002"},
        {"parameter_digest": "sha256:" + "c" * 64},
        {"input_revision": 42},
        {"input_selection_digest": "sha256:" + "d" * 64},
        {"request_idempotency_key": "9f1c5a2e-0000-4000-8000-000000000002"},
    ],
)
def test_binding_mismatch_is_refused(override: dict[str, object]) -> None:
    policy, token = _confirmed()

    outcome = reserve_admission(
        policy, token, request=_reserve_request(**override), now=CONFIRMED_AT + 30
    )

    assert outcome.result == "confirmation_binding_mismatch"


def test_reserve_without_a_confirmation_is_refused() -> None:
    outcome = reserve_admission(_policy(), "", request=_reserve_request(), now=CONFIRMED_AT)

    assert (outcome.reserved, outcome.result) == (False, "confirmation_invalid")


def test_tampered_confirmation_is_refused() -> None:
    policy, token = _confirmed()
    payload, signature = token.split(".", 1)

    outcome = reserve_admission(
        policy, f"{payload}x.{signature}", request=_reserve_request(), now=CONFIRMED_AT + 30
    )

    assert outcome.result == "confirmation_invalid"


@pytest.mark.parametrize(
    "override",
    [
        {"principal_id": "principal-fixture-002"},
        {"creator_account_id": "account-fixture-002"},
        {"session_id": "session-fixture-002"},
    ],
)
def test_confirmation_of_another_session_is_refused(override: dict[str, str]) -> None:
    _, token = _confirmed()

    outcome = reserve_admission(
        _policy(_identity(**override)),
        token,
        request=_reserve_request(),
        now=CONFIRMED_AT + 30,
    )

    assert outcome.result == "confirmation_invalid"


@pytest.mark.parametrize("instant", [CONFIRMED_AT - 1, CONFIRMED_AT + 8 * 60 * 60 + 1])
def test_confirmation_outside_its_window_is_refused(instant: int) -> None:
    policy, token = _confirmed()

    outcome = reserve_admission(policy, token, request=_reserve_request(), now=instant)

    assert outcome.result == "confirmation_invalid"


def test_confirmation_with_an_unknown_field_is_refused() -> None:
    policy = _policy()
    confirmation, _ = issue_admission_confirmation(
        policy, _subject(), issued_at=CONFIRMED_AT
    )
    forged = sign_local_document(
        {**confirmation.durable_record(), "discount": "applied"}
    )

    with pytest.raises(AdmissionConfirmationError):
        open_admission_confirmation(policy, forged, now=CONFIRMED_AT)


def test_csrf_token_is_not_a_confirmation() -> None:
    policy = _policy()

    with pytest.raises(AdmissionConfirmationError):
        open_admission_confirmation(
            policy, csrf_token(policy, issued_at=CONFIRMED_AT), now=CONFIRMED_AT
        )


def test_confirmation_is_not_a_csrf_token() -> None:
    policy, token = _confirmed()

    with pytest.raises(Exception) as error:
        verify_csrf_document(policy, token, now=CONFIRMED_AT)

    assert "CSRF token" in str(error.value)


def test_confirmation_requires_a_csrf_checked_request() -> None:
    with pytest.raises(HTTPException) as error:
        confirm_admission(_http_request(), None, _subject())

    assert error.value.status_code == 403


def test_confirmation_requires_a_bridge_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")
    identity = _identity()
    policy = _policy(identity)
    request = _http_request(
        host="example.invalid",
        origin="http://example.invalid",
        session_cookie=issue_local_session_token(identity),
    )

    with pytest.raises(HTTPException) as error:
        confirm_admission(request, csrf_token(policy), _subject())

    assert (error.value.status_code, error.value.detail) == (
        403,
        "Request origin is not authorized",
    )


def test_confirmation_requires_an_authenticated_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")

    with pytest.raises(HTTPException) as error:
        confirm_admission(_http_request(), None, _subject())

    assert error.value.status_code == 401


def test_bridge_originated_csrf_checked_request_mints_a_usable_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")
    identity = _identity()
    request = _http_request(session_cookie=issue_local_session_token(identity))

    confirmation, token = confirm_admission(
        request, csrf_token(_policy(identity)), _subject()
    )

    outcome = reserve_admission(
        _policy(identity),
        token,
        request=_reserve_request(),
        now=confirmation.confirmed_at,
    )
    assert (outcome.reserved, outcome.result) == (True, "reserved")
    assert outcome.confirmation is not None
    assert outcome.confirmation.durable_record() == confirmation.durable_record()


def test_confirmed_reserve_still_obeys_the_admission_decision() -> None:
    policy, token = _confirmed()

    outcome = reserve_admission(
        policy,
        token,
        request=_reserve_request(permit_state="spent"),
        now=CONFIRMED_AT + 30,
    )

    assert (outcome.reserved, outcome.result) == (False, "permit_spent")
