"""Reuse exact immutable membership pages within a graph segment."""

from dataclasses import dataclass
import hashlib
from itertools import groupby


@dataclass(frozen=True, slots=True)
class MembershipPage:
    page_id: str
    reused: bool
    count: int


def supported(connection):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_membership_pages'"
    ).fetchone() is not None


def prepare_pages(connection, account, segment_id, kind, records, keys,
                  previous_segment_id=None, *, check=lambda: None):
    """Select old pages only after comparing their complete stored membership."""
    if kind not in ('node', 'edge'):
        raise ValueError('graph_record_kind_invalid')
    check()
    previous = {}
    if previous_segment_id is not None:
        previous = {row['bucket']: row['page_id'] for row in connection.execute(
            """SELECT r.bucket,r.page_id FROM graph_segment_membership_pages r
               JOIN graph_membership_pages p USING(creator_account_id,page_id)
               WHERE r.creator_account_id=? AND r.segment_id=? AND r.kind=?
                 AND p.kind=r.kind AND p.bucket=r.bucket AND p.sealed=1""",
            (account, previous_segment_id, kind))}
    pages = {}
    for prefix, grouped in groupby(sorted(keys), key=lambda key: key[3:6]):
        check()
        page_id = previous.get(prefix)
        desired = []
        count = 0
        for key in grouped:
            check()
            count += 1
            if page_id is not None:
                desired.append((key, hashlib.sha256(records[key].encode('utf-8')).hexdigest()))
        reused = False
        if page_id is not None:
            rows = connection.execute(
                f"SELECT {kind}_id,content_id FROM graph_membership_{kind}s "
                f"WHERE creator_account_id=? AND page_id=? ORDER BY {kind}_id",
                (account, page_id))
            try:
                actual = []
                for row in rows:
                    check()
                    actual.append(tuple(row))
                reused = actual == desired
            finally:
                rows.close()
        check()
        if not reused:
            # Keep new page keys in their creating segment's range.
            page_id = segment_id + ':' + prefix[-1]
            connection.execute(
                "INSERT INTO graph_membership_pages(creator_account_id,page_id,kind,bucket) "
                "VALUES (?,?,?,?)", (account, page_id, kind, prefix))
        connection.execute(
            """INSERT INTO graph_segment_membership_pages
               (creator_account_id,segment_id,kind,bucket,page_id) VALUES (?,?,?,?,?)""",
            (account, segment_id, kind, prefix, page_id))
        pages[prefix] = MembershipPage(page_id, reused, count)
    return pages


def write_members(connection, kind, members, page_plans):
    """Insert new physical memberships; selected old pages remain untouched."""
    if kind not in ('node', 'edge'):
        raise ValueError('graph_record_kind_invalid')
    if page_plans is None:
        connection.executemany(
            f"INSERT INTO graph_segment_{kind}s"
            f"(creator_account_id,segment_id,{kind}_id,content_id) VALUES (?,?,?,?)",
            members)
        return len(members)
    values = []
    for account, segment, key, content in members:
        page = page_plans[(kind, segment)][key[3:6]]
        if not page.reused:
            values.append((account, page.page_id, key, content))
    if values:
        connection.executemany(
            f"INSERT INTO graph_membership_{kind}s"
            f"(creator_account_id,page_id,{kind}_id,content_id) VALUES (?,?,?,?)",
            values)
    return len(values)
