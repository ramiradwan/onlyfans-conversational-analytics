"""Append reuse keeps verified serialized prefix data without importing it again."""
from datetime import timedelta
from unittest.mock import Mock

from app.analytics.enrichment_cache import EnrichmentReuse
from app.analytics.opaque_refs import message_ref, account_ref, conversation_ref
from app.analytics.conversation_enrichment_unit_sql import load_unit
from app.analytics.conversation_enrichment_units import analyzer_records, message_records
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message
from tests.test_dominant_append_reuse import dominant_fixture

import pytest

pytestmark = [pytest.mark.ci_tier('integration')]


def active_unit(f):
    with f.stores.database.read() as db:
        generation = db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0]
        return load_unit(db, generation, account_ref(ACCOUNT), conversation_ref(ACCOUNT, 'chat-0'))


def test_append_stages_only_new_analyzer_records(tmp_path, monkeypatch):
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        previous = active_unit(f)
        original, retained = EnrichmentReuse.retain_record, []
        def observe(self, entry):
            retained.append(entry.key.message_ref)
            return original(self, entry)
        monkeypatch.setattr(EnrichmentReuse, 'retain_record', observe)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'new-tail', NOW, 2)
            advance(db)
        result = f.pipeline.project_account(ACCOUNT)
        assert retained == [message_ref(ACCOUNT, 'chat-0', 'new-tail')] * 3
        current = active_unit(f)
        assert message_records(current)[:-1] == message_records(previous)
        assert set(analyzer_records(previous)) <= set(analyzer_records(current))
        cold_equal(f, result.artifact)
    finally:
        cleanup(f)


def test_new_tail_does_not_search_predecessor_analyzer_cache(tmp_path, monkeypatch):
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        fallback = Mock(side_effect=AssertionError('new message is absent from the verified prefix'))
        monkeypatch.setattr(f.stores.projections, 'load_conversation_enrichment_entries', fallback)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'new-tail', NOW, 2)
            advance(db)
        result = f.pipeline.project_account(ACCOUNT)
        fallback.assert_not_called()
        cold_equal(f, result.artifact)
    finally:
        cleanup(f)


def test_predecessor_lookup_scope_is_local_and_restored_after_failure():
    from types import SimpleNamespace
    import pytest
    store = SimpleNamespace(load_enrichment_entries=Mock(return_value={}),
        load_conversation_enrichment_entries=Mock(return_value={}))
    reuse = EnrichmentReuse(store, ACCOUNT, lambda: NOW)
    new = SimpleNamespace(digest='new-key', message_ref='new-message', conversation_ref='conversation')
    old = SimpleNamespace(digest='old-key', message_ref='old-message', conversation_ref='conversation')
    with pytest.raises(RuntimeError, match='cancelled build'):
        with reuse.known_new_message(new.message_ref):
            reuse.prefetch([new])
            store.load_conversation_enrichment_entries.assert_not_called()
            reuse.prefetch([old])
            store.load_conversation_enrichment_entries.assert_called_once()
            raise RuntimeError('cancelled build')
    assert reuse._known_new_message is None


def test_append_without_exact_prefix_keeps_full_changed_unit_validation(tmp_path, monkeypatch):
    from app.analytics import conversation_enrichment_unit_sql as sql
    original, counts = sql._validate_unit, []
    def observe(unit, **kwargs):
        counts.append(unit.header.message_count)
        return original(unit, **kwargs)
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        monkeypatch.setattr(sql, '_validate_unit', observe)
        monkeypatch.setattr(sql, '_validate_appended_unit', lambda *args, **kwargs: False)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'new-tail', NOW, 2)
            advance(db)
        result = f.pipeline.project_account(ACCOUNT)
        assert 1025 in counts
        cold_equal(f, result.artifact)
    finally:
        cleanup(f)


def test_corrupt_predecessor_analyzer_payload_is_not_copied(tmp_path, monkeypatch):
    import pytest
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        original = f.stores.projections.load_enrichment_unit_contents
        def corrupted(*args):
            return {key: (value[0], b'not a compressed analyzer payload')
                    for key, value in original(*args).items()}
        monkeypatch.setattr(f.stores.projections, 'load_enrichment_unit_contents', corrupted)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'new-tail', NOW, 2)
            advance(db)
        with pytest.raises(ValueError, match='conversation_enrichment_unit_encoding_invalid'):
            f.pipeline.project_account(ACCOUNT)
    finally:
        cleanup(f)
