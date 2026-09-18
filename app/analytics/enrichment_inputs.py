"""Apply declared analyzer inputs and reuse only exact per-analyzer results."""

from app.analytics.cancellation import check_cancelled
from app.analytics.enrichment_cache import (
    ACTIVE_REUSE, AnalyzerCachePolicy, CACHE_BATCH_SIZE, RESULT_TYPES, enrichment_key,
)
from app.analytics.errors import AnalyzerConfigurationInvalid
from app.models.analytics import AnalysisMode, MessageAnalysisInput


def analyzer_policy(analyzer):
    policy = getattr(analyzer, "cache_policy", None)
    if policy is None:
        return None
    if not isinstance(policy, AnalyzerCachePolicy):
        raise AnalyzerConfigurationInvalid()
    if analyzer.mode == AnalysisMode.MODEL and policy.model_digest is None:
        raise AnalyzerConfigurationInvalid()
    if (policy.preceding_messages or policy.following_messages) and not callable(
        getattr(analyzer, "analyze_with_context", None)
    ):
        raise AnalyzerConfigurationInvalid()
    return policy


def analyze_messages(account_id, conversation, ordered, analyzers, descriptors, cancellation):
    reuse = ACTIVE_REUSE.get()
    policies = tuple(analyzer_policy(analyzer) for analyzer in analyzers)
    slots = tuple(RESULT_TYPES)

    def input_at(index):
        message = ordered[index]
        return MessageAnalysisInput(
            creator_account_id=account_id, conversation_id=conversation.conversation_id,
            participant_id=conversation.platform_user_id, message_id=message.message_id,
            text=message.text, sent_at=message.sent_at, direction=message.direction,
        )

    for offset in range(0, len(ordered), CACHE_BATCH_SIZE):
        check_cancelled(cancellation)
        jobs, keys = [], []
        for index in range(offset, min(offset + CACHE_BATCH_SIZE, len(ordered))):
            message = input_at(index)
            inputs = []
            for slot, descriptor, policy in zip(slots, descriptors, policies, strict=True):
                context = () if policy is None else tuple(
                    input_at(other) for other in range(
                        max(0, index - policy.preceding_messages),
                        min(len(ordered), index + policy.following_messages + 1),
                    ) if other != index
                )
                key = enrichment_key(message, slot, descriptor, policy, context) if reuse and policy else None
                if key is not None:
                    keys.append(key)
                inputs.append((slot, context, key))
            jobs.append((ordered[index], message, inputs))
        if reuse:
            reuse.prefetch(keys)
        for source, message, inputs in jobs:
            results = []
            for analyzer, descriptor, policy, (slot, context, key) in zip(
                analyzers, descriptors, policies, inputs, strict=True
            ):
                check_cancelled(cancellation)
                metadata = {"analyzer_name": descriptor.analyzer_name,
                    "analyzer_revision": descriptor.revision,
                    "analyzer_config_digest": descriptor.config_digest,
                    "analysis_mode": descriptor.mode,
                    "calibration_status": descriptor.calibration_status}
                result = reuse.get(key) if key is not None else None
                if result is not None and any(getattr(result, k) != v for k, v in metadata.items()):
                    result = None
                if result is None:
                    value = message.model_copy(deep=True)
                    if policy and (policy.preceding_messages or policy.following_messages):
                        result = analyzer.analyze_with_context(value, tuple(m.model_copy(deep=True) for m in context))
                    else:
                        result = analyzer.analyze(value)
                    check_cancelled(cancellation)
                    result = RESULT_TYPES[slot].model_validate({**result.model_dump(), **metadata})
                if key is not None:
                    reuse.retain(key, result)
                results.append(result)
            yield source, results
