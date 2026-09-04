"""The served risk disclosure is measured against Legal's recorded approval state.

The provisioning surface serves a byte copy of the disclosure, and this
repository holds no evidence that those bytes are the approved instrument:
approval lives in the separate Legal policy manifest, which this repository
neither contains nor may locate at a relative path.

`shared/legal/risk-disclosure.lock.json` therefore pins the manifest's recorded
approval fields for the `risk_disclosure` document and carries no disclosure
bytes and no hash of the served file. The served file is hashed at run time and
compared with the pinned `rendered_sha256`, which yields four outcomes:

* nothing is approved yet, so the comparison has no right-hand side and is
  reported as an expected failure;
* an approved instrument exists and the served bytes hash to it, which passes;
* an approved instrument exists and the served bytes do not, which fails;
* the instrument is retired, meaning superseded or withdrawn, which fails.

A record that is neither consistently unapproved nor consistently approved, and
a status outside the manifest enum, both fail instead of resolving to one of the
four.

A pin read only by the repository that carries it cannot detect upstream change.
Point `OFCA_LEGAL_POLICY_MANIFEST` at a `policy-manifest.json` to refute the pin
and the served copy against their upstream. Without that variable those two
checks skip rather than pass, because an absent source of truth is not agreement
with one.

Detection only: nothing here alters consent behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from app.provisioning.app import _DISCLOSURE_ASSET


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "shared" / "legal" / "risk-disclosure.lock.json"
LOCK_SCHEMA = "ofca-legal-instrument-lock/v1"
UPSTREAM_MANIFEST_VARIABLE = "OFCA_LEGAL_POLICY_MANIFEST"

SHA256 = re.compile(r"^[a-f0-9]{64}$")
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PUBLIC_ROUTE = re.compile(r"^/[a-z0-9_/-]+$")

# Document statuses, matching the policy manifest schema enum.
DRAFT = "draft"
APPROVED = "approved"
SUPERSEDED = "superseded"
WITHDRAWN = "withdrawn"
MANIFEST_STATUSES = frozenset({DRAFT, APPROVED, SUPERSEDED, WITHDRAWN})
RETIRED_STATUSES = frozenset({SUPERSEDED, WITHDRAWN})

# Verdicts. APPROVED doubles as the status that produces it.
UNAPPROVED = "unapproved"
RETIRED = "retired"
INCOHERENT = "incoherent"
UNRECOGNIZED = "unrecognized"
FAILING_VERDICTS = frozenset({RETIRED, INCOHERENT, UNRECOGNIZED})


def read_lock() -> dict[str, Any]:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    assert lock["schema"] == LOCK_SCHEMA
    return lock


def manifest_state(manifest: dict[str, Any], document_key: str) -> dict[str, Any]:
    """Project the approval fields this repository pins out of a policy manifest."""
    document = manifest["documents"][document_key]
    locale = document["canonical_locale"]
    variant = document["variants"][locale]
    return {
        "document_id": document["document_id"],
        "status": document["status"],
        "version": document["version"],
        "effective_at": document["effective_at"],
        "approved_at": document["approved_at"],
        "approved_by": document["approved_by"],
        "public_url": document["public_url"],
        "canonical_locale": locale,
        "variant": {
            "locale": locale,
            "source_path": variant["source_path"],
            "source_sha256": variant["source_sha256"],
            "rendered_sha256": variant["rendered_sha256"],
            "approved_at": variant["approved_at"],
            "approved_by": variant["approved_by"],
        },
    }


def approval_markers(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "approved_at": state["approved_at"],
        "approved_by": state["approved_by"],
        "variant.approved_at": state["variant"]["approved_at"],
        "variant.approved_by": state["variant"]["approved_by"],
        "variant.rendered_sha256": state["variant"]["rendered_sha256"],
    }


def approval(state: dict[str, Any]) -> str:
    """Classify a recorded state against the manifest's document statuses.

    Only `draft` holding no approval markers and `approved` holding all of them
    resolve to a servable judgement. A retired status is its own verdict whatever
    the markers hold, because retiring an instrument does not clear the record of
    who approved it. Every other combination, and every status outside the enum,
    is a failure rather than a quiet fallback.
    """
    status = state["status"]
    markers = approval_markers(state)
    filled = {name for name, value in markers.items() if value is not None}
    if status not in MANIFEST_STATUSES:
        return UNRECOGNIZED
    if status in RETIRED_STATUSES:
        return RETIRED
    if status == APPROVED:
        return APPROVED if len(filled) == len(markers) else INCOHERENT
    return UNAPPROVED if not filled else INCOHERENT


def explain(lock: dict[str, Any], state: dict[str, Any], verdict: str, served: str) -> str:
    """Diagnose a failing verdict in the terms that produced it."""
    origin = f"{lock['source_repository']}:{lock['source_path']}"
    document = lock["document_key"]
    status = state["status"]
    if verdict == UNRECOGNIZED:
        return (
            f"{origin} records {document} with status {status!r}, which is not one of "
            f"{sorted(MANIFEST_STATUSES)}"
        )
    if verdict == RETIRED:
        return (
            f"{origin} records {document} as {status!r}, so the served {served} is a "
            "retired instrument"
        )
    return (
        f"{LOCK_PATH.name} records a partial approval for {document}: "
        f"{approval_markers(state)}"
    )


def served_digest() -> str:
    return hashlib.sha256(_DISCLOSURE_ASSET.read_bytes()).hexdigest()


def upstream_manifest_path() -> Path:
    configured = os.environ.get(UPSTREAM_MANIFEST_VARIABLE)
    if not configured:
        pytest.skip(
            f"{UPSTREAM_MANIFEST_VARIABLE} is unset, so {LOCK_PATH.name} is unrefuted"
        )
    path = Path(configured).resolve()
    assert path.is_file(), f"{UPSTREAM_MANIFEST_VARIABLE} does not name a file: {path}"
    return path


def upstream_root(lock: dict[str, Any], manifest_path: Path) -> Path:
    """Resolve the upstream checkout root from the manifest's own declared path."""
    depth = len(Path(lock["source_path"]).parts)
    assert manifest_path.as_posix().endswith(lock["source_path"])
    return manifest_path.parents[depth - 1]


def test_served_disclosure_is_the_approved_legal_instrument() -> None:
    lock = read_lock()
    state = lock["recorded_state"]
    verdict = approval(state)
    served = served_digest()

    assert verdict not in FAILING_VERDICTS, explain(lock, state, verdict, served)
    if verdict == UNAPPROVED:
        pytest.xfail(
            f"{lock['source_repository']}:{lock['source_path']} records "
            f"{lock['document_key']} as {state['status']!r} with no approved "
            f"rendering, so the served {served} is not an approved instrument"
        )
    assert served == state["variant"]["rendered_sha256"]


def test_recorded_state_is_a_reference_and_not_a_copy_of_the_served_bytes() -> None:
    lock = read_lock()
    state = lock["recorded_state"]
    served = served_digest()
    without_rendering = {
        **lock,
        "recorded_state": {
            **state,
            "variant": {**state["variant"], "rendered_sha256": None},
        },
    }
    rendered = state["variant"]["rendered_sha256"]

    assert ROOT / lock["shipped_asset"] == _DISCLOSURE_ASSET
    assert approval(state) in {APPROVED, UNAPPROVED, RETIRED}
    assert served not in json.dumps(without_rendering)
    assert SEMVER.match(state["version"])
    assert PUBLIC_ROUTE.match(state["public_url"])
    assert rendered is None or SHA256.match(rendered)
    for digest in (rendered, state["variant"]["source_sha256"]):
        assert digest is None or SHA256.match(digest)


def test_recorded_state_matches_the_upstream_policy_manifest() -> None:
    lock = read_lock()
    manifest_path = upstream_manifest_path()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == lock["manifest_schema_version"]
    assert manifest_state(manifest, lock["document_key"]) == lock["recorded_state"]


def test_served_disclosure_matches_the_upstream_source_it_is_taken_from() -> None:
    lock = read_lock()
    manifest_path = upstream_manifest_path()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    state = manifest_state(manifest, lock["document_key"])
    verdict = approval(state)
    # Approval moves which upstream file governs, not whether one does: an
    # approved variant's source_sha256 binds the same served bytes its
    # rendered_sha256 does, so the comparison holds on both sides of approval
    # and only a retired or incoherent record leaves nothing to compare.
    if verdict not in {UNAPPROVED, APPROVED}:
        pytest.skip(
            f"{lock['document_key']} is {state['status']!r} upstream, so no "
            "recorded variant governs the served bytes"
        )
    source = upstream_root(lock, manifest_path) / state["variant"]["source_path"]
    recorded = state["variant"]["source_sha256"]

    assert source.is_file(), f"upstream source is missing: {source}"
    assert served_digest() == hashlib.sha256(source.read_bytes()).hexdigest()
    assert recorded is None or recorded == served_digest()
