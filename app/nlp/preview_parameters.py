"""Versioned identities for the disclosed preview sample and selected output."""
import hashlib
from typing import Literal

from app.models.text_analysis_jobs import ExecutionParameters
from app.nlp.profiles import ComponentRecipe, Descriptor, PolicyIdentity, artifact_closure_digest


SELECTION_POLICY = PolicyIdentity(policy_id="text-analysis-preview-selection.v1", revision="1",
    sha256=hashlib.sha256(b"text-analysis-preview-selection.v1\0category-summary;conversations-desc;"
        b"messages-desc;mention-occurrences-desc;category-id-asc;complete-observed-evidence;"
        b"resolved-complete-additional-categories;shown-only").hexdigest())
RECURRENCE_SELECTION_POLICY = PolicyIdentity(policy_id="text-analysis-recurring-mentions-preview-selection.v1",
    revision="1", sha256=hashlib.sha256(b"recurring-mentions-preview.v1\0exact-nfc-trim-case-preserved;"
        b"two-distinct-incoming-correspondents;unique-source-span;senders-desc;messages-desc;"
        b"literal-codepoints-asc;complete-observed-evidence;resolved-additional-phrases;shown-only").hexdigest())


class PreviewSourcePolicy(Descriptor):
    schema_version: Literal['text-analysis-preview-source-policy.v1'] = 'text-analysis-preview-source-policy.v1'
    unit: Literal['message-local-original-text'] = 'message-local-original-text'
    directions: Literal['both'] = 'both'
    population: Literal['latest-live-evidence-eligible-at-cutoff'] = 'latest-live-evidence-eligible-at-cutoff'
    max_messages: Literal[32] = 32
    max_text_chars: Literal[65536] = 65536
    retention_days: Literal[90] = 90
    interval: Literal['lower-exclusive-cutoff-inclusive'] = 'lower-exclusive-cutoff-inclusive'
    ordering: Literal['utc-sent-at-message-id'] = 'utc-sent-at-message-id'


class PreviewRecipe(Descriptor):
    schema_version: Literal['text-analysis-preview-recipe.v1'] = 'text-analysis-preview-recipe.v1'
    algorithm_profile_sha256: str
    algorithm_artifacts_sha256: str
    policy_identity_sha256: str
    model_or_rules_identity_sha256: str
    selection_policy_sha256: str
    output: Literal['selected-preview-only'] = 'selected-preview-only'


class RecurringMentionSourcePolicy(PreviewSourcePolicy):
    schema_version: Literal['text-analysis-preview-source-policy.v2'] = 'text-analysis-preview-source-policy.v2'
    directions: Literal['inbound'] = 'inbound'
    population: Literal['latest-live-incoming-participant-at-cutoff'] = 'latest-live-incoming-participant-at-cutoff'
    max_messages: Literal[100] = 100


PREVIEW_SOURCE_POLICY = PreviewSourcePolicy()
RECURRENCE_SOURCE_POLICY = RecurringMentionSourcePolicy()
RECURRENCE_SOURCE_DIGEST = RECURRENCE_SOURCE_POLICY.identity_digest()


def preview_parameters(profile, closure, selection_policy) -> ExecutionParameters:
    data = tuple(a for a in profile.artifacts if a.role in ('weights', 'tokenizer', 'configuration'))
    if artifact_closure_digest(data) != closure:
        raise ValueError('text_analysis_artifact_closure_mismatch')
    if selection_policy not in (SELECTION_POLICY, RECURRENCE_SELECTION_POLICY):
        raise ValueError('text_analysis_preview_recipe_unavailable')
    if selection_policy == RECURRENCE_SELECTION_POLICY and [b.task for b in profile.bindings] != ['concept_mentions']:
        raise ValueError('text_analysis_preview_mentions_unavailable')
    source_policy = RECURRENCE_SOURCE_POLICY if selection_policy == RECURRENCE_SELECTION_POLICY else PREVIEW_SOURCE_POLICY
    recipe = PreviewRecipe(algorithm_profile_sha256=profile.identity_digest(),
        algorithm_artifacts_sha256=closure, policy_identity_sha256=source_policy.identity_digest(),
        model_or_rules_identity_sha256=ComponentRecipe(
            components=tuple(c.identity_digest() for c in profile.components), bindings=profile.bindings).identity_digest(),
        selection_policy_sha256=selection_policy.sha256)
    return ExecutionParameters(parameter_sha256=recipe.identity_digest(),
        **recipe.model_dump(exclude={'schema_version', 'canonicalization', 'output', 'selection_policy_sha256'}))


def require_preview_parameters(profile, parameters, selection_policy):
    if preview_parameters(profile, parameters.algorithm_artifacts_sha256, selection_policy) != parameters:
        raise ValueError('text_analysis_preview_recipe_unavailable')
