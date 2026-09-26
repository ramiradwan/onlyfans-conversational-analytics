"""Share bounded conversation output regardless of message count."""

from contextlib import contextmanager
from datetime import timedelta

import pytest

from app.analytics.compact_graph import CompactGraph
from app.analytics.conversation_pages import ConversationPageReference
from app.analytics.opaque_refs import conversation_ref
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message, make_fixture,
)
from tests.test_shared_conversation_pages import counts


@pytest.fixture
def fixture(tmp_path):
    value = make_fixture(tmp_path)
    yield value
    cleanup(value)


def fragment_count(fixture):
    with fixture.stores.database.read() as db:
        return db.execute('SELECT COUNT(*) FROM conversation_fragments').fetchone()[0]


@pytest.mark.parametrize('messages', [1, 3, 256, 257])
def test_compact_build_uses_pages_without_materializing_fragments(tmp_path, monkeypatch, messages):
    value = make_fixture(tmp_path, conversations=1, messages=messages)
    try:
        def forbidden(_):
            raise AssertionError('compact build materialized graph models')
        monkeypatch.setattr(CompactGraph, 'materialize', forbidden)
        candidate = value.pipeline.build_candidate(ACCOUNT)
        result = value.pipeline.publish_candidate(candidate)
        assert fragment_count(value) == 0
        assert counts(value)['conversation_page_sets'] == 1
        assert counts(value)['conversation_owned_pages'] == 0
        assert result.artifact.projection.creator_metrics.message_count == messages
        cold_equal(value, result.artifact)
    finally:
        cleanup(value)


def test_unchanged_small_conversations_stage_references_without_copying_payloads(fixture, monkeypatch):
    fixture.pipeline.project_account(ACCOUNT)
    before = counts(fixture)
    fixture.source.loaded.clear()
    observed = []
    stage = fixture.stores.projections.stage_built_artifact
    def checked(artifact, **options):
        observed.extend(options['conversation_pages'])
        return stage(artifact, **options)
    monkeypatch.setattr(fixture.stores.projections, 'stage_built_artifact', checked)
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    assert len(observed) == 3
    assert all(isinstance(item, ConversationPageReference) for item in observed)
    assert counts(fixture)['conversation_page_content'] == before['conversation_page_content']
    assert counts(fixture)['conversation_page_refs'] == before['conversation_page_refs'] * 2
    assert fragment_count(fixture) == 0
    assert fixture.source.loaded == []
    assert [analyzer.calls for analyzer in fixture.analyzers] == [9, 9, 9]
    result = fixture.pipeline.publish_candidate(candidate)
    assert counts(fixture) == before
    cold_equal(fixture, result.artifact)


@pytest.mark.parametrize('legacy_mode', ['models', 'unavailable_pages'])
def test_valid_fragments_convert_without_source_reads_or_analysis(fixture, monkeypatch, legacy_mode):
    fixture.stores.projections.reuse_conversation_enrichment_units = False
    if legacy_mode == 'models':
        fixture.pipeline.compact_graph = False
        fixture.pipeline.project_account(ACCOUNT)
        fixture.pipeline.compact_graph = True
    else:
        opener = fixture.stores.projections.open_conversation_fragments
        @contextmanager
        def fragments_only(account):
            with opener(account) as reader:
                del reader.pages
                yield reader
        with monkeypatch.context() as patch:
            patch.setattr(fixture.stores.projections, 'open_conversation_fragments', fragments_only)
            fixture.pipeline.project_account(ACCOUNT)
    assert fragment_count(fixture) == 3
    assert counts(fixture)['conversation_page_sets'] == 0
    fixture.source.loaded.clear()
    result = fixture.pipeline.rebuild_account(ACCOUNT)
    assert fixture.source.loaded == []
    assert [analyzer.calls for analyzer in fixture.analyzers] == [9, 9, 9]
    assert fragment_count(fixture) == 0
    assert counts(fixture)['conversation_page_sets'] == 3
    cold_equal(fixture, result.artifact)
    fixture.source.loaded.clear()
    cold_equal(fixture, fixture.pipeline.rebuild_account(ACCOUNT).artifact)
    assert fixture.source.loaded == []


def test_only_the_changed_small_conversation_creates_new_pages(fixture, monkeypatch):
    fixture.pipeline.project_account(ACCOUNT)
    fixture.source.loaded.clear()
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'added', NOW, 4)
        advance(db)
    observed = []
    stage = fixture.stores.projections.stage_built_artifact
    def checked(artifact, **options):
        observed.extend(options['conversation_pages'])
        return stage(artifact, **options)
    monkeypatch.setattr(fixture.stores.projections, 'stage_built_artifact', checked)
    result = fixture.pipeline.project_account(ACCOUNT)
    fresh = [item for item in observed if not isinstance(item, ConversationPageReference)]
    assert len(fresh) == 1
    assert fresh[0].header.conversation_ref == conversation_ref(ACCOUNT, 'chat-1')
    assert fixture.source.loaded == ['chat-1']
    assert [analyzer.calls for analyzer in fixture.analyzers] == [10, 10, 10]
    cold_equal(fixture, result.artifact)


@pytest.mark.parametrize('limit,expected', [('MAX_FRAGMENT_TOTAL_BYTES', 0), ('MAX_FRAGMENTS', 1)])
def test_small_pages_respect_shared_limits_without_changing_answers(fixture, monkeypatch, limit, expected):
    import app.analytics.conversation_reuse as reuse
    monkeypatch.setattr(reuse, limit, 1)
    result = fixture.pipeline.project_account(ACCOUNT)
    assert counts(fixture)['conversation_page_sets'] == expected
    assert fragment_count(fixture) == 0
    cold_equal(fixture, result.artifact)
    rebuilt = fixture.pipeline.rebuild_account(ACCOUNT)
    assert counts(fixture)['conversation_page_sets'] == expected
    cold_equal(fixture, rebuilt.artifact)


def test_discard_and_source_expiry_reclaim_small_pages(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    before = counts(fixture)
    candidate = fixture.pipeline.build_candidate(ACCOUNT, force=True)
    fixture.pipeline.discard_candidate(candidate)
    assert counts(fixture) == before
    fixture.clock.now += timedelta(days=91)
    assert fixture.stores.projections.enforce_retention(ACCOUNT)
    assert not any(counts(fixture).values())


def test_fragment_conversion_preserves_the_original_source_window(fixture):
    from app.analytics.conversation_pages import ConversationPageHeader
    from app.analytics.conversation_reuse import ConversationFragment
    fixture.pipeline.compact_graph = False
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        previous = [ConversationFragment.model_validate_json(row[0])
                    for row in db.execute('SELECT document_json FROM conversation_fragments')]
    expected = {item.conversation_ref: (item.retention_cutoff, item.expires_at) for item in previous}
    fixture.clock.now += timedelta(days=1)
    fixture.pipeline.compact_graph = True
    fixture.source.loaded.clear()
    result = fixture.pipeline.rebuild_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        headers = [ConversationPageHeader.model_validate_json(row[0])
                   for row in db.execute('SELECT header_json FROM conversation_page_sets')]
    assert len(headers) == 3
    assert {item.conversation_ref: (item.retention_cutoff, item.expires_at) for item in headers} == expected
    assert fixture.source.loaded == []
    assert [analyzer.calls for analyzer in fixture.analyzers] == [9, 9, 9]
    cold_equal(fixture, result.artifact)
