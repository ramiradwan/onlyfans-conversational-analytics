"""Reject undeclared inputs and preserve limits and analysis admission."""

import pytest
from pydantic import ValidationError

from app.analytics.enrichment import EnrichmentStage
from app.analytics.enrichment_cache import AnalyzerCachePolicy
from app.analytics.errors import AnalyzerConfigurationInvalid
from app.analytics.pipeline import AnalyticsPipeline
from app.security.runtime_policy import RuntimeAuthorizationDenied
from tests.test_enrichment_reuse import setup, counts, NOW, ACCOUNT, assert_cold_equal


@pytest.mark.parametrize("fields", [
    {"preceding_messages": -1}, {"preceding_messages": 33}, {"following_messages": True},
    {"model_digest": "unversioned"}, {"context_revision": ""}, {"unknown": "value"},
])
def test_input_policy_is_closed_and_bounded(fields):
    with pytest.raises(ValidationError):
        AnalyzerCachePolicy(**{"taxonomy_revision": "test.v1", **fields})


def test_context_requires_an_explicit_analyzer_interface(setup):
    _, _, analyzers, _ = setup
    analyzers[0].cache_policy = AnalyzerCachePolicy(taxonomy_revision="test.v1", preceding_messages=1)
    with pytest.raises(AnalyzerConfigurationInvalid):
        EnrichmentStage(sentiment=analyzers[0])


def test_runtime_input_policy_drift_is_rejected(setup):
    _, pipeline, analyzers, _ = setup
    pipeline.project_account(ACCOUNT)
    analyzers[0].cache_policy = AnalyzerCachePolicy(taxonomy_revision="changed.v2")
    with pytest.raises(AnalyzerConfigurationInvalid):
        pipeline.rebuild_account(ACCOUNT)


@pytest.mark.parametrize("limit,value,expected_calls", [("MAX_CACHE_ENTRIES", 2, 16), ("MAX_CACHE_BYTES", 1, 18)])
def test_small_cache_budget_falls_back_without_changing_answers(setup, monkeypatch, limit, value, expected_calls):
    import app.analytics.enrichment_cache as cache
    _, pipeline, analyzers, _ = setup
    monkeypatch.setattr(cache, limit, value)
    first = pipeline.project_account(ACCOUNT)
    second = pipeline.rebuild_account(ACCOUNT)
    assert first.artifact == second.artifact
    assert sum(counts(analyzers)) == expected_calls


def test_warm_cache_does_not_bypass_analysis_admission(setup, monkeypatch):
    from app.analytics import licensed_pipeline
    source, pipeline, analyzers, stores = setup
    pipeline.project_account(ACCOUNT)
    def denied(*args):
        raise RuntimeAuthorizationDenied("Analysis is not authorized")
    monkeypatch.setattr(licensed_pipeline, "_auth_store", lambda _: object())
    monkeypatch.setattr(licensed_pipeline, "require_cached_analysis_run", denied)
    protected = licensed_pipeline.LicensedAnalyticsPipeline(source, projections=stores.projections,
        enrichment=pipeline.enrichment, clock=lambda: NOW)
    with pytest.raises(RuntimeAuthorizationDenied):
        protected.rebuild_account(ACCOUNT)
    assert counts(analyzers) == [3, 3, 3]


def test_failed_analyzer_does_not_leave_reusable_partial_output(setup, monkeypatch):
    _, pipeline, analyzers, _ = setup
    original = analyzers[1].analyze
    def failed(message):
        raise RuntimeError("synthetic analyzer failure")
    monkeypatch.setattr(analyzers[1], "analyze", failed)
    with pytest.raises(RuntimeError):
        pipeline.project_account(ACCOUNT)
    assert counts(analyzers) == [1, 0, 0]
    monkeypatch.setattr(analyzers[1], "analyze", original)
    pipeline.project_account(ACCOUNT)
    assert counts(analyzers) == [4, 3, 3]


def test_one_new_message_does_not_invalidate_a_hundred_unchanged_messages(setup):
    source, pipeline, analyzers, _ = setup
    source.conversations["chat"]["messages"] = [source.message(i) for i in range(100)]
    pipeline.project_account(ACCOUNT)
    assert counts(analyzers) == [100, 100, 100]
    source.conversations["chat"]["messages"].append(source.message(100))
    source.revision += 1
    result = pipeline.project_account(ACCOUNT)
    assert counts(analyzers) == [101, 101, 101]
    assert_cold_equal(source, pipeline, result.artifact)


def test_rebuild_reuses_an_accepted_model_sample(setup):
    from app.analytics.analyzers import RuleBasedSentimentAnalyzer
    from app.models.analytics import AnalysisMode
    source, _, _, stores = setup
    class Sampled(RuleBasedSentimentAnalyzer):
        mode = AnalysisMode.MODEL
        cache_policy = AnalyzerCachePolicy(taxonomy_revision="labels.v1", model_digest="sha256:" + "3" * 64)
        calls = 0
        def analyze(self, message):
            self.calls += 1
            return super().analyze(message).model_copy(update={"score": self.calls / 10})
    analyzer = Sampled()
    pipeline = AnalyticsPipeline(source, projections=stores.projections,
        enrichment=EnrichmentStage(sentiment=analyzer), clock=lambda: NOW)
    first = pipeline.project_account(ACCOUNT)
    second = pipeline.rebuild_account(ACCOUNT)
    assert analyzer.calls == 3
    assert first.artifact == second.artifact


def test_model_reuse_requires_a_model_artifact(setup):
    from app.models.analytics import AnalysisMode
    _, _, analyzers, _ = setup
    analyzers[0].mode = AnalysisMode.MODEL
    with pytest.raises(AnalyzerConfigurationInvalid):
        EnrichmentStage(sentiment=analyzers[0])
