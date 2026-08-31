"""Source-time provenance for bounded historical analytics derivations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.models.analytics import AnalyticsProjection


PARTICIPANT_ANALYTICS_MAX_DAYS = 90
HISTORICAL_DERIVATION_SCHEMA = "historical-derivation.v1"
RETENTION_BASIS = "canonical_message_sent_at"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("historical_derivation_time_timezone_required")
    return value.astimezone(timezone.utc)


class HistoricalDerivationProvenance(BaseModel):
    """Immutable processing-time evidence separated from source-time authority."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_id: Literal["historical-derivation.v1"] = Field(
        default=HISTORICAL_DERIVATION_SCHEMA,
        validation_alias="schema",
        serialization_alias="schema",
    )
    retention_basis: Literal["canonical_message_sent_at"] = RETENTION_BASIS
    derived_at: AwareDatetime
    source_time_start: AwareDatetime | None = None
    source_time_end: AwareDatetime | None = None
    retention_due_at: AwareDatetime | None = None
    source_message_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_source_time_authority(self) -> "HistoricalDerivationProvenance":
        self.derived_at = _utc(self.derived_at)
        if self.source_message_count == 0:
            if any(
                value is not None
                for value in (
                    self.source_time_start,
                    self.source_time_end,
                    self.retention_due_at,
                )
            ):
                raise ValueError("historical_derivation_empty_window_invalid")
            return self
        if (
            self.source_time_start is None
            or self.source_time_end is None
            or self.retention_due_at is None
        ):
            raise ValueError("historical_derivation_source_window_missing")
        self.source_time_start = _utc(self.source_time_start)
        self.source_time_end = _utc(self.source_time_end)
        self.retention_due_at = _utc(self.retention_due_at)
        if self.source_time_start > self.source_time_end:
            raise ValueError("historical_derivation_source_window_invalid")
        expected_due = self.source_time_start + timedelta(
            days=PARTICIPANT_ANALYTICS_MAX_DAYS
        )
        if self.retention_due_at != expected_due:
            raise ValueError("historical_derivation_retention_due_invalid")
        return self


def historical_retention_cutoff(derived_at: datetime) -> datetime:
    """Return the exclusive source-time lower bound for one derivation."""

    return _utc(derived_at) - timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)


def source_time_is_authorized(source_at: datetime, *, cutoff: datetime) -> bool:
    """Admit source material only while its original event time remains in scope."""

    return _utc(source_at) > _utc(cutoff)


def provenance_for_projection(
    projection: AnalyticsProjection,
    *,
    derived_at: datetime,
) -> HistoricalDerivationProvenance:
    """Describe a generation without letting processing time reset source retention."""

    source_times = sorted(_utc(item.sent_at) for item in projection.message_enrichments)
    if not source_times:
        return HistoricalDerivationProvenance(
            derived_at=_utc(derived_at),
            source_message_count=0,
        )
    source_time_start = source_times[0]
    return HistoricalDerivationProvenance(
        derived_at=_utc(derived_at),
        source_time_start=source_time_start,
        source_time_end=source_times[-1],
        retention_due_at=source_time_start
        + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS),
        source_message_count=len(source_times),
    )
