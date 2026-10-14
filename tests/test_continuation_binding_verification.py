"""Continuation bindings retain normal purpose-isolated signed verification."""

import pytest

from app.security.hosted_grants import GrantVerificationRefused
from test_hosted_grants import (
    bundle, claim, identity, device, _client, _association,
    _ACCOUNT_ID, _SECOND_ACCOUNT_ID, _EXTERNAL_SUBJECT, _tamper_signature,
)


pytestmark = [pytest.mark.ci_tier("fast")]


def test_continuation_uses_existing_key_and_original_membership_without_legacy_hosted_calls(
        tmp_path, bundle, claim, identity, device):
    client, store, transport, key = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    key.reopen_existing = lambda: bundle.installation_key
    key.ensure_ready = lambda: pytest.fail("continuation attempted initial key path")
    before = tuple(transport.requests)
    association = _association(request_id="019a0934-7800-7000-8000-000000000003")
    reference = client.accept_continuation_binding(bundle.creator_bindings[_ACCOUNT_ID], association,
        membership_reference_id=consumed.grant_reference_ids[1])
    assert reference.creator_account_id == _ACCOUNT_ID
    assert reference.subject == _EXTERNAL_SUBJECT
    assert tuple(transport.requests) == before
    assert not any(grant.grant_type == "creator_account_binding" for grant in store.verified_grants())


@pytest.mark.parametrize("alteration", ["signature", "account", "purpose"])
def test_invalid_continuation_binding_never_reaches_persistence(
        tmp_path, bundle, claim, identity, device, alteration):
    client, store, transport, key = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    key.reopen_existing = lambda: bundle.installation_key
    key.ensure_ready = lambda: pytest.fail("continuation attempted initial key path")
    token = {"signature": _tamper_signature(bundle.creator_bindings[_ACCOUNT_ID]),
             "account": bundle.creator_bindings[_SECOND_ACCOUNT_ID],
             "purpose": bundle.tokens["installation_grant"]}[alteration]
    association = _association(request_id="019a0934-7800-7000-8000-000000000003")
    with pytest.raises(GrantVerificationRefused):
        client.accept_continuation_binding(token, association,
            membership_reference_id=consumed.grant_reference_ids[1])
    assert not any(grant.grant_type == "creator_account_binding" for grant in store.verified_grants())
