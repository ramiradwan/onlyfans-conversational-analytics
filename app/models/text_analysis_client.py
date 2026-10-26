"""Closed pre-dispatch requests and original-actor acknowledgements."""

from typing import Annotated, Literal
from pydantic import Field, field_validator, model_validator
from app.analytics.query_contracts import Instant
from app.models.text_analysis_enrollment import EnrollmentCommand
from app.models.text_analysis_jobs import ExecutionIntent, PreviewConfirmation
from app.models.text_analysis_preview_execution import PreviewPrepareRequest
from app.nlp.contracts import Contract, Digest

ClientIntentKind = Literal['enrollment_command', 'preview_prepare', 'preview_confirm']


class PreviewConfirmIntent(Contract):
    external_key: str
    confirmation: PreviewConfirmation
    _uuid = field_validator('external_key')(ExecutionIntent.uuid4.__func__)

    @model_validator(mode='after')
    def bound(self):
        if self.external_key != self.confirmation.external_key:
            raise ValueError('client_intent_key_mismatch')
        return self


class ClientIntentPut(Contract):
    schema_version: Literal['text-analysis-client-intent.v1'] = (
        'text-analysis-client-intent.v1'
    )
    kind: ClientIntentKind
    key: str
    body: EnrollmentCommand | PreviewPrepareRequest | PreviewConfirmIntent
    _uuid = field_validator('key')(ExecutionIntent.uuid4.__func__)

    @model_validator(mode='after')
    def bound(self):
        model = {
            'enrollment_command': EnrollmentCommand,
            'preview_prepare': PreviewPrepareRequest,
            'preview_confirm': PreviewConfirmIntent,
        }[self.kind]
        if type(self.body) is not model:
            raise ValueError('client_intent_kind_mismatch')
        key = (
            self.body.command_key
            if self.kind == 'enrollment_command'
            else self.body.external_key
        )
        if key != self.key:
            raise ValueError('client_intent_key_mismatch')
        from app.nlp.profiles import canonical_bytes

        if len(canonical_bytes(self.body)) > 8192:
            raise ValueError('client_intent_body_limit')
        return self


class ClientIntentRecord(ClientIntentPut):
    schema_version: Literal['text-analysis-client-intent-record.v1'] = (
        'text-analysis-client-intent-record.v1'
    )
    body_sha256: Digest
    retained_at: Instant

    @model_validator(mode='after')
    def digest(self):
        import hashlib
        from app.nlp.profiles import canonical_bytes

        if hashlib.sha256(canonical_bytes(self.body)).hexdigest() != self.body_sha256:
            raise ValueError('client_intent_body_mismatch')
        return self


class ClientIntentCursor(Contract):
    kind: ClientIntentKind
    key: str
    _uuid = field_validator('key')(ExecutionIntent.uuid4.__func__)


class ClientIntentPage(Contract):
    items: Annotated[tuple[ClientIntentRecord, ...], Field(max_length=64)]
    next_after: ClientIntentCursor | None

    @model_validator(mode='after')
    def ordered(self):
        keys = [(item.kind, item.key) for item in self.items]
        if keys != sorted(set(keys)) or (
            self.next_after is not None
            and (not keys or (self.next_after.kind, self.next_after.key) != keys[-1])
        ):
            raise ValueError('client_intent_page_invalid')
        return self
