"""Resolve short-lived analytics references through live canonical reads."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
import time
from typing import Callable

from app.analytics.cancellation import CancellationCheck
from app.analytics.errors import AnalyticsError
from app.analytics.evidence_contracts import (
    EvidenceContextItem, EvidenceLocation, EvidenceMessage, EvidenceSource,
    ResolvedEvidence, ResolvedEvidenceContext,
)
from app.analytics.historical_derivation import (
    PARTICIPANT_ANALYTICS_MAX_DAYS, historical_retention_cutoff,
)
from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref
from app.analytics.query_contracts import QuestionEvidence, SourceSpan, utc_instant
from app.analytics.query_cursor import digest
from app.analytics.query_execution import QuestionBudget, QuestionCancelled, QuestionLimitExceeded, QuestionLimits
from app.security.runtime_policy import RuntimePolicy, authorized_account


class EvidenceUnavailable(AnalyticsError):
    code = "analytics_evidence_unavailable"
    public_message = "This source is unavailable. Run the question again."


@dataclass(frozen=True, slots=True, repr=False)
class _Locator:
    account: str
    location: EvidenceLocation
    created_at: datetime
    expires_at: datetime
    deadline: float
    source_state_token: str | None


@contextmanager
def _safe_errors():
    try:
        yield
    except (EvidenceUnavailable, QuestionCancelled, QuestionLimitExceeded):
        raise
    except Exception:
        raise EvidenceUnavailable() from None


class EvidenceResolver:
    def __init__(
        self, source: EvidenceSource, *, max_entries: int = 4096,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        request_refresh: Callable[[str], None] | None = None,
    ) -> None:
        if type(max_entries) is not int or not 1 <= max_entries <= 32_768:
            raise ValueError("evidence_capacity_invalid")
        self._source, self._capacity = source, max_entries
        self._clock, self._monotonic = clock, monotonic
        self._request_refresh = request_refresh
        self._entries: OrderedDict[str, _Locator] = OrderedDict()
        self._lock = RLock()
        self._epoch = 0

    def _budget(self, cancellation: CancellationCheck | None) -> QuestionBudget:
        return QuestionBudget(QuestionLimits(max_records=4),
                              monotonic=self._monotonic, cancellation_check=cancellation)

    @staticmethod
    def _reference(account: str, value: object) -> QuestionEvidence:
        reference = QuestionEvidence.model_validate(value)
        if reference.account_ref != account_ref(account):
            raise EvidenceUnavailable()
        return reference

    @staticmethod
    def _matches_location(account: str, reference: QuestionEvidence,
                          location: EvidenceLocation) -> None:
        if (reference.conversation_ref != conversation_ref(account, location.conversation_id)
                or reference.message_ref != message_ref(account, location.conversation_id,
                                                        location.message_id)):
            raise EvidenceUnavailable()

    def _prune(self, now: datetime) -> None:
        tick = self._monotonic()
        for key, entry in list(self._entries.items()):
            if now < entry.created_at or now >= entry.expires_at or tick >= entry.deadline:
                del self._entries[key]

    def _read_exact(self, account: str, reference: QuestionEvidence,
                    location: EvidenceLocation, budget: QuestionBudget) -> EvidenceMessage:
        with _safe_errors():
            budget.check()
            value = self._source.read_evidence_message(account, location, budget)
            if value is None:
                raise EvidenceUnavailable()
            message = EvidenceMessage.model_validate(value)
            if message.reference(reference.span) != reference:
                raise EvidenceUnavailable()
            if reference.span is not None and reference.span.end > len(message.text):
                raise EvidenceUnavailable()
            now = utc_instant(self._clock())
            if not historical_retention_cutoff(now) < reference.sent_at <= now:
                raise EvidenceUnavailable()
            budget.check()
            return message

    def _invalidate(self, account: str) -> None:
        scoped = account_ref(account)
        with self._lock:
            self._epoch += 1
            for key, entry in list(self._entries.items()):
                if entry.account == scoped:
                    del self._entries[key]
        if self._request_refresh is not None:
            try:
                self._request_refresh(account)
            except Exception:
                pass

    def bind(
        self, policy: RuntimePolicy, reference: object, location: object, *,
        valid_until: datetime, cancellation_check: CancellationCheck | None = None,
    ) -> None:
        """Register a query's exact source reference; never replace its version."""

        account = authorized_account(policy, None)
        with _safe_errors():
            ref = self._reference(account, reference)
            target = EvidenceLocation.model_validate(location)
            self._matches_location(account, ref, target)
            budget = self._budget(cancellation_check)
            with self._lock:
                epoch = self._epoch
            try:
                message = self._read_exact(account, ref, target, budget)
            except EvidenceUnavailable:
                self._invalidate(account)
                raise
            now = utc_instant(self._clock())
            expiry = min(utc_instant(valid_until), now + timedelta(minutes=15),
                         ref.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS))
            if expiry <= now:
                raise EvidenceUnavailable()
            entry = _Locator(account_ref(account), target, now, expiry,
                             self._monotonic() + (expiry - now).total_seconds(),
                             message.source_state_token)
            with self._lock:
                budget.check()
                if self._epoch != epoch:
                    raise EvidenceUnavailable()
                self._prune(now)
                key = digest(ref.model_dump(mode="json"))
                self._entries[key] = entry
                self._entries.move_to_end(key)
                while len(self._entries) > self._capacity:
                    self._entries.popitem(last=False)
            budget.check()

    def resolve(
        self, policy: RuntimePolicy, reference: object, *,
        cancellation_check: CancellationCheck | None = None,
    ) -> ResolvedEvidence:
        account = authorized_account(policy, None)
        with _safe_errors():
            ref = self._reference(account, reference)
            budget = self._budget(cancellation_check)
            budget.check()
            key = digest(ref.model_dump(mode="json"))
            now = utc_instant(self._clock())
            with self._lock:
                self._prune(now)
                entry = self._entries.get(key)
            if entry is None or entry.account != account_ref(account):
                raise EvidenceUnavailable()
            try:
                message = self._read_exact(account, ref, entry.location, budget)
                if message.source_state_token != entry.source_state_token:
                    raise EvidenceUnavailable()
                browser_span = None
                if ref.span is not None:
                    browser_span = SourceSpan(
                        start=len(message.text[:ref.span.start].encode("utf-16-le")) // 2,
                        end=len(message.text[:ref.span.end].encode("utf-16-le")) // 2,
                    )
                current = self._read_exact(account, ref, entry.location, budget)
                if current.source_state_token != entry.source_state_token:
                    raise EvidenceUnavailable()
            except EvidenceUnavailable:
                self._invalidate(account)
                raise
            checked_at = utc_instant(self._clock())
            with self._lock:
                self._prune(checked_at)
                if self._entries.get(key) is not entry:
                    raise EvidenceUnavailable()
                self._entries.move_to_end(key)
            budget.check()
            return ResolvedEvidence(reference=ref, location=entry.location,
                                    text=message.text, direction=message.direction,
                                    checked_at=checked_at, browser_span=browser_span)

    def resolve_context(self, policy, request, budget):
        """Read neighboring retained messages without extending the anchor lease."""
        account = authorized_account(policy, None)
        with _safe_errors():
            ref = self._reference(account, request.reference)
            key = digest(ref.model_dump(mode="json"))
            now = utc_instant(self._clock())
            with self._lock:
                self._prune(now)
                entry = self._entries.get(key)
            if entry is None or entry.account != account_ref(account) or entry.source_state_token is None:
                raise EvidenceUnavailable()
            try:
                anchor = self._read_exact(account, ref, entry.location, budget)
                if anchor.source_state_token != entry.source_state_token:
                    raise EvidenceUnavailable()
                locations, anchor_index, earlier, later = self._source.evidence_context_locations(
                    account, entry.location, request.before, request.after, budget,
                    cutoff=historical_retention_cutoff(now), now=now)
                if (not 1 <= len(locations) <= request.before + request.after + 1
                        or not 0 <= anchor_index < len(locations)
                        or locations[anchor_index] != entry.location):
                    raise EvidenceUnavailable()
                messages = []
                for index, location in enumerate(locations):
                    budget.check()
                    message = anchor if index == anchor_index else EvidenceMessage.model_validate(
                        self._source.read_evidence_message(account, location, budget))
                    reference = ref if index == anchor_index else message.reference()
                    self._matches_location(account, reference, location)
                    if (reference.account_ref != ref.account_ref
                            or reference.conversation_ref != ref.conversation_ref
                            or reference.source_revision != ref.source_revision
                            or message.source_state_token != entry.source_state_token):
                        raise EvidenceUnavailable()
                    messages.append((message, reference, location))
                items = []
                for message, reference, location in messages:
                    current = self._read_exact(account, reference, location, budget)
                    if current.source_state_token != entry.source_state_token:
                        raise EvidenceUnavailable()
                    span = reference.span
                    browser_span = None if span is None else SourceSpan(
                        start=len(message.text[:span.start].encode("utf-16-le")) // 2,
                        end=len(message.text[:span.end].encode("utf-16-le")) // 2)
                    items.append(EvidenceContextItem(reference=reference, text=message.text,
                        direction=message.direction, browser_span=browser_span))
            except EvidenceUnavailable:
                self._invalidate(account)
                raise
            checked_at = utc_instant(self._clock())
            with self._lock:
                self._prune(checked_at)
                if self._entries.get(key) is not entry:
                    raise EvidenceUnavailable()
            budget.check()
            response = ResolvedEvidenceContext(reference=ref,
                projection_generation=request.expected_generation,
                projection_digest=request.expected_projection_digest, checked_at=checked_at,
                anchor_index=anchor_index, items=tuple(items),
                has_earlier_stored_items=earlier, has_later_stored_items=later)
            return response, entry

    def check_context(self, policy, response, budget, entry):
        """Fence the original anchor lease and all neighbor retention after selection."""
        account = authorized_account(policy, None)
        with _safe_errors():
            ref = self._reference(account, response.reference)
            key = digest(ref.model_dump(mode="json"))
            now = utc_instant(self._clock())
            with self._lock:
                self._prune(now)
                if self._entries.get(key) is not entry or entry.account != account_ref(account):
                    raise EvidenceUnavailable()
            try:
                for item in response.items:
                    budget.check()
                    if not historical_retention_cutoff(now) < item.reference.sent_at <= now:
                        raise EvidenceUnavailable()
                message = self._read_exact(account, ref, entry.location, budget)
                if message.source_state_token != entry.source_state_token:
                    raise EvidenceUnavailable()
            except EvidenceUnavailable:
                self._invalidate(account)
                raise
            with self._lock:
                self._prune(utc_instant(self._clock()))
                if self._entries.get(key) is not entry:
                    raise EvidenceUnavailable()
            budget.check()

    def clear_account(self, policy: RuntimePolicy) -> None:
        """Discard source locators on account switch or deletion, without analysis."""

        scoped = account_ref(authorized_account(policy, None))
        with self._lock:
            self._epoch += 1
            for key, entry in list(self._entries.items()):
                if entry.account == scoped:
                    del self._entries[key]

    def expire(self) -> int:
        """Remove expired locators during local maintenance."""

        with self._lock:
            before = len(self._entries)
            self._prune(utc_instant(self._clock()))
            return before - len(self._entries)
