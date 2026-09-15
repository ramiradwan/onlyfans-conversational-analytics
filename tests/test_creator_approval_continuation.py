from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.provisioning.app import create_provisioning_app
from app.provisioning.binding_acquisition import acquire_creator_account_binding
from app.provisioning.session import PROVISIONING_ORIGIN, PROVISIONING_SESSION_COOKIE_NAME
from app.security.hosted_grants import GrantVerificationRefused


HANDOFF_TOKEN = "t" * 32
EXTENSION_ID = "lfiompogjmmgnbkacdnikbfoihmlloda"
HOSTED_URL = "https://secure-setup.example.com/start"


class Claim:
    def __call__(self, *, package: str) -> str | None:
        del package
        return None


class Association:
    def __call__(self, **arguments):
        del arguments
        return "hosted_unavailable"


class Binding:
    def __call__(self):
        return "binding_acquisition_unavailable"


class Finalize:
    def __call__(self, **arguments):
        del arguments
        return "membership_refresh_unavailable"


def _application(hosted_onboarding_url: str = ""):
    return create_provisioning_app(
        claim_submission=Claim(),
        creator_association_initiation=Association(),
        creator_binding_acquisition=Binding(),
        completion_ready=lambda: False,
        finalize_action=Finalize(),
        extension_id=EXTENSION_ID,
        hosted_onboarding_url=hosted_onboarding_url,
        launcher_handoff_token=HANDOFF_TOKEN,
    )


def _shell(application) -> str:
    client = TestClient(application, base_url=PROVISIONING_ORIGIN)
    handoff = client.post(
        "/api/v1/provisioning/handoff",
        headers={"Authorization": "Provisioning " + HANDOFF_TOKEN},
    )
    code = handoff.json()["handoff_code"]
    redeemed = client.get(f"/provisioning/handoff?code={code}", follow_redirects=False)
    cookie = {
        "Cookie": (
            f"{PROVISIONING_SESSION_COOKIE_NAME}="
            f"{redeemed.cookies[PROVISIONING_SESSION_COOKIE_NAME]}"
        )
    }
    response = client.get("/provisioning", headers=cookie)
    assert response.status_code == 200
    return response.text


def test_creator_approval_continuation_uses_only_the_release_owned_hosted_entry() -> None:
    page = _shell(_application(HOSTED_URL))
    marker = page.split('id="continue-creator-approval"', 1)[1].split("</a>", 1)[0]

    assert f'href="{HOSTED_URL}"' in marker
    assert 'target="_blank"' in marker
    assert 'rel="noopener noreferrer"' in marker
    assert "hidden" not in marker
    assert "association_request_id" not in marker
    assert "creator_account_id" not in marker
    assert "installation_id" not in marker
    assert "organization_id" not in marker
    assert "token=" not in marker
    assert "Returning here does not complete approval" in page


def test_creator_approval_continuation_fails_safe_when_hosted_entry_is_absent() -> None:
    page = _shell(_application())
    marker = page.split('id="continue-creator-approval"', 1)[1].split("</a>", 1)[0]

    assert 'href=""' in marker
    assert "hidden" in marker
    assert "Creator approval cannot be opened right now." in page
    assert "example.invalid" not in page


class _MismatchStore:
    def __init__(self) -> None:
        self.approved = False
        self.membership = SimpleNamespace(
            grant_type="membership_snapshot",
            installation_id="installation-1",
            reference_id="membership-1",
        )
        self.candidate = SimpleNamespace(
            association_request_id="association-1",
            onboarding_transaction_id="onboarding-1",
            organization_id="organization-1",
            installation_id="installation-1",
            creator_account_id="creator-1",
        )

    def verified_grants(self):
        return (self.membership,)

    def pending_provisioning_candidate(self, installation_id: str):
        assert installation_id == self.candidate.installation_id
        return self.candidate

    def record_verified_grant_and_approve_provisioning_candidate(self, *args, **kwargs):
        del args, kwargs
        self.approved = True
        return True


class _MismatchedBindingClient:
    def acquire_creator_account_binding(self, association, *, membership_reference_id: str):
        assert association.creator_account_id == "creator-1"
        assert association.installation_id == "installation-1"
        assert membership_reference_id == "membership-1"
        raise GrantVerificationRefused("creator_account_mismatch")


def test_creator_account_authority_mismatch_fails_closed_without_local_approval() -> None:
    store = _MismatchStore()

    result = acquire_creator_account_binding(
        store=store,
        client=_MismatchedBindingClient(),
    )

    assert result == "grant_verification_refused"
    assert store.approved is False
