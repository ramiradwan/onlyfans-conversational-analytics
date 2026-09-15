from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from app.provisioning.claim_package import (
    CLAIM_PACKAGE_PROFILE_V2,
    decode_claim_package,
)
from app.security.hosted_grants import CLAIM_PROFILE_V2


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PIN = ROOT / "contracts" / "consumer-pin.json"
HOSTED_API_VECTORS = (
    ROOT
    / "contracts"
    / "capability-license-hosted-api-v1"
    / "provisioning-quote-exchange.json"
)
SHARED_CONTRACT_COMMIT = "50c08ee8b3f3dbb1364b875e876a32ab7c641f9a"
HOSTED_ISSUE_CASE = "claim-v2-issue-positive"


def _issue_case() -> dict[str, Any]:
    vectors = cast(
        list[dict[str, Any]],
        json.loads(HOSTED_API_VECTORS.read_text(encoding="utf-8")),
    )
    return next(case for case in vectors if case.get("case_id") == HOSTED_ISSUE_CASE)


def test_product_snapshot_matches_shared_hosted_v2_contract_pin() -> None:
    pin = cast(
        dict[str, Any],
        json.loads(CONTRACT_PIN.read_text(encoding="utf-8")),
    )

    assert pin["source_repository"] == "ramiradwan/creator-platform-contracts"
    assert pin["source_commit"] == SHARED_CONTRACT_COMMIT


def test_brain_production_decoder_accepts_exact_shared_hosted_v2_handoff_bytes() -> None:
    case = _issue_case()
    response = cast(dict[str, Any], case["response"])
    handoff = cast(dict[str, Any], response["handoff_package"])

    assert case["operation_id"] == "issueInstallationClaimV2"
    assert response["profile"] == CLAIM_PROFILE_V2
    assert handoff["profile"] == CLAIM_PACKAGE_PROFILE_V2
    assert handoff["claim_profile"] == CLAIM_PROFILE_V2

    # The control-plane hosted onboarding producer at ce3e6d8ab3e9f7db5e157a503618539704ce71a9
    # pins this same shared-contract case and passes the server-provided encoded
    # value unchanged to QR/clipboard. Decode those exact contract bytes here;
    # do not reconstruct a package in Product compatibility evidence.
    decoded = decode_claim_package(
        cast(str, response["handoff_package_encoded"]),
        production=True,
    )

    assert decoded.durable_state() == {
        "claim_id": handoff["claim_id"],
        "onboarding_transaction_id": handoff["onboarding_transaction_id"],
        "organization_id": handoff["organization_id"],
        "installation_id": handoff["installation_id"],
        "claim_profile": handoff["claim_profile"],
    }

    claim = decoded.release_claim()
    assert claim.claim_id == handoff["claim_id"]
    assert claim.claim_secret == handoff["claim_secret"]
    assert claim.challenge == handoff["challenge"]
    assert claim.consume_path == handoff["consume_path"]
    assert claim.claim_profile == CLAIM_PROFILE_V2
