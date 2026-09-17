from __future__ import annotations

from fastapi.testclient import TestClient

from app.provisioning.app import create_provisioning_app
from app.provisioning.session import PROVISIONING_ORIGIN, PROVISIONING_SESSION_COOKIE_NAME
from provisioning_markup import PageMarkup


HANDOFF_TOKEN = "t" * 32
EXTENSION_ID = "lfiompogjmmgnbkacdnikbfoihmlloda"
ONBOARDING_URL = "https://setup.example.com/start"


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


def app(hosted_onboarding_url: str = ""):
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


def shell(application) -> str:
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


def test_configured_first_run_has_one_authoritative_secure_setup_entry() -> None:
    page = shell(app(ONBOARDING_URL))
    markup = PageMarkup(page)
    entry = markup.elements["open-secure-setup"]
    assert entry.tag == "a"
    assert entry.attributes["href"] == ONBOARDING_URL
    assert entry.attributes["target"] == "_blank"
    assert entry.attributes["rel"] == "noopener noreferrer"
    assert entry.text
    assert not entry.hidden
    assert markup.has_visible_guidance("open-secure-setup")
    assert markup.is_visible_step_text("claim-step-description", "claim-step")
    assert "LOCAL_PROVISIONING_HOSTED_ORIGIN" not in page
    assert "installation_id" not in page
    assert "organization_id" not in page


def test_unconfigured_development_surface_does_not_offer_a_fake_hosted_entry() -> None:
    page = shell(app())
    marker = page.split('id="open-secure-setup"', 1)[1].split("</a>", 1)[0]
    assert 'href=""' in marker
    assert "hidden" in marker
    assert "example.invalid" not in page
