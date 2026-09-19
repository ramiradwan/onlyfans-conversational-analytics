"""Source facts consumed by bounded conversation question handlers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal, Protocol

from app.analytics.evidence_contracts import EvidenceLocation
from app.analytics.query_contracts import QuestionEvidence, ResolvedQuestion
from app.analytics.query_execution import QuestionBudget, QuestionReadSession


@dataclass(frozen=True, slots=True)
class QuestionMessage:
    message_ref: str
    sent_at: datetime
    role: Literal["participant", "creator", "system", "unknown"]
    kind: Literal["message", "attachment", "system", "unknown"]
    order: int | None = None
    ordering: Literal["source", "inferred"] = "inferred"
    pricing: Literal["positive", "negative", "uncertain", "unsupported", "missing", "error"] = "missing"
    classification_current: bool = False
    language: str | None = None


@dataclass(frozen=True, slots=True)
class QuestionConversation:
    conversation_ref: str
    messages: tuple[QuestionMessage, ...]
    coverage: Literal["complete", "partial", "unknown"] = "unknown"


class QuestionFactsSession(QuestionReadSession, Protocol):
    def conversations(self, question: ResolvedQuestion, budget: QuestionBudget
                      ) -> Iterable[QuestionConversation]: ...

    def evidence(self, message: QuestionMessage, budget: QuestionBudget
                 ) -> tuple[QuestionEvidence, EvidenceLocation]: ...
