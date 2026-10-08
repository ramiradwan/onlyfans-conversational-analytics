"""Source facts consumed by bounded conversation question handlers."""

from __future__ import annotations

import json
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
def imported_history_kind(document, conversation):
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except (ValueError, TypeError):
            return 'unknown'
    if not isinstance(document, dict):
        return 'unknown'
    context = document.get('context')
    source = document.get('source_kind_evidence')
    kind = document.get('kind')
    pin = document.get('client_pin_set_version')
    if not isinstance(kind, str) or not isinstance(pin, str):
        return 'unknown'
    if (document.get('schema') != 'connector-history-kind/v1'
            or document.get('mapping_version') != 'onlyfans-event-kind/0.2.0'
            or document.get('evidence_standard') != 'client-parity'
            or document.get('evidence_state') != 'supported'
            or pin not in {
                'onlyfans-client-parity-pins/2026-10-03.1', 'onlyfans-client-parity-pins/2026-10-04.1'}
            or not isinstance(source, dict) or source.get('availability') != 'available'
            or not isinstance(context, dict)
            or not isinstance(context.get('account_id'), str) or not context['account_id']
            or not isinstance(context.get('generation_id'), str)
            or not 0 < len(context['generation_id']) <= 256
            or context.get('method') != 'GET'
            or context.get('endpoint') != '/api2/v2/chats/{id}/messages'
            or context.get('surface') != 'native-rest-message-page'
            or context.get('conversation_id') != conversation):
        return 'unknown'
    if document.get('rule_id') != {'human_message': 'CP-H1', 'non_message_event': 'CP-S1'}.get(kind):
        return 'unknown'
    return {'human_message': 'message', 'non_message_event': 'system'}.get(kind, 'unknown')
