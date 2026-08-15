"""The first-stage ASGI app has no runtime routes or schemas."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute

from app.provisioning.app import (
    PROVISIONING_CLAIM_PATH,
    PROVISIONING_FINALIZE_PATH,
    PROVISIONING_STATUS_PATH,
    create_provisioning_app,
)
from app.provisioning.session import (
    PROVISIONING_CSRF_HEADER,
    PROVISIONING_ORIGIN,
    PROVISIONING_SESSION_COOKIE_NAME,
)


HANDOFF_TOKEN = "t" * 32
ASSOCIATION_REQUEST_ID = "association-1"
ACCOUNT_ID = "creator-account-1"
REPORTED_PLATFORM_ID = "platform-creator-9999"
PACKAGE = "cGFzdGVkLWNsYWltLXBhY2thZ2UtZml4dHVyZQ"
BODY = {
    "association_request_id": ASSOCIATION_REQUEST_ID,
    "detected_creator_account_id": ACCOUNT_ID,
    "reported_platform_creator_id": REPORTED_PLATFORM_ID,
}


class RecordingFinalizeAction:
    """Finalization seam standing in for the durable completion module."""

    def __init__(self, refusal: str | None = None) -> None:
        self.refusal = refusal
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **arguments: Any) -> str | None:
        self.calls.append(arguments)
        return self.refusal


class RecordingClaimSubmission:
    """Claim seam standing in for the durable claim-submission module."""

    def __init__(self, refusal: str | None = None) -> None:
        self.refusal = refusal
        self.packages: list[str] = []

    def __call__(self, *, package: str) -> str | None:
        self.packages.append(package)
        return self.refusal


def provisioning_app(
    *,
    claim_submission: Any = None,
    completion_ready: Any = lambda: False,
    finalize_action: Any = None,
    completion_exit: Any = None,
):
    return create_provisioning_app(
        claim_submission=claim_submission or RecordingClaimSubmission(),
        completion_ready=completion_ready,
        finalize_action=finalize_action or RecordingFinalizeAction(),
        launcher_handoff_token=HANDOFF_TOKEN,
        completion_exit=completion_exit,
    )


def bounded_session(application) -> tuple[TestClient, dict[str, str], str]:
    """Redeem one launcher handoff and read the session-bound CSRF token."""

    client = TestClient(application, base_url=PROVISIONING_ORIGIN)
    handoff = client.post(
        "/api/v1/provisioning/handoff",
        headers={"Authorization": "Provisioning " + HANDOFF_TOKEN},
    )
    redeemed = client.get(
        f"/provisioning/handoff?code={handoff.json()['handoff_code']}",
        follow_redirects=False,
    )
    cookie = {
        "Cookie": (
            f"{PROVISIONING_SESSION_COOKIE_NAME}="
            f"{redeemed.cookies[PROVISIONING_SESSION_COOKIE_NAME]}"
        )
    }
    shell = client.get("/provisioning", headers=cookie)
    token = shell.text.split('data-provisioning-csrf="')[1].split('"')[0]
    return client, cookie, token


def test_provisioning_app_exposes_no_runtime_route() -> None:
    application = provisioning_app()

    assert application.openapi_url is None
    assert {(route.path, tuple(sorted(route.methods or ()))) for route in application.routes} == {
        ("/health", ("GET",)),
        ("/api/v1/provisioning/handoff", ("POST",)),
        ("/provisioning/handoff", ("GET",)),
        ("/provisioning", ("GET",)),
        ("/api/v1/provisioning/status", ("GET",)),
        ("/api/v1/provisioning/claim", ("POST",)),
        ("/api/v1/provisioning/finalize", ("POST",)),
        ("/api/v1/provisioning/retry", ("POST",)),
    }
    assert not any(isinstance(route, WebSocketRoute) for route in application.routes)


def test_ready_provisioning_requests_distinguished_restart() -> None:
    exits: list[str] = []
    application = provisioning_app(
        completion_ready=lambda: True,
        completion_exit=lambda: exits.append("restart"),
    )
    client, cookie, _ = bounded_session(application)

    status = client.get(PROVISIONING_STATUS_PATH, headers=cookie)

    assert status.json() == {"state": "configured_restart"}
    assert application.state.completion_requested is True
    assert exits == ["restart"]


def test_finalization_hands_the_page_body_to_the_finalize_action() -> None:
    action = RecordingFinalizeAction()
    application = provisioning_app(finalize_action=action)
    client, cookie, token = bounded_session(application)

    response = client.post(
        PROVISIONING_FINALIZE_PATH,
        json=BODY,
        headers={**cookie, "Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: token},
    )

    assert response.status_code == 200
    assert response.json() == {"state": "configured_restart"}
    assert action.calls == [
        {
            "association_request_id": ASSOCIATION_REQUEST_ID,
            "detected_creator_account_id": ACCOUNT_ID,
            "reported_platform_creator_id": REPORTED_PLATFORM_ID,
        }
    ]


def test_a_refused_finalization_reports_its_nonsecret_reason() -> None:
    action = RecordingFinalizeAction(refusal="incomplete_grant_set")
    application = provisioning_app(finalize_action=action)
    client, cookie, token = bounded_session(application)

    response = client.post(
        PROVISIONING_FINALIZE_PATH,
        json=BODY,
        headers={**cookie, "Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: token},
    )

    assert response.status_code == 409
    assert response.json() == {
        "state": "provisioning_ready",
        "reason": "incomplete_grant_set",
    }
    assert len(action.calls) == 1


def test_finalization_without_the_session_csrf_token_never_reaches_the_action() -> None:
    """The exact body a bounded session would send is refused without its token."""

    action = RecordingFinalizeAction()
    application = provisioning_app(finalize_action=action)
    client, cookie, _ = bounded_session(application)

    response = client.post(
        PROVISIONING_FINALIZE_PATH,
        json=BODY,
        headers={**cookie, "Origin": PROVISIONING_ORIGIN},
    )

    assert response.status_code == 403
    assert action.calls == []


def test_finalization_from_another_origin_never_reaches_the_action() -> None:
    action = RecordingFinalizeAction()
    application = provisioning_app(finalize_action=action)
    client, cookie, token = bounded_session(application)

    response = client.post(
        PROVISIONING_FINALIZE_PATH,
        json=BODY,
        headers={
            **cookie,
            "Origin": "http://attacker.localhost:17871",
            PROVISIONING_CSRF_HEADER: token,
        },
    )

    assert response.status_code == 403
    assert action.calls == []


def test_finalization_outside_a_bounded_session_never_reaches_the_action() -> None:
    action = RecordingFinalizeAction()
    application = provisioning_app(finalize_action=action)
    client = TestClient(application, base_url=PROVISIONING_ORIGIN)

    response = client.post(
        PROVISIONING_FINALIZE_PATH,
        json=BODY,
        headers={"Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: "guessed"},
    )

    assert response.status_code == 401
    assert action.calls == []


def test_a_pasted_package_reaches_the_claim_submission_action() -> None:
    submission = RecordingClaimSubmission()
    application = provisioning_app(claim_submission=submission)
    client, cookie, token = bounded_session(application)

    response = client.post(
        PROVISIONING_CLAIM_PATH,
        json={"package": PACKAGE},
        headers={**cookie, "Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: token},
    )

    assert response.status_code == 200
    assert response.json() == {"state": "installation_registered"}
    assert submission.packages == [PACKAGE]


@pytest.mark.parametrize(
    "pasted",
    [
        f"  {PACKAGE}  ",
        f"\n{PACKAGE}\n",
        f"\t{PACKAGE}\r\n",
        f" {PACKAGE} ",
    ],
)
def test_surrounding_whitespace_is_stripped_before_the_action_sees_it(
    pasted: str,
) -> None:
    """A pasted field carries transport whitespace; the package is unchanged."""

    submission = RecordingClaimSubmission()
    application = provisioning_app(claim_submission=submission)
    client, cookie, token = bounded_session(application)

    response = client.post(
        PROVISIONING_CLAIM_PATH,
        json={"package": pasted},
        headers={**cookie, "Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: token},
    )

    assert response.status_code == 200
    assert submission.packages == [PACKAGE]


def test_whitespace_inside_a_package_is_not_removed() -> None:
    """Only the ends are transport. An interior byte is the package's problem."""

    submission = RecordingClaimSubmission(refusal="encoding")
    application = provisioning_app(claim_submission=submission)
    client, cookie, token = bounded_session(application)

    response = client.post(
        PROVISIONING_CLAIM_PATH,
        json={"package": f" {PACKAGE[:4]} {PACKAGE[4:]} "},
        headers={**cookie, "Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: token},
    )

    assert response.status_code == 409
    assert submission.packages == [f"{PACKAGE[:4]} {PACKAGE[4:]}"]


def test_a_refused_claim_reports_its_nonsecret_reason() -> None:
    submission = RecordingClaimSubmission(refusal="claim_already_consumed")
    application = provisioning_app(claim_submission=submission)
    client, cookie, token = bounded_session(application)

    response = client.post(
        PROVISIONING_CLAIM_PATH,
        json={"package": PACKAGE},
        headers={**cookie, "Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: token},
    )

    assert response.status_code == 409
    assert response.json() == {
        "state": "provisioning_ready",
        "reason": "claim_already_consumed",
    }


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://attacker.localhost:17871", PROVISIONING_CSRF_HEADER: "token"},
        {"Origin": PROVISIONING_ORIGIN},
    ],
    ids=["no-origin-or-token", "foreign-origin", "no-csrf-token"],
)
def test_claim_submission_without_a_bound_mutation_never_reaches_the_action(
    headers: dict[str, str],
) -> None:
    """The byte-identical body a bounded session sends is refused without its token."""

    submission = RecordingClaimSubmission()
    application = provisioning_app(claim_submission=submission)
    client, cookie, _ = bounded_session(application)

    response = client.post(
        PROVISIONING_CLAIM_PATH, json={"package": PACKAGE}, headers={**cookie, **headers}
    )

    assert response.status_code == 403
    assert submission.packages == []


def test_claim_submission_outside_a_bounded_session_never_reaches_the_action() -> None:
    submission = RecordingClaimSubmission()
    application = provisioning_app(claim_submission=submission)
    client = TestClient(application, base_url=PROVISIONING_ORIGIN)

    response = client.post(
        PROVISIONING_CLAIM_PATH,
        json={"package": PACKAGE},
        headers={"Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: "guessed"},
    )

    assert response.status_code == 401
    assert submission.packages == []


@pytest.mark.parametrize(
    "body",
    [
        {"package": ""},
        {"package": "a" * 2049},
        {"package": PACKAGE, "claim_secret": "s" * 43},
        {},
        {"package": 17},
    ],
    ids=["empty", "oversized", "extra-field", "absent", "not-text"],
)
def test_an_unbounded_claim_body_never_reaches_the_action(body: dict[str, Any]) -> None:
    submission = RecordingClaimSubmission()
    application = provisioning_app(claim_submission=submission)
    client, cookie, token = bounded_session(application)

    response = client.post(
        PROVISIONING_CLAIM_PATH,
        json=body,
        headers={**cookie, "Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: token},
    )

    assert response.status_code == 422
    assert submission.packages == []


def test_a_status_poll_never_finalizes() -> None:
    action = RecordingFinalizeAction()
    application = provisioning_app(finalize_action=action)
    client, cookie, _ = bounded_session(application)

    for _ in range(3):
        assert client.get(PROVISIONING_STATUS_PATH, headers=cookie).json() == {
            "state": "provisioning_ready"
        }

    assert action.calls == []
