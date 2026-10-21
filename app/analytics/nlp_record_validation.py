"""Bind existing NLP records to one supplied trusted evidence snapshot."""
from __future__ import annotations

import hashlib

from app.analytics.evidence_contracts import EvidenceMessage, MAX_EVIDENCE_TEXT_CHARS
from app.analytics.opaque_refs import participant_ref
from app.nlp.contracts import NlpBoundaryError, SourceBinding
from app.nlp.profiles import AnalysisProfile
from app.nlp import v2


def _check_unicode(value):
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
    elif isinstance(value, dict):
        for key, child in value.items():
            _check_unicode(key)
            _check_unicode(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            _check_unicode(child)


def validate_source_bound_record(profile: AnalysisProfile, source: EvidenceMessage,
                                 frame: bytes) -> v2.EnrichmentRecord:
    """Validate both tasks against unchanged original text and source identity.

    The caller supplies the trusted canonical snapshot. This performs no live
    source lookup, authorization, qualification, persistence or publication.
    """
    try:
        if type(frame) is not bytes or not 0 < len(frame) <= v2.MAX_RECORD_BYTES:
            raise ValueError()
        if type(profile) is not AnalysisProfile or type(source) is not EvidenceMessage:
            raise ValueError()
        if type(source.text) is not str or len(source.text) > MAX_EVIDENCE_TEXT_CHARS:
            raise ValueError()
        # Dump to ordinary values so unchecked instances and nested models must
        # pass their contracts again, without rendering serializer warnings.
        profile = AnalysisProfile.model_validate(profile.model_dump(mode="python", warnings=False))
        source = EvidenceMessage.model_validate(source.model_dump(mode="python", warnings=False))
        _check_unicode(profile.model_dump(mode="python", warnings=False))
        _check_unicode(source.model_dump(mode="python", warnings=False))
        reference = source.reference()
        binding = SourceBinding(
            account_scope=reference.account_ref,
            source_ref=reference.message_ref,
            source_version=reference.source_version_digest.removeprefix("sha256:"),
            speaker_ref=participant_ref(source.account_id, source.sender_id),
        )
        record = v2.validate_record(profile, binding, frame)
        text_digest = hashlib.sha256(source.text.encode("utf-8")).hexdigest()
        for task in record.tasks:
            if (task.result.text_sha256 != text_digest
                    or task.result.payload.coverage.source_codepoints != len(source.text)):
                raise ValueError()
        return record
    except Exception:
        raise NlpBoundaryError("nlp_invalid_source_record") from None
