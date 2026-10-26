"""Compiled identities for licensed message-local execution."""
from typing import Literal

from app.analytics.evidence_contracts import MAX_EVIDENCE_TEXT_CHARS
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS, RETENTION_BASIS
from app.models.text_analysis_jobs import ExecutionParameters
from app.nlp.contracts import Digest
from app.nlp.profiles import Descriptor, TaskBinding, artifact_closure_digest


class SourcePolicy(Descriptor):
    schema_version: Literal['text-analysis-source-policy.v1'] = 'text-analysis-source-policy.v1'
    unit: Literal['message-local-original-text'] = 'message-local-original-text'
    directions: Literal['both'] = 'both'
    population: Literal['complete-retained-eligible-account'] = 'complete-retained-eligible-account'
    max_text_chars: int = MAX_EVIDENCE_TEXT_CHARS
    retention_days: int = PARTICIPANT_ANALYTICS_MAX_DAYS
    retention_basis: Literal['canonical_message_sent_at'] = RETENTION_BASIS


class ComponentRecipe(Descriptor):
    schema_version: Literal['text-analysis-component-recipe.v1'] = 'text-analysis-component-recipe.v1'
    components: tuple[Digest, ...]
    bindings: tuple[TaskBinding, ...]


class ExecutionRecipe(Descriptor):
    schema_version: Literal['text-analysis-execution-recipe.v1'] = 'text-analysis-execution-recipe.v1'
    algorithm_profile_sha256: Digest
    algorithm_artifacts_sha256: Digest
    policy_identity_sha256: Digest
    model_or_rules_identity_sha256: Digest
    output: Literal['licensed-full-record'] = 'licensed-full-record'


SOURCE_POLICY = SourcePolicy()


def execution_parameters(profile, closure: str) -> ExecutionParameters:
    data = tuple(a for a in profile.artifacts if a.role in ('weights', 'tokenizer', 'configuration'))
    if artifact_closure_digest(data) != closure:
        raise ValueError('text_analysis_artifact_closure_mismatch')
    recipe = ExecutionRecipe(algorithm_profile_sha256=profile.identity_digest(),
        algorithm_artifacts_sha256=closure, policy_identity_sha256=SOURCE_POLICY.identity_digest(),
        model_or_rules_identity_sha256=ComponentRecipe(
            components=tuple(c.identity_digest() for c in profile.components), bindings=profile.bindings).identity_digest())
    return ExecutionParameters(parameter_sha256=recipe.identity_digest(),
        **recipe.model_dump(exclude={'schema_version', 'canonicalization', 'output'}))


def require_original_parameters(profile, parameters: ExecutionParameters) -> None:
    if execution_parameters(profile, parameters.algorithm_artifacts_sha256) != parameters:
        raise ValueError('text_analysis_parameter_version_unavailable')
