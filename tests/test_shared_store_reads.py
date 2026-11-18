import asyncio
import threading
from datetime import datetime, timezone

import pytest

from app.persistence.auth import SQLiteAuthenticationStore

pytestmark = [pytest.mark.ci_tier("fast")]


def counted(tmp_path):
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    database, connect, opened = store.database, store.database.connect, []

    def counting():
        opened.append(1)
        return connect()

    database.connect = counting
    return database, opened


@pytest.mark.asyncio
async def test_reads_in_a_shared_block_reuse_one_connection_and_see_committed_writes(tmp_path):
    database, opened = counted(tmp_path)
    with database.shared_reads():
        with database.read() as first, database.read() as second:
            assert first is second
        with database.shared_reads(), database.read() as nested:
            assert nested is first
        with database.transaction() as connection:
            assert connection is not first
            connection.execute("CREATE TABLE shared_probe (value INTEGER)")
            connection.execute("INSERT INTO shared_probe VALUES (1)")
        with database.read() as current:
            assert current.execute("SELECT value FROM shared_probe").fetchone()[0] == 1
    assert len(opened) == 2
    with database.read() as after:
        assert after is not first
    assert len(opened) == 3


@pytest.mark.asyncio
async def test_threads_and_tasks_started_in_a_shared_block_open_their_own_connections(tmp_path):
    database, opened = counted(tmp_path)
    seen = {}

    def threaded():
        with database.read() as connection:
            seen["thread"] = connection

    async def later():
        await asyncio.sleep(0)
        with database.read() as connection:
            seen["task"] = connection

    with database.shared_reads():
        with database.read() as shared:
            pass
        thread = threading.Thread(target=threaded)
        thread.start()
        thread.join()
        task = asyncio.create_task(later())
    await task
    assert seen["thread"] is not shared and seen["task"] is not shared
    assert len(opened) == 3
