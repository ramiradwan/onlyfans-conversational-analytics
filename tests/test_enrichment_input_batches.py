"""Keep scalar cache identities while sharing bounded input preparation."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
import weakref

import pytest

from app.analytics import enrichment_cache as cache, enrichment_inputs as inputs
from app.analytics.analyzers import (RuleBasedSentimentAnalyzer,
    RuleBasedTopicEntityAnalyzer, RuleBasedEngagementAnalyzer)
from app.analytics.enrichment import EnrichmentStage
from app.analytics.errors import ProjectionBuildCancelled
from app.models.analytics import CanonicalConversation, CanonicalMessage, MessageAnalysisInput

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
BASES = (RuleBasedSentimentAnalyzer, RuleBasedTopicEntityAnalyzer, RuleBasedEngagementAnalyzer)


def messages(count, *, account='synthetic-owner'):
    return {index: MessageAnalysisInput(creator_account_id=account, conversation_id='chat',
        participant_id='fan', message_id=str(index), text='Thanks café $10',
        sent_at=NOW + timedelta(minutes=index), direction='inbound') for index in range(count)}


def digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps({'name': 'enrichment_reuse',
        'revision': 'v1', 'config': value}, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def document(message):
    return {'creator_account_id': message.creator_account_id,
        'conversation_id': message.conversation_id, 'participant_id': message.participant_id,
        'message_id': message.message_id, 'text': message.text,
        'sent_at': message.sent_at.astimezone(timezone.utc).isoformat(),
        'direction': message.direction.value}


@pytest.mark.parametrize('before,after', [(0, 0), (1, 0), (0, 1), (32, 32), (2, 7)])
@pytest.mark.parametrize('index', [0, 31, 63, 95])
def test_batched_keys_match_scalar_keys_and_independent_digests(before, after, index):
    values = messages(128)
    batch = cache.EnrichmentKeyBatch(values)
    stage = EnrichmentStage()
    for slot, descriptor in zip(cache.RESULT_TYPES, stage._descriptors, strict=True):
        policy = cache.AnalyzerCachePolicy(taxonomy_revision='test.v1',
            preceding_messages=before, following_messages=after)
        context = tuple(i for i in range(max(0, index-before), min(128, index+after+1)) if i != index)
        adapter = {'descriptor': descriptor.model_dump(mode='json'), 'policy': policy.model_dump(mode='json')}
        key = batch.key(index, slot, digest(adapter), context)
        assert key == cache.enrichment_key(values[index], slot, descriptor, policy,
                                           tuple(values[i] for i in context))
        assert key.input_digest == digest(document(values[index]))
        assert key.context_digest == digest([document(values[i]) for i in context])
        assert key.adapter_digest == digest(adapter)
        assert key.expires_at == min(values[i].sent_at for i in (index, *context)) + timedelta(days=90)


def conversation(count):
    return CanonicalConversation(conversation_id='chat', platform_user_id='fan', messages=[
        CanonicalMessage(message_id=value.message_id, source_ordinal=index, text=value.text,
            sent_at=value.sent_at, direction=value.direction) for index, value in messages(count).items()])


def run_stage(stage, count, *, cancellation=None):
    batches = []
    reuse = SimpleNamespace(prefetch=lambda keys: batches.append(keys), get=lambda key: None,
                            retain=lambda key, result: None)
    token = cache.ACTIVE_REUSE.set(reuse)
    try:
        result = stage.enrich_conversation('synthetic-owner', conversation(count), cancellation_check=cancellation)
    finally:
        cache.ACTIVE_REUSE.reset(token)
    return result, batches


@pytest.mark.parametrize('count', [1, 63, 64, 65, 129])
def test_default_inputs_and_empty_context_are_encoded_once_per_batch(monkeypatch, count):
    calls = Counter()
    fingerprint, input_document = cache.fingerprint, cache.input_document
    def observed(value):
        calls['adapter' if isinstance(value, dict) and 'descriptor' in value else
              'context' if isinstance(value, list) else 'input'] += 1
        return fingerprint(value)
    def encoded(message):
        calls['document'] += 1
        return input_document(message)
    monkeypatch.setattr(cache, 'fingerprint', observed)
    monkeypatch.setattr(inputs, 'fingerprint', observed)
    monkeypatch.setattr(cache, 'input_document', encoded)
    stage = EnrichmentStage()
    output, batches = run_stage(stage, count)
    assert calls == {'adapter': 3, 'context': (count+63)//64, 'input': count, 'document': count}
    assert all(len(batch) <= 192 for batch in batches)
    assert sum(map(len, batches)) == count * 3
    assert output == stage.enrich_conversation('synthetic-owner', conversation(count))


def contextual_stage(before=32, after=32, *, mutate=False):
    analyzers = []
    for base in BASES:
        class Contextual(base):
            cache_policy = cache.AnalyzerCachePolicy(taxonomy_revision='synthetic.v1',
                preceding_messages=before, following_messages=after)
            def analyze_with_context(self, message, context):
                result = self.analyze(message)
                if mutate:
                    object.__setattr__(message, 'text', 'changed')
                    for other in context:
                        object.__setattr__(other, 'text', 'changed')
                return result
        analyzers.append(Contextual())
    return EnrichmentStage(sentiment=analyzers[0], topics_entities=analyzers[1], engagement=analyzers[2])


def test_context_inputs_share_models_and_release_each_batch(monkeypatch):
    created, sizes, references = [], [], []
    model, batch_type = inputs.MessageAnalysisInput, inputs.EnrichmentKeyBatch
    def construct(**kwargs):
        created.append(kwargs['message_id'])
        return model(**kwargs)
    class ObservedBatch(batch_type):
        def __init__(self, values):
            super().__init__(values)
            sizes.append(len(values))
            references.append(weakref.ref(self))
    monkeypatch.setattr(inputs, 'MessageAnalysisInput', construct)
    monkeypatch.setattr(inputs, 'EnrichmentKeyBatch', ObservedBatch)
    output, batches = run_stage(contextual_stage(), 193)
    assert sizes == [96, 128, 97, 33]
    assert len(created) == sum(sizes)
    assert all(item() is None for item in references)
    assert sum(map(len, batches)) == 193 * 3
    assert len(output) == 193


def test_analyzer_mutation_cannot_change_other_inputs_or_keys():
    ordinary, ordinary_keys = run_stage(contextual_stage(2, 2), 65)
    mutated, mutated_keys = run_stage(contextual_stage(2, 2, mutate=True), 65)
    assert ordinary == mutated
    assert ordinary_keys == mutated_keys


@pytest.mark.parametrize('enabled', [False, True])
def test_undeclared_inputs_do_not_compute_cache_keys(monkeypatch, enabled):
    analyzers = [base() for base in BASES]
    for analyzer in analyzers:
        analyzer.cache_policy = None
    stage = EnrichmentStage(sentiment=analyzers[0], topics_entities=analyzers[1], engagement=analyzers[2])
    def forbidden(*args, **kwargs):
        raise AssertionError('uncacheable inputs were fingerprinted')
    monkeypatch.setattr(inputs, 'fingerprint', forbidden)
    monkeypatch.setattr(cache, 'fingerprint', forbidden)
    if enabled:
        output, batches = run_stage(stage, 3)
        assert batches == [[]]
    else:
        output = stage.enrich_conversation('synthetic-owner', conversation(3))
    assert len(output) == 3


@pytest.mark.parametrize('cancel_after', [0, 10, 65])
def test_input_preparation_obeys_cancellation(monkeypatch, cancel_after):
    model, created = inputs.MessageAnalysisInput, []
    def construct(**kwargs):
        created.append(kwargs['message_id'])
        return model(**kwargs)
    monkeypatch.setattr(inputs, 'MessageAnalysisInput', construct)
    with pytest.raises(ProjectionBuildCancelled):
        run_stage(contextual_stage(), 193, cancellation=lambda: len(created) >= cancel_after)
    assert len(created) == cancel_after


def test_input_batch_rejects_more_than_one_batch_and_context_window():
    with pytest.raises(ValueError, match='enrichment_input_batch_invalid'):
        cache.EnrichmentKeyBatch(messages(129))


@pytest.mark.parametrize('field,value', [
    ('creator_account_id', 'other-owner'), ('conversation_id', 'other-chat'),
    ('participant_id', 'other-fan'), ('message_id', 'other-message'),
    ('text', 'different text'), ('direction', 'outbound'),
    ('sent_at', NOW + timedelta(hours=1)),
])
def test_new_batches_never_reuse_identity_from_changed_inputs(field, value):
    old = messages(1)
    changed = {0: MessageAnalysisInput.model_validate({**old[0].model_dump(), field: value})}
    adapter = 'sha256:' + 'a' * 64
    first = cache.EnrichmentKeyBatch(old).key(0, 'sentiment', adapter, ())
    second = cache.EnrichmentKeyBatch(changed).key(0, 'sentiment', adapter, ())
    assert first.input_digest != second.input_digest
    assert first.digest != second.digest


def test_context_windows_cross_lookup_boundaries_without_changing_keys():
    stage = contextual_stage()
    _, batches = run_stage(stage, 193)
    actual = [key for batch in batches for key in batch]
    values = messages(193)
    for index in (0, 31, 63, 64, 95, 127, 128, 160, 192):
        context = tuple(values[i] for i in range(max(0, index-32), min(193, index+33)) if i != index)
        for offset, (slot, descriptor, policy) in enumerate(zip(cache.RESULT_TYPES,
                stage._descriptors, stage._input_policies, strict=True)):
            assert actual[index*3+offset] == cache.enrichment_key(values[index], slot,
                descriptor, policy, context)
