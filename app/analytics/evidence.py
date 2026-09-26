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
    EvidenceLocation, EvidenceMessage, EvidenceSource, ResolvedEvidence,
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
                self._read_exact(account, ref, target, budget)
            except EvidenceUnavailable:
                self._invalidate(account)
                raise
            now = utc_instant(self._clock())
            expiry = min(utc_instant(valid_until), now + timedelta(minutes=15),
                         ref.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS))
            if expiry <= now:
                raise EvidenceUnavailable()
            entry = _Locator(account_ref(account), target, now, expiry,
                             self._monotonic() + (expiry - now).total_seconds())
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
                browser_span = None
                if ref.span is not None:
                    browser_span = SourceSpan(
                        start=len(message.text[:ref.span.start].encode("utf-16-le")) // 2,
                        end=len(message.text[:ref.span.end].encode("utf-16-le")) // 2,
                    )
                self._read_exact(account, ref, entry.location, budget)
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
