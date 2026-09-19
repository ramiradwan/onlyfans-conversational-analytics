"""Verify the canonical metadata migration preserves source data and restart identity."""

import shutil
from pathlib import Path
from types import SimpleNamespace

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import MigrationRunner
from tests.continuous_analytics_fixture import ACCOUNT, NOW, insert_message


def test_populated_canonical_upgrade_preserves_exact_source_identity(tmp_path):
    catalog = tmp_path/'catalog'
    catalog.mkdir()
    for path in (Path(__file__).parents[1]/'app/persistence/sql').glob('*.sql'):
        if int(path.name[:4]) <= 8:
            shutil.copy2(path, catalog/path.name)
    database = CanonicalSQLite(tmp_path/'canonical.sqlite3')
    MigrationRunner(database, migrations_dir=catalog).run()
    with database.transaction() as db:
        db.execute('INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES (?,?,?)',
            (ACCOUNT, 1, NOW.isoformat()))
        db.execute('''INSERT INTO account_chats(creator_account_id,chat_id,record_kind,platform_user_id,
            content_hash,winning_stream_epoch,winning_source_seq,is_deleted,updated_at)
            VALUES (?,'chat','full','fan','hash',1,1,0,?)''', (ACCOUNT,NOW.isoformat()))
        insert_message(db, 'chat', 'message', NOW)
    source = HistoryAnalyticsSource(SimpleNamespace(database=database))
    expected = source.read_identity(ACCOUNT)
    with database.read() as db:
        assert source._identity_cache.token(db, ACCOUNT) is None
    runner = MigrationRunner(database, migrations_dir=Path(__file__).parents[1]/'app/persistence/sql')
    runner.run()
    assert runner.last_backup_path is not None
    with database.open_detached(runner.last_backup_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 8
        assert db.execute('SELECT text FROM account_messages').fetchone()[0] == 'Thanks pricing'
    with database.read() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 9
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert source._identity_cache.token(db, ACCOUNT) is not None
    assert source.read_identity(ACCOUNT) == expected
    reopened = HistoryAnalyticsSource(SimpleNamespace(database=CanonicalSQLite(database.path)))
    assert reopened.read_identity(ACCOUNT) == expected
