"""Stable identities that bind canonical snapshots to derived generations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.models.analytics import AnalyticsProjection
from app.transport.ingestion import AccountReadModel


@dataclass(frozen=True, slots=True)
class CanonicalIdentity:
    """Revision plus a digest of the exact canonical account snapshot."""

    revision: int
    content_digest: str


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"unsupported canonical identity value: {type(value).__name__}")


def _digest(domain: bytes, value: Any) -> str:
    encoded = json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(domain + b"\0" + encoded).hexdigest()


def canonical_identity(account: AccountReadModel) -> CanonicalIdentity:
    """Identify one immutable account snapshot without retaining its content."""

    if account.view_revision < 0:
        raise ValueError("canonical revision must be non-negative")
    return CanonicalIdentity(
        revision=account.view_revision,
        content_digest=_digest(
            b"ofca:canonical-account:v1",
            {
                "view_revision": account.view_revision,
                "conversations": account.conversations,
            },
        ),
    )


def pipeline_identity_digest(projection: AnalyticsProjection) -> str:
    """Bind every analyzer and pipeline/config identity used by a generation."""

    analyzers = sorted(
        (
            item.analyzer_name,
            item.revision,
            item.config_digest,
            item.mode.value,
            item.calibration_status.value,
        )
        for item in projection.analyzers
    )
    return _digest(
        b"ofca:analytics-pipeline-identity:v1",
        {
            "schema_version": projection.schema_version,
            "pipeline_revision": projection.pipeline_revision,
            "pipeline_config_digest": projection.pipeline_config_digest,
            "analyzers": analyzers,
        },
    )
