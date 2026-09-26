"""Check backup integrity for reused graph content."""

import pytest

from app.persistence.backup import backup_projections_database, verify_backup, SQLiteBackupError
from tests.continuous_analytics_fixture import ACCOUNT
from tests.test_shared_graph import fixture
from tests.test_sqlite_backup import open_encrypted_backup, refresh_external_hash


@pytest.mark.parametrize('kind,property_name,value', [
    ('node', 'character_count', 999),
    ('edge', 'interval_seconds', 999.0),
])
def test_shared_backup_rejects_valid_property_tamper(fixture, tmp_path, kind, property_name, value):
    fixture.pipeline.project_account(ACCOUNT)
    backup = tmp_path / 'shared.backup.sqlite3'
    backup_projections_database(fixture.stores.database, backup)
    connection = open_encrypted_backup(backup, 'projections')
    try:
        table = 'graph_' + kind + '_content'
        connection.execute('DROP TRIGGER ' + table + '_immutable')
        predicate = "kind='message'" if kind == 'node' else "relation='precedes'"
        row = connection.execute(f'SELECT content_id FROM {table} WHERE {predicate} LIMIT 1').fetchone()
        assert row is not None
        cursor = connection.execute(f'UPDATE {table} SET properties_json=json_set(properties_json,?,?) WHERE content_id=?',
            ('$.' + property_name, value, row[0]))
        assert cursor.rowcount == 1
        connection.commit()
    finally:
        connection.close()
    refresh_external_hash(backup)
    with pytest.raises(SQLiteBackupError, match='rows do not match digests'):
        verify_backup(backup, expected_store='projections')
