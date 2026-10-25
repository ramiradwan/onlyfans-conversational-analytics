"""Closed text-analysis scope and rights decisions."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import re

from app.models.knowledge_contributions import CapabilityBinding
from app.security.grant_types import ACCOUNT_AUTHORITY_GRANT_TYPES
from app.security.runtime_policy import RuntimeAuthorizationDenied, RuntimePolicy, require_identity
from app.security.text_analysis_contract import OUTPUT_POLICY, canonical, parse_json
from app.security.text_analysis_references import (
    PROFILE_SHA256, TextAnalysisLicenseReference, TextAnalysisPreviewReference,
    TextAnalysisReference, _plain,
)


@dataclass(frozen=True, slots=True)
class TextAnalysisRunContext:
    reference_id: str
    compact_jws_sha256: str
    principal_id: str
    creator_account_id: str
    organization_id: str
    installation_id: str
    installation_key_id: str
    installation_key_jkt: str
    seat_id: str
    seat_scope: str
    licensed_major_version: int
    selected_major_version: int
    artifact_family: str
    algorithm_profile_sha256: str
    algorithm_artifacts_sha256: str
    policy_identity_sha256: str
    fallback_major_version: int | None = None
    activation_lineage_sha256: str | None = None

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name in {'licensed_major_version', 'selected_major_version', 'fallback_major_version'}:
                if value is None and item.name == 'fallback_major_version':
                    continue
                if type(value) is not int or not 1 <= value <= 2147483647:
                    raise ValueError('invalid_text_analysis_major')
            elif item.name.endswith('sha256'):
                if value is None and item.name == 'activation_lineage_sha256':
                    continue
                if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
                    raise ValueError('invalid_text_analysis_digest')
            elif type(value) is not str or not 0 < len(value) <= 512:
                raise ValueError('invalid_text_analysis_context')
        if self.artifact_family != 'text-analysis-artifact':
            raise ValueError('invalid_text_analysis_family')


def encode_run_context(context: TextAnalysisRunContext) -> bytes:
    if type(context) is not TextAnalysisRunContext:
        raise ValueError('invalid_text_analysis_context')
    return canonical(asdict(context))


def decode_run_context(raw: bytes) -> TextAnalysisRunContext:
    value = parse_json(raw, maximum=16384, require_canonical=True)
    if type(value) is not dict or set(value) != {item.name for item in fields(TextAnalysisRunContext)}:
        raise ValueError('invalid_text_analysis_context')
    return TextAnalysisRunContext(**value)


def _deny(reason):
    raise RuntimeAuthorizationDenied(reason)


def require_text_analysis(policy: RuntimePolicy, context: TextAnalysisRunContext, *, kind: str, phase: str) -> TextAnalysisReference:
    if kind not in {'license', 'preview'} or phase not in {'import', 'prepare', 'start', 'resume', 'commit'}:
        _deny('invalid_text_analysis_operation')
    encode_run_context(context)
    identity = require_identity(policy)
    if (identity.principal_id, identity.creator_account_id) != (context.principal_id, context.creator_account_id):
        _deny('text_analysis_account_mismatch')
    account = policy.identity_authority
    if account is None or not set(ACCOUNT_AUTHORITY_GRANT_TYPES).issubset(account.grant_types):
        _deny('text_analysis_identity_missing')
    for name in ('organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt'):
        if getattr(account, name) != getattr(context, name):
            _deny('text_analysis_identity_mismatch')
    reference = policy.text_analysis_authority
    expected = TextAnalysisLicenseReference if kind == 'license' else TextAnalysisPreviewReference
    if type(reference) is not expected:
        _deny('text_analysis_authority_missing')
    if (reference.reference_id, reference.compact_jws_sha256) != (context.reference_id, context.compact_jws_sha256):
        _deny('text_analysis_reference_mismatch')
    claims = reference.claims
    for name in ('organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt', 'seat_id', 'seat_scope', 'licensed_major_version'):
        if type(claims[name]) is not type(getattr(context, name)) or claims[name] != getattr(context, name):
            _deny('text_analysis_scope_mismatch')
    subject = f'organization:{context.organization_id}:installation:{context.installation_id}:seat:{context.seat_id}:capability:text-analysis'
    if claims['sub'] != subject or claims['capability'] != 'text-analysis' or claims['compatible_artifact_family'] != context.artifact_family:
        _deny('text_analysis_scope_mismatch')
    if context.selected_major_version == context.licensed_major_version:
        if context.fallback_major_version is not None:
            _deny('text_analysis_fallback_mismatch')
    elif (context.fallback_major_version != context.selected_major_version
          or context.selected_major_version >= context.licensed_major_version
          or context.selected_major_version not in claims['fallback_major_versions']):
        _deny('text_analysis_fallback_denied')
    if context.activation_lineage_sha256 is not None and claims['update_rights'] is not True:
        _deny('text_analysis_update_denied')
    if kind == 'preview' and (_plain(claims['output_policy']) != OUTPUT_POLICY or claims['execution_limit'] != 1):
        _deny('text_analysis_output_policy_mismatch')
    return reference


def capability_binding(reference: TextAnalysisReference, context: TextAnalysisRunContext) -> CapabilityBinding:
    if type(reference) not in {TextAnalysisLicenseReference, TextAnalysisPreviewReference}:
        _deny('text_analysis_authority_missing')
    if reference.reference_id != context.reference_id or reference.compact_jws_sha256 != context.compact_jws_sha256:
        _deny('text_analysis_reference_mismatch')
    return CapabilityBinding(capability_id='text-analysis', artifact_family=context.artifact_family,
        artifact_major=context.selected_major_version, object_sha256=reference.compact_jws_sha256,
        object_reference_id=reference.reference_id, profile_id=reference.claims['profile'],
        profile_sha256=PROFILE_SHA256[reference.kind])


def require_text_analysis_fence(policy: RuntimePolicy, context: TextAnalysisRunContext, *, kind: str, phase: str,
                                canonical_database=None) -> TextAnalysisReference:
    from app.core.optional_execution import require_optional_execution
    require_optional_execution()
    reference = require_text_analysis(policy, context, kind=kind, phase=phase)
    from app.persistence.text_analysis_authority import TextAnalysisTransitionFence
    fence = policy.text_analysis_fence
    if type(fence) is not TextAnalysisTransitionFence or not fence.is_current(policy, hashlib.sha256(encode_run_context(context)).hexdigest(), phase=phase):
        _deny('text_analysis_transition_fence_missing')
    if canonical_database is not None and not fence.owns_canonical_database(canonical_database):
        _deny('text_analysis_canonical_owner_mismatch')
    return reference
