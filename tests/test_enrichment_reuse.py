"""Verify analyzer calls and exact rebuild output using independent source mutations."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.analytics.analyzers import RuleBasedSentimentAnalyzer, RuleBasedTopicEntityAnalyzer, RuleBasedEngagementAnalyzer
from app.analytics.enrichment import EnrichmentStage
from app.analytics.enrichment_cache import AnalyzerCachePolicy
from app.analytics.factory import create_analytics_stores
from app.analytics.identity import canonical_identity
from app.analytics.pipeline import AnalyticsPipeline
from app.canonical.read_models import AccountReadModel
from app.persistence.projection_activation import InMemoryProjectionActivationRepository

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
ACCOUNT = "synthetic-cache-owner"


class Source:
    def __init__(self):
        self.revision = 1
        self.conversations = {"chat": {"conversation_id": "chat", "platform_user_id": "fan",
            "messages": [self.message(index) for index in range(3)]}}

    @staticmethod
    def message(index):
        return {"message_id": str(index), "source_ordinal": index, "text": f"Thanks price ${index+1}",
                "sent_at": NOW-timedelta(days=2)+timedelta(minutes=index), "direction": "inbound"}

    def account_exists(self, account):
        return account in {ACCOUNT, "synthetic-other"}

    def account_revisions(self):
        return [(ACCOUNT, self.revision)]

    def account_read_model(self, account):
        return AccountReadModel(view_revision=self.revision, conversations=deepcopy(self.conversations))


def counters():
    instances = []
    for base in (RuleBasedSentimentAnalyzer, RuleBasedTopicEntityAnalyzer, RuleBasedEngagementAnalyzer):
        class Counting(base):
            calls = 0
            def analyze(self, message):
                self.calls += 1
                return super().analyze(message)
        instances.append(Counting())
    return instances


@pytest.fixture(params=["memory", "sqlite"])
def setup(request, tmp_path):
    source = Source()
    identity = lambda account: canonical_identity(source.account_read_model(account))
    activation = InMemoryProjectionActivationRepository(identity)
    stores = create_analytics_stores(request.param, projections_path=tmp_path/'analytics.sqlite3',
        activation=activation, canonical_identity_reader=identity, retention_clock=lambda: NOW)
    analyzers = counters()
    stage = EnrichmentStage(sentiment=analyzers[0], topics_entities=analyzers[1], engagement=analyzers[2])
    pipeline = AnalyticsPipeline(source, projections=stores.projections, enrichment=stage, clock=lambda: NOW)
    yield source, pipeline, analyzers, stores
    close = getattr(stores.projections, "close_retention_scheduler", None)
    if close:
        close()


def counts(analyzers):
    return [analyzer.calls for analyzer in analyzers]


def assert_cold_equal(source, hot, artifact):
    cold = AnalyticsPipeline(source, enrichment=hot.enrichment, clock=hot._retention_clock,
                             reuse_enrichment=False)
    rebuilt = cold._build(ACCOUNT, source.account_read_model(ACCOUNT),
                         projection_generation=artifact.projection.projection_generation)
    assert artifact == rebuilt


def test_unchanged_force_rebuild_reuses_each_analyzer(setup):
    source, pipeline, analyzers, _ = setup
    first = pipeline.project_account(ACCOUNT)
    assert counts(analyzers) == [3, 3, 3]
    second = pipeline.rebuild_account(ACCOUNT)
    assert counts(analyzers) == [3, 3, 3]
    assert first.artifact == second.artifact
    assert_cold_equal(source, pipeline, second.artifact)


@pytest.mark.parametrize("mutation,extra", [("append", 1), ("edit", 1), ("delete", 0),
    ("late", 1), ("metadata", 1), ("revision", 0), ("participant", 3)])
def test_only_changed_inputs_run_again(setup, mutation, extra):
    source, pipeline, analyzers, _ = setup
    pipeline.project_account(ACCOUNT)
    messages = source.conversations["chat"]["messages"]
    if mutation == "append":
        messages.append(source.message(3))
    elif mutation == "edit":
        messages[1]["text"] = "A different synthetic price"
    elif mutation == "delete":
        messages.pop(0)
    elif mutation == "late":
        item = source.message(8)
        item["sent_at"] = messages[0]["sent_at"] - timedelta(minutes=1)
        messages.insert(0, item)
    elif mutation == "metadata":
        messages[1]["direction"] = "outbound"
    elif mutation == "participant":
        source.conversations["chat"]["platform_user_id"] = "different-fan"
    for index, message in enumerate(messages):
        message["source_ordinal"] = index
    source.revision += 1
    result = pipeline.project_account(ACCOUNT)
    assert counts(analyzers) == [3 + extra] * 3
    assert_cold_equal(source, pipeline, result.artifact)


@pytest.mark.parametrize("change", ["revision", "config", "taxonomy", "model"])
def test_analyzer_identity_invalidates_only_its_own_results(setup, change):
    source, pipeline, analyzers, stores = setup
    pipeline.project_account(ACCOUNT)
    if change == "revision":
        analyzers[0].revision += ".changed"
    elif change == "config":
        analyzers[0].config_digest = "sha256:" + "1" * 64
    elif change == "taxonomy":
        analyzers[0].cache_policy = AnalyzerCachePolicy(taxonomy_revision="labels.v2")
    else:
        analyzers[0].cache_policy = AnalyzerCachePolicy(taxonomy_revision="sentiment.labels.v1",
                                                       model_digest="sha256:" + "2" * 64)
    stage = EnrichmentStage(sentiment=analyzers[0], topics_entities=analyzers[1], engagement=analyzers[2])
    changed = AnalyticsPipeline(source, projections=stores.projections, enrichment=stage, clock=lambda: NOW)
    result = changed.project_account(ACCOUNT)
    assert counts(analyzers) == [6, 3, 3]
    assert_cold_equal(source, changed, result.artifact)


def test_no_cross_account_reuse(setup):
    _, pipeline, analyzers, _ = setup
    pipeline.project_account(ACCOUNT)
    pipeline.project_account("synthetic-other")
    assert counts(analyzers) == [6, 6, 6]
    pipeline.rebuild_account(ACCOUNT)
    assert counts(analyzers) == [6, 6, 6]


def test_context_window_changes_invalidate_only_dependent_results(setup):
    source, pipeline, analyzers, stores = setup
    class ContextSentiment(RuleBasedSentimentAnalyzer):
        cache_policy = AnalyzerCachePolicy(taxonomy_revision="sentiment.labels.v1",
            context_revision="previous.v1", preceding_messages=1)
        calls = 0
        def analyze_with_context(self, message, context):
            self.calls += 1
            return super().analyze(message.model_copy(update={"text": " ".join(m.text for m in context) + message.text}))
    contextual = ContextSentiment()
    stage = EnrichmentStage(sentiment=contextual, topics_entities=analyzers[1], engagement=analyzers[2])
    pipeline = AnalyticsPipeline(source, projections=stores.projections, enrichment=stage, clock=lambda: NOW)
    pipeline.project_account(ACCOUNT)
    source.conversations["chat"]["messages"][0]["text"] = "Not good"
    source.revision += 1
    result = pipeline.project_account(ACCOUNT)
    assert contextual.calls == 5
    assert counts(analyzers)[1:] == [4, 4]
    assert_cold_equal(source, pipeline, result.artifact)


def test_cancelled_unpublished_results_are_never_reused(setup):
    source, pipeline, analyzers, _ = setup
    candidate = pipeline.build_candidate(ACCOUNT)
    pipeline.discard_candidate(candidate)
    pipeline.project_account(ACCOUNT)
    assert counts(analyzers) == [6, 6, 6]


def test_accepted_outputs_survive_pipeline_recreation(setup):
    source, pipeline, analyzers, stores = setup
    original = pipeline.project_account(ACCOUNT)
    replacement = AnalyticsPipeline(source, projections=stores.projections,
        enrichment=pipeline.enrichment, clock=lambda: NOW)
    assert replacement.rebuild_account(ACCOUNT).artifact == original.artifact
    assert counts(analyzers) == [3, 3, 3]


def test_missing_opt_in_analyzer_runs_without_reuse(setup):
    source, _, analyzers, stores = setup
    analyzers[0].cache_policy = None
    pipeline = AnalyticsPipeline(source, projections=stores.projections,
        enrichment=EnrichmentStage(sentiment=analyzers[0], topics_entities=analyzers[1], engagement=analyzers[2]),
        clock=lambda: NOW)
    pipeline.project_account(ACCOUNT)
    pipeline.rebuild_account(ACCOUNT)
    assert counts(analyzers) == [6, 3, 3]


def test_fresh_message_cannot_extend_old_context_expiry(setup):
    source, pipeline, _, _ = setup
    oldest = NOW - timedelta(days=89)
    source.conversations["chat"]["messages"][0]["sent_at"] = oldest
    pipeline.project_account(ACCOUNT)
    pipeline._retention_clock = lambda: NOW + timedelta(days=2)
    source.revision += 1
    result = pipeline.rebuild_account(ACCOUNT)
    assert len(result.artifact.projection.message_enrichments) == 2
    assert_cold_equal(source, pipeline, result.artifact)
