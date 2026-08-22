"""The first-stage ASGI app has no runtime routes or schemas."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute

from app.provisioning.app import (
    PROVISIONING_CLAIM_PATH,
    PROVISIONING_CREATOR_BINDING_ACQUISITION_PATH,
    PROVISIONING_CREATOR_ASSOCIATION_PATH,
    PROVISIONING_FINALIZE_PATH,
    PROVISIONING_SCRIPT_PATH,
    PROVISIONING_STATUS_PATH,
    create_provisioning_app,
)
from app.provisioning.session import (
    PROVISIONING_CSRF_HEADER,
    PROVISIONING_ORIGIN,
    PROVISIONING_SESSION_COOKIE_NAME,
)


HANDOFF_TOKEN = "t" * 32
EXTENSION_ID = "lfiompogjmmgnbkacdnikbfoihmlloda"
ASSOCIATION_REQUEST_ID = "association-1"
ACCOUNT_ID = "creator-account-1"
REPORTED_PLATFORM_ID = "platform-creator-9999"
PACKAGE = "cGFzdGVkLWNsYWltLXBhY2thZ2UtZml4dHVyZQ"
BODY = {
    "association_request_id": ASSOCIATION_REQUEST_ID,
    "detected_creator_account_id": ACCOUNT_ID,
    "reported_platform_creator_id": REPORTED_PLATFORM_ID,
}


class ProvisioningMarkup(HTMLParser):
    """Small structural probe for the served, dependency-free document."""

    def __init__(self) -> None:
        super().__init__()
        self.elements_by_id: dict[str, dict[str, str | None]] = {}
        self.labels_for: set[str] = set()
        self.step_order: list[str] = []
        self.styles: list[str] = []
        self.scripts: list[dict[str, str | None]] = []
        self.external_asset_tags: list[str] = []
        self._style_parts: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if identifier := attributes.get("id"):
            self.elements_by_id[identifier] = attributes
        if tag == "label" and (target := attributes.get("for")):
            self.labels_for.add(target)
        if step := attributes.get("data-step"):
            self.step_order.append(step)
        if tag == "style":
            self._style_parts = []
        if tag == "script":
            self.scripts.append(attributes)
        if tag in {"link", "img", "picture", "source", "video", "audio", "iframe", "object"}:
            self.external_asset_tags.append(tag)

    def handle_data(self, data: str) -> None:
        if self._style_parts is not None:
            self._style_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style" and self._style_parts is not None:
            self.styles.append("".join(self._style_parts))
            self._style_parts = None


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


class RecordingCreatorAssociationInitiation:
    """Association seam standing in for the durable hosted action."""

    def __init__(self, refusal: str | None = None) -> None:
        self.refusal = refusal
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **arguments: Any) -> object:
        self.calls.append(arguments)
        if self.refusal is not None:
            return self.refusal
        return type(
            "AssociationStatus",
            (),
            {
                "association_request_id": "0198a1b2-c3d4-7000-8000-000000000001",
                "status": "pending",
                "updated_at": "2026-08-20T00:00:00Z",
            },
        )()


class RecordingCreatorBindingAcquisition:
    def __init__(self, refusal: str | None = None) -> None:
        self.refusal = refusal
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        if self.refusal is not None:
            return self.refusal
        return type(
            "BindingAcquisitionStatus",
            (),
            {
                "association_request_id": ASSOCIATION_REQUEST_ID,
                "status": "approved",
            },
        )()


def provisioning_app(
    *,
    claim_submission: Any = None,
    creator_association_initiation: Any = None,
    creator_binding_acquisition: Any = None,
    completion_ready: Any = lambda: False,
    finalize_action: Any = None,
    completion_exit: Any = None,
    extension_id: str | None = EXTENSION_ID,
):
    return create_provisioning_app(
        claim_submission=claim_submission or RecordingClaimSubmission(),
        creator_association_initiation=(
            creator_association_initiation or RecordingCreatorAssociationInitiation()
        ),
        creator_binding_acquisition=(
            creator_binding_acquisition or RecordingCreatorBindingAcquisition()
        ),
        completion_ready=completion_ready,
        finalize_action=finalize_action or RecordingFinalizeAction(),
        extension_id=extension_id,
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
        ("/provisioning/provisioning.js", ("GET",)),
        ("/api/v1/provisioning/status", ("GET",)),
        ("/api/v1/provisioning/claim", ("POST",)),
        ("/api/v1/provisioning/creator-association", ("POST",)),
        ("/api/v1/provisioning/creator-association/acquire", ("POST",)),
        ("/api/v1/provisioning/finalize", ("POST",)),
        ("/api/v1/provisioning/retry", ("POST",)),
    }
    assert not any(isinstance(route, WebSocketRoute) for route in application.routes)


def test_shell_serves_module_relative_provisioning_document_and_script() -> None:
    application = provisioning_app()
    client, cookie, _ = bounded_session(application)

    shell = client.get("/provisioning", headers=cookie)
    script = client.get(PROVISIONING_SCRIPT_PATH, headers=cookie)

    assert shell.status_code == 200
    assert f'data-provisioning-extension-id="{EXTENSION_ID}"' in shell.text
    assert 'src="/provisioning/provisioning.js"' in shell.text
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("application/javascript")
    assert "createProvisioningController" in script.text


def test_served_shell_has_accessible_step_structure_and_inline_adaptive_theme() -> None:
    application = provisioning_app()
    client, cookie, _ = bounded_session(application)

    shell = client.get("/provisioning", headers=cookie)
    markup = ProvisioningMarkup()
    markup.feed(shell.text)

    assert shell.status_code == 200
    assert markup.step_order == ["registration", "identity", "approval", "finalization"]
    assert [markup.elements_by_id[f"{name}-step"]["data-state"] for name in (
        "claim", "identity", "binding", "finalize"
    )] == ["current", "locked", "locked", "locked"]
    assert markup.elements_by_id["claim-step"]["aria-current"] == "step"
    assert all(
        "aria-current" not in markup.elements_by_id[f"{name}-step"]
        for name in ("identity", "binding", "finalize")
    )
    assert "claim-package" in markup.labels_for
    assert set(
        markup.elements_by_id["claim-package"]["aria-describedby"].split()
    ) == {
        "claim-package-help",
        "claim-package-validation",
        "claim-package-count",
        "claim-action-help",
    }
    for control_id in (
        "claim-submit",
        "refresh-identity",
        "confirm-identity",
        "acquire-association",
        "finalize-provisioning",
    ):
        description_ids = markup.elements_by_id[control_id]["aria-describedby"].split()
        assert description_ids
        assert all(description_id in markup.elements_by_id for description_id in description_ids)
    assert all(
        f"{name}-step-state" in markup.elements_by_id
        for name in ("claim", "identity", "binding", "finalize")
    )

    assert len(markup.styles) == 1
    style = markup.styles[0]
    assert all(
        feature in style
        for feature in (
            "--color-primary:",
            "--space-1:",
            "--radius-small:",
            ":focus-visible",
            "prefers-color-scheme: dark",
            "prefers-reduced-motion: reduce",
            "forced-colors: active",
        )
    )
    assert "@import" not in style
    assert "@font-face" not in style
    assert "url(" not in style
    assert markup.external_asset_tags == []
    assert markup.scripts == [
        {"type": "module", "src": "/provisioning/provisioning.js"}
    ]


@pytest.mark.parametrize("extension_id", [None, "wrong", "q" * 32])
def test_shell_with_invalid_configured_extension_id_exposes_no_extension_target(
    extension_id: str | None,
) -> None:
    application = provisioning_app(extension_id=extension_id)
    client, cookie, _ = bounded_session(application)

    shell = client.get("/provisioning", headers=cookie)

    assert 'data-provisioning-extension-id=""' in shell.text


def test_provisioning_shell_refuses_a_wrong_host() -> None:
    application = provisioning_app()
    client, cookie, _ = bounded_session(application)

    response = client.get("/provisioning", headers={**cookie, "Host": "wrong.localhost"})

    assert response.status_code == 421


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


def test_creator_association_hands_only_the_detected_account_to_its_action() -> None:
    action = RecordingCreatorAssociationInitiation()
    application = provisioning_app(creator_association_initiation=action)
    client, cookie, token = bounded_session(application)

    response = client.post(
        PROVISIONING_CREATOR_ASSOCIATION_PATH,
        json={"detected_creator_account_id": ACCOUNT_ID},
        headers={**cookie, "Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: token},
    )

    assert response.status_code == 200
    assert response.json() == {
        "association_request_id": "0198a1b2-c3d4-7000-8000-000000000001",
        "status": "pending",
        "updated_at": "2026-08-20T00:00:00Z",
    }
    assert action.calls == [
        {
            "detected_creator_account_id": ACCOUNT_ID,
            "onboarding_transaction_id": None,
            "organization_id": None,
            "installation_id": None,
        }
    ]


def test_a_refused_creator_association_reports_its_nonsecret_reason() -> None:
    action = RecordingCreatorAssociationInitiation(refusal="account_already_bound")
    application = provisioning_app(creator_association_initiation=action)
    client, cookie, token = bounded_session(application)

    response = client.post(
        PROVISIONING_CREATOR_ASSOCIATION_PATH,
        json={"detected_creator_account_id": ACCOUNT_ID},
        headers={**cookie, "Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: token},
    )

    assert response.status_code == 409
    assert response.json() == {
        "state": "provisioning_ready",
        "reason": "account_already_bound",
    }


def test_creator_binding_acquisition_has_no_caller_supplied_coordinates() -> None:
    action = RecordingCreatorBindingAcquisition()
    application = provisioning_app(creator_binding_acquisition=action)
    client, cookie, token = bounded_session(application)

    response = client.post(
        PROVISIONING_CREATOR_BINDING_ACQUISITION_PATH,
        json={},
        headers={**cookie, "Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: token},
    )

    assert response.status_code == 200
    assert response.json() == {
        "association_request_id": ASSOCIATION_REQUEST_ID,
        "status": "approved",
    }
    assert action.calls == 1


def test_a_refused_creator_binding_acquisition_reports_its_nonsecret_reason() -> None:
    action = RecordingCreatorBindingAcquisition(refusal="approval_pending")
    application = provisioning_app(creator_binding_acquisition=action)
    client, cookie, token = bounded_session(application)

    response = client.post(
        PROVISIONING_CREATOR_BINDING_ACQUISITION_PATH,
        json={},
        headers={**cookie, "Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: token},
    )

    assert response.status_code == 409
    assert response.json() == {
        "state": "provisioning_ready",
        "reason": "approval_pending",
    }


def test_creator_binding_acquisition_rejects_caller_coordinates() -> None:
    action = RecordingCreatorBindingAcquisition()
    application = provisioning_app(creator_binding_acquisition=action)
    client, cookie, token = bounded_session(application)

    response = client.post(
        PROVISIONING_CREATOR_BINDING_ACQUISITION_PATH,
        json={"installation_id": "attacker-installation"},
        headers={**cookie, "Origin": PROVISIONING_ORIGIN, PROVISIONING_CSRF_HEADER: token},
    )

    assert response.status_code == 422
    assert action.calls == 0


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
