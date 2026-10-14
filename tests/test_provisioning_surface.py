"""The first-stage ASGI app has no runtime routes or schemas."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest


pytestmark = [pytest.mark.ci_tier('fast')]


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
ROOT = Path(__file__).resolve().parents[1]
DISCLOSURE_ASSET = (
    ROOT / "app" / "provisioning" / "creator-platform-data-risk-disclosure.html"
)
DISCLOSURE_INSTRUMENT = "risk_disclosure"
# The rendering the Agent binds acknowledgment to. Taking the expected digest
# from the bindings rather than repeating it here means a disclosure edited
# without its binding fails, which is the pairing the release itself requires.
LEGAL_BINDINGS = (
    ROOT
    / "extension"
    / "tests"
    / "fixtures"
    / "legal-instrument-bindings.synthetic.json"
)


def bound_disclosure_sha256() -> str:
    bindings = json.loads(LEGAL_BINDINGS.read_text(encoding="utf-8"))
    return bindings["instruments"][DISCLOSURE_INSTRUMENT]["rendered_sha256"]


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
