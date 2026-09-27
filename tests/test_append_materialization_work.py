"""Count old-message reconstruction across the complete append and staging path."""
from collections import Counter
from unittest.mock import Mock

import pytest

from app.models.analytics import MessageEnrichment
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message
from tests.test_dominant_append_reuse import dominant_fixture


@pytest.mark.parametrize('state', ['ordinary', 'rebuilt', 'restarted'])
def test_append_build_and_staging_materialize_only_boundary_messages(tmp_path, monkeypatch, record_property, state):
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        if state == 'restarted':
            from app.analytics.canonical_source import HistoryAnalyticsSource
            from app.analytics.factory import create_analytics_stores
            from app.analytics.pipeline import AnalyticsPipeline
            cleanup(f)
            f.source = HistoryAnalyticsSource(f.repositories.history)
            f.stores = create_analytics_stores('sqlite', projections_path=tmp_path/'analytics.sqlite3',
                activation=f.repositories.projection_activation,
                canonical_identity_reader=f.source.read_identity, retention_clock=lambda: NOW)
            f.pipeline = AnalyticsPipeline(f.source, projections=f.stores.projections,
                enrichment=f.pipeline.enrichment, clock=lambda: NOW)
            assert f.pipeline.prepare_questions(ACCOUNT, 1)
        if state == 'rebuilt':
            f.pipeline.project_account(ACCOUNT, force=True)
        parsed = Counter()
        original = MessageEnrichment.model_validate_json
        def observe(cls, data, *args, **kwargs):
            result = original(data, *args, **kwargs)
            parsed[result.message_ref] += 1
            return result
        monkeypatch.setattr(MessageEnrichment, 'model_validate_json', classmethod(observe))
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'one-new-tail', NOW, 2)
            advance(db)
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        from app.analytics.opaque_refs import message_ref
        old = {message_ref(ACCOUNT, 'chat-0', f'dominant-{i}') for i in range(1024)}
        counts = {ref: number for ref, number in parsed.items() if ref in old}
        record_property('old_model_builds', sum(counts.values()))
        record_property('distinct_old_messages', len(counts))
        assert sum(counts.values()) <= 2, dict(total=sum(counts.values()), distinct=len(counts))
        monkeypatch.setattr(MessageEnrichment, 'model_validate_json', original)
        cold_equal(f, candidate.artifact())
    finally:
        cleanup(f)


def test_staging_selects_only_cache_entry_sources(tmp_path, monkeypatch):
    from app.analytics.enrichment_cache import storage_entries
    from app.analytics.conversation_enrichment_units import IncrementalMessageEnrichments
    from tests.test_append_enrichment_copying import active_unit
    from types import SimpleNamespace
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        unit = active_unit(f)
        from app.analytics.conversation_enrichment_units import analyzer_records
        entries = analyzer_records(unit)[:3]
        values = IncrementalMessageEnrichments(unit.header.account_ref, (unit,), f.stores.projections)
        parse = Mock(wraps=MessageEnrichment.model_validate_json)
        monkeypatch.setattr(MessageEnrichment, 'model_validate_json', parse)
        records = list(storage_entries(SimpleNamespace(projection=SimpleNamespace(message_enrichments=values)), entries))
        assert len(records) == 3
        assert parse.call_count <= 3, parse.call_count
    finally:
        cleanup(f)


def test_selected_cache_validation_uses_actual_source_expiry(tmp_path):
    from dataclasses import replace
    from datetime import timedelta
    from app.analytics.conversation_enrichment_units import IncrementalMessageEnrichments, message_records
    from tests.test_append_enrichment_copying import active_unit
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        original = active_unit(f)
        tail = MessageEnrichment.model_validate_json(message_records(original)[-1])
        changed = replace(original, header=replace(original.header,
            first_source_at=NOW + timedelta(days=10)))
        values = IncrementalMessageEnrichments(original.header.account_ref, (changed,), f.stores.projections)
        selected, first = values.validation_messages_for({tail.message_ref})
        assert selected == {tail.message_ref: tail}
        assert first == original.header.first_source_at
        def stop():
            raise RuntimeError('cancelled source selection')
        with pytest.raises(RuntimeError, match='cancelled source selection'):
            values.validation_messages_for({tail.message_ref}, check=stop)
    finally:
        cleanup(f)


def test_unselected_corrupt_prefix_still_requires_full_validation(tmp_path, monkeypatch):
    import hashlib, json, zlib
    from dataclasses import replace
    from app.analytics import conversation_enrichment_units as units
    from app.analytics import conversation_enrichment_unit_sql as storage
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        original = units.append_enrichment_unit
        def corrupt(*args, **kwargs):
            unit = original(*args, **kwargs)
            rows = list(units.message_records(unit))
            item = json.loads(rows[0])
            item['sentiment']['score'] = 'not a numeric score'
            rows[0] = units._canonical(item)
            raw = b'\n'.join(rows)
            digest = hashlib.sha256(raw).hexdigest()
            metrics = hashlib.sha256(units._canonical(unit.header.metrics.model_dump(mode='json'))).hexdigest()
            header = replace(unit.header, canonical_digest=digest,
                unit_id=units._unit_id(digest, unit.header.analyzer_digest, metrics, len(rows)))
            return replace(unit, header=header, messages=zlib.compress(raw))
        monkeypatch.setattr(units, 'append_enrichment_unit', corrupt)
        validate = Mock(wraps=storage._validate_unit)
        monkeypatch.setattr(storage, '_validate_unit', validate)
        with f.stores.database.read() as db:
            previous = db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0]
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'one-new-tail', NOW, 2)
            advance(db)
        with pytest.raises(ValueError):
            f.pipeline.project_account(ACCOUNT)
        assert any(call.args[0].header.message_count == 1025 for call in validate.call_args_list)
        with f.stores.database.read() as db:
            assert db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0] == previous
    finally:
        cleanup(f)


def test_cache_records_are_validated_once_as_they_are_stored(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.analytics.enrichment_cache import CachedEnrichment, storage_entries
    from app.analytics.conversation_enrichment_units import IncrementalMessageEnrichments, analyzer_records
    from tests.test_append_enrichment_copying import active_unit
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        unit = active_unit(f)
        entries = analyzer_records(unit)[:9]
        values = IncrementalMessageEnrichments(unit.header.account_ref, (unit,), f.stores.projections)
        artifact = SimpleNamespace(projection=SimpleNamespace(message_enrichments=values))
        parse = Mock(wraps=CachedEnrichment.model_validate_json)
        monkeypatch.setattr(CachedEnrichment, 'model_validate_json', parse)
        records = storage_entries(artifact, entries)
        next(records)
        assert parse.call_count == 1, parse.call_count
        assert len(list(records)) == len(entries) - 1
        assert parse.call_count == len(entries)
    finally:
        cleanup(f)
