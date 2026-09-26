"""Membership metadata fits within the existing local writer-cache ceiling."""

import pytest
from sqlcipher3 import dbapi2 as sqlite3

from app.analytics.database import (
    MAX_CONTENT_WRITE_CACHE_KIB, content_write_cache, content_write_cache_target,
)


def test_metadata_allowance_preserves_existing_maximum_and_default():
    assert MAX_CONTENT_WRITE_CACHE_KIB == 128 * 1024
    assert content_write_cache_target(100000) == 50000
    assert content_write_cache_target(100000, membership_page_count=8192) == 58192
    assert content_write_cache_target(1000000, membership_page_count=8192) == 128 * 1024
    assert content_write_cache_target(0, membership_page_count=0) == 16 * 1024


@pytest.mark.parametrize('value', [-1, True, 0.5, None, 100001])
def test_invalid_page_counts_are_rejected(value):
    with pytest.raises(ValueError, match='graph_membership_page_count_invalid'):
        content_write_cache_target(100000, membership_page_count=value)


@pytest.mark.parametrize('fail', [False, True])
def test_metadata_allowance_is_restored_on_every_exit(fail):
    db = sqlite3.connect(':memory:')
    db.execute('PRAGMA cache_size=-4096')
    try:
        with content_write_cache(db, 100000):
            assert db.execute('PRAGMA cache_size').fetchone()[0] == -50000
            try:
                with content_write_cache(db, 100000, membership_page_count=8192):
                    assert db.execute('PRAGMA cache_size').fetchone()[0] == -58192
                    if fail:
                        raise RuntimeError('injected cancellation')
            except RuntimeError:
                assert fail
            assert db.execute('PRAGMA cache_size').fetchone()[0] == -50000
        assert db.execute('PRAGMA cache_size').fetchone()[0] == -4096
    finally:
        db.close()
