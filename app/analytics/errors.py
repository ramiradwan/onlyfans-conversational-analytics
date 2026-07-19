"""Sanitized domain failures for the derived analytics plane."""

from __future__ import annotations


class AnalyticsError(RuntimeError):
    """Base error whose public fields never contain canonical values."""

    code = "analytics_error"
    public_message = "Analytics processing failed."

    def __init__(self) -> None:
        super().__init__(self.public_message)


class CanonicalAccountNotFound(AnalyticsError):
    code = "analytics_account_not_found"
    public_message = "The authenticated account has no canonical state."


class ProjectionUnavailable(AnalyticsError):
    code = "analytics_projection_unavailable"
    public_message = "Analytics are not available yet."

    def __init__(
        self,
        *,
        availability: str = "unavailable",
        reason_code: str | None = None,
        building: bool | None = None,
    ) -> None:
        if building is not None:
            availability = "building" if building else "unavailable"
        self.availability = availability
        self.building = availability == "building"
        self.reason_code = reason_code
        self.retryable = True
        if self.building:
            self.code = "analytics_projection_building"
            self.public_message = "Analytics are being prepared."
        elif availability == "error":
            self.code = reason_code or "analytics_projection_error"
            self.public_message = "Analytics could not be prepared."
        super().__init__()


class ProjectionCoordinatorClosed(AnalyticsError):
    code = "analytics_projection_coordinator_closed"
    public_message = "Analytics projection scheduling is closed."


class ProjectionBackpressure(AnalyticsError):
    code = "analytics_projection_backpressure"
    public_message = "Analytics projection capacity is temporarily full."


class ProjectionBuildCancelled(AnalyticsError):
    code = "analytics_projection_cancelled"
    public_message = "Analytics projection work was cancelled."


class ProjectionStorageUnavailable(AnalyticsError):
    code = "analytics_projection_storage_unavailable"
    public_message = "Analytics storage is being recovered."


class InvalidAnalyticsRequest(AnalyticsError):
    code = "analytics_request_invalid"
    public_message = "The analytics request is invalid."

    def __init__(self, code: str, public_message: str) -> None:
        self.code = code
        self.public_message = public_message
        super().__init__()


class CanonicalStateInvalid(AnalyticsError):
    code = "canonical_state_invalid"
    public_message = "Canonical state is not valid for analytics."


class AnalyzerConfigurationInvalid(AnalyticsError):
    code = "analyzer_configuration_invalid"
    public_message = "An analytics adapter has invalid provenance."


class CanonicalRevisionChanged(AnalyticsError):
    code = "canonical_revision_changed"
    public_message = "Canonical state changed while analytics were being prepared."
