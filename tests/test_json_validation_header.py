"""Check streaming JSON syntax validation and the persisted projection header."""

import json
from pathlib import Path
import shutil
import tracemalloc

import pytest
from hypothesis import given, settings, strategies as st

from app.persistence.json_header import json_object_header, json_validation_scope
from app.persistence import sqlite_api
from app.analytics.database import ProjectionsDatabase
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from tests.continuous_analytics_fixture import ACCOUNT, NOW, make_fixture, cleanup

FIELDS = '["message_enrichments","conversation_metrics"]'


@pytest.mark.parametrize("document", [None, 3, "", "[]", "null", "1", "true",
    '{"x":1,}', '{"x":}', '{x:1}', '{"x" 1}', '{} {}', '{"x":NaN}',
    '{"x":Infinity}', '{"x":-Infinity}', '{"x":1,"x":2}',
    '{"message_enrichments":[1,]}', '{"message_enrichments":[,1]}',
    '{"message_enrichments":[1 2]}', '{"message_enrichments":[NaN]}',
    '{"message_enrichments":[{"x":1,"x":2}]}', '{"message_enrichments":null}',
    '{"conversation_metrics":{}}', '{"x":"\\q"}', '{"x":"unterminated}',
    '{"message_enrichments":[{"x":1}]', '{"x":1}garbage'])
def test_invalid_documents_never_produce_a_header(document):
    assert json_object_header(document, FIELDS) is None


_JSON = st.recursive(st.none() | st.booleans() | st.integers(-1000000, 1000000)
    | st.floats(allow_nan=False, allow_infinity=False) | st.text(max_size=24),
    lambda values: st.lists(values, max_size=4) | st.dictionaries(st.text(max_size=12), values, max_size=4),
    max_leaves=16)


@given(st.lists(_JSON, max_size=8), st.lists(_JSON, max_size=5), _JSON)
@settings(max_examples=60, deadline=None)
def test_header_matches_independent_json_decoding(messages, conversations, metadata):
    document = {"message_enrichments": messages, "conversation_metrics": conversations,
                "metadata": metadata, "account_ref": "synthetic", "source_revision": 7}
    text = " \n" + json.dumps(document, ensure_ascii=True, indent=2) + "\t"
    expected = json.loads(text)
    expected.update(message_enrichments=[], conversation_metrics=[])
    assert json.loads(json_object_header(text, FIELDS)) == expected


@pytest.mark.parametrize("text", ['{}', ' { "x": "a\\\\b\\\"[}]" } ',
    '{"message_enrichments":[],"conversation_metrics":[{}]}',
    '{"x":{"n":-1.25e-10},"message_enrichments":["é",true,null]}'])
def test_valid_whitespace_escapes_and_unicode(text):
    expected = json.loads(text)
    for field in json.loads(FIELDS):
        if field in expected:
            expected[field] = []
    assert json.loads(json_object_header(text, FIELDS)) == expected


def test_array_validation_does_not_retain_all_decoded_rows():
    row = json.dumps({"text": "synthetic" * 40, "value": [1, 2, 3]})
    text = '{"message_enrichments":[' + ','.join([row] * 12000) + ']}'
    tracemalloc.start()
    try:
        header = json_object_header(text, FIELDS)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert json.loads(header) == {"message_enrichments": []}
    assert peak < 512 * 1024


def test_cancellation_inside_sql_function_propagates_without_input_text(tmp_path):
    f = make_fixture(tmp_path)
    class Cancelled(RuntimeError):
        pass
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls == 5:
            raise Cancelled("synthetic_cancelled")
    try:
        with f.stores.database.read() as db, pytest.raises(Cancelled, match="synthetic_cancelled"):
            with json_validation_scope(check):
                db.execute('SELECT ofca_json_header_v1(?,?)', ('{"message_enrichments":[1,2,3,4,5]}', FIELDS)).fetchone()
        assert calls == 5
    finally:
        cleanup(f)


def test_stored_header_is_derived_from_the_compact_document(tmp_path):
    f = make_fixture(tmp_path)
    try:
        result = f.pipeline.project_account(ACCOUNT).artifact
        with f.stores.database.read() as db:
            row = db.execute('SELECT document_json,validation_header FROM analytics_projections').fetchone()
            document, header = map(json.loads, row)
            assert result.projection.message_enrichments
            assert document['message_enrichments'] == []
            assert header['message_enrichments'] == header['conversation_metrics'] == []
            assert header['creator_metrics'] == document['creator_metrics']
            assert header['projection_digest'] == result.projection.projection_digest
        with pytest.raises(sqlite_api.DatabaseError):
            with f.stores.database.transaction() as db:
                db.execute("UPDATE analytics_projections SET validation_header='{}'")
        assert f.stores.projections.get_artifact(ACCOUNT) == result
    finally:
        cleanup(f)


def test_missing_json_function_cannot_validate_a_document(tmp_path):
    f = make_fixture(tmp_path)
    try:
        with f.stores.database.read() as db:
            db.create_function('ofca_json_header_v1', 2, None)
            with pytest.raises(sqlite_api.DatabaseError):
                db.execute('SELECT ofca_json_header_v1(?,?)', ('{}', FIELDS)).fetchone()
    finally:
        cleanup(f)


def test_populated_header_upgrade_keeps_bytes_backups_and_foreign_keys(tmp_path):
    f = make_fixture(tmp_path / 'canonical')
    catalog = tmp_path / 'catalog'
    catalog.mkdir()
    for path in (Path(__file__).parents[1] / 'app/analytics/sql').glob('*.sql'):
        if int(path.name[:4]) <= 8:
            shutil.copy2(path, catalog / path.name)
    legacy = ProjectionsDatabase(tmp_path / 'projection.sqlite3', migrations_dir=catalog)
    store = SQLiteAnalyticsProjectionStore(legacy, activation=f.repositories.projection_activation,
        canonical_identity_reader=f.source.read_identity)
    try:
        original = AnalyticsPipeline(f.source, projections=store, clock=lambda: NOW).project_account(ACCOUNT).artifact
        with legacy.read() as db:
            before = db.execute('SELECT document_json FROM analytics_projections').fetchone()[0]
        updated = ProjectionsDatabase(legacy.path)
        with updated.open_detached(updated.migration_runner.last_backup_path) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 8
            assert db.execute('SELECT document_json FROM analytics_projections').fetchone()[0] == before
        with updated.read() as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 20
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            assert db.execute('SELECT document_json FROM analytics_projections').fetchone()[0] == before
        reopened = SQLiteAnalyticsProjectionStore(updated, activation=f.repositories.projection_activation,
            canonical_identity_reader=f.source.read_identity)
        assert reopened.get_artifact(ACCOUNT) == original
    finally:
        cleanup(f)
