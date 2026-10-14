from __future__ import annotations

from types import SimpleNamespace


from app.provisioning.binding_acquisition import acquire_creator_account_binding
from app.security.hosted_grants import GrantVerificationRefused

import pytest

pytestmark = [pytest.mark.ci_tier('fast')]


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


class _MismatchStore:
    def __init__(self) -> None:
        self.approved = False
        self.membership = SimpleNamespace(
            grant_type="membership_snapshot",
            installation_id="installation-1",
            reference_id="membership-1",
        )
        self.candidate = SimpleNamespace(
            association_request_id="0198a1b2-c3d4-7800-8000-000000000011",
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
