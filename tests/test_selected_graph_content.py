"""Indexed content lookups retain exact account and generation selection."""

from collections import Counter
from unittest.mock import Mock

import pytest

from app.analytics.shared_graph import selected_content_ids
from app.persistence import sqlite_api as sqlite3


@pytest.fixture(params=["node", "edge"])
def lookup(request):
    kind = request.param
    key_name = kind + "_id"
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE generation_graph_segments (
        generation_id TEXT, creator_account_id TEXT, kind TEXT,
        bucket TEXT, segment_id TEXT,
        PRIMARY KEY(generation_id,creator_account_id,kind,bucket)
    ) WITHOUT ROWID""")
    db.execute(f"""CREATE TABLE graph_{kind}_content (
        creator_account_id TEXT, content_id TEXT, {key_name} TEXT,
        PRIMARY KEY(creator_account_id,content_id)
    ) WITHOUT ROWID""")
    db.execute(f"CREATE INDEX graph_{kind}_content_by_id "
               f"ON graph_{kind}_content(creator_account_id,{key_name},content_id)")
    db.execute(f"""CREATE TABLE graph_segment_{kind}s (
        creator_account_id TEXT, segment_id TEXT, {key_name} TEXT,
        content_id TEXT, PRIMARY KEY(creator_account_id,segment_id,{key_name})
    ) WITHOUT ROWID""")
    expected = {}
    prefix = "g1:" if kind == "node" else "e1:"
    keys = [prefix + f"{i % 256:02x}" + f"{i:062x}" for i in range(768)]
    for account_index, account in enumerate(("a1:" + "1" * 64, "a1:" + "2" * 64)):
        for version, generation in enumerate(("previous", "current")):
            values = {}
            for index, key in enumerate(keys):
                content = f"{1 + account_index * 10000 + version * 1000 + index:064x}"
                db.execute(f"INSERT INTO graph_{kind}_content VALUES (?,?,?)",
                           (account, content, key))
                # Each generation omits a different set of existing content rows.
                if index % 7 == version:
                    continue
                segment = generation + "-" + key[3:5]
                db.execute("INSERT OR IGNORE INTO generation_graph_segments VALUES (?,?,?,?,?)",
                           (generation, account, kind, key[3:5], segment))
                db.execute(f"INSERT INTO graph_segment_{kind}s VALUES (?,?,?,?)",
                           (account, segment, key, content))
                values[key] = content
            expected[account, generation] = values
    db.commit()
    try:
        yield db, kind, keys, expected
    finally:
        db.close()


@pytest.mark.parametrize("generation", ["previous", "current", "absent"])
def test_lookup_selects_only_requested_memberships(lookup, generation):
    db, kind, keys, expected = lookup
    requested = [*reversed(keys), *keys[:31]]
    requested.append(("g1:" if kind == "node" else "e1:") + "f" * 64)
    for account in ("a1:" + "1" * 64, "a1:" + "2" * 64, "a1:" + "3" * 64):
        assert selected_content_ids(db, generation, account, kind, requested) == (
            expected.get((account, generation), {})
        )


def test_lookup_deduplicates_and_bounds_parameter_batches(lookup):
    db, kind, keys, expected = lookup
    proxy = Mock(wraps=db)
    account = "a1:" + "1" * 64
    assert selected_content_ids(proxy, "current", account, kind, keys * 2) == (
        expected[account, "current"]
    )
    assert proxy.execute.call_count == 3
    assert all(len(call.args[1]) == 259 for call in proxy.execute.call_args_list)


def test_lookup_cancellation_before_sql_and_between_rows(lookup):
    db, kind, keys, _ = lookup
    proxy = Mock(wraps=db)
    account = "a1:" + "1" * 64
    with pytest.raises(RuntimeError, match="cancelled"):
        selected_content_ids(proxy, "current", account, kind, keys,
                             check=Mock(side_effect=RuntimeError("cancelled")))
    proxy.execute.assert_not_called()
    check = Mock(side_effect=[None, None, RuntimeError("cancelled")])
    with pytest.raises(RuntimeError, match="cancelled"):
        selected_content_ids(proxy, "current", account, kind, keys, check=check)
    assert proxy.execute.call_count == 1


def test_lookup_rejects_invalid_kind_without_sql():
    connection = Mock()
    with pytest.raises(ValueError, match="graph_record_kind_invalid"):
        selected_content_ids(connection, "current", "account", "invalid", ["key"])
    connection.execute.assert_not_called()


def test_empty_lookup_does_not_execute_sql():
    connection = Mock()
    assert selected_content_ids(connection, "current", "account", "node", []) == {}
    connection.execute.assert_not_called()


def test_lookup_avoids_searching_every_bucket_for_each_key(lookup):
    db, kind, keys, expected = lookup
    account = "a1:" + "1" * 64
    requested = keys[:256]
    counts = Counter()
    phase = ["indexed"]

    def progress():
        counts[phase[0]] += 100
        return 0

    db.set_progress_handler(progress, 100)
    try:
        actual = selected_content_ids(db, "current", account, kind, requested)
        phase[0] = "manifest"
        marks = ",".join("?" for _ in requested)
        rows = db.execute(f"""SELECT r.{kind}_id,r.content_id
            FROM generation_graph_segments m
            JOIN graph_segment_{kind}s r USING(creator_account_id,segment_id)
            WHERE m.generation_id=? AND m.creator_account_id=? AND m.kind=?
              AND r.{kind}_id IN ({marks})""",
            ("current", account, kind, *requested))
        reference = dict(rows)
    finally:
        db.set_progress_handler(None, 0)
    assert actual == reference == {
        key: expected[account, "current"][key]
        for key in requested if key in expected[account, "current"]
    }
    # Compare engine instructions, not machine-dependent elapsed time.
    assert counts["indexed"] * 4 < counts["manifest"], counts
