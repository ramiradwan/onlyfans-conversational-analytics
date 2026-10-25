"""Retirement fences for canonical-origin findings, including retained snapshots."""


class OriginCheckerUnavailable(ValueError):
    """The canonical owner is unavailable; stored data is not corrupt."""


def origin_live(db, account_ref, record_digest):
    if db.execute('PRAGMA user_version').fetchone()[0] < 34:
        return True
    rows = db.execute('SELECT nlp_origin_live(canonical_store_id,contribution_id,manifest_sha256,account_ref,record_digest) '
                      'FROM nlp_record_origins WHERE account_ref=? AND record_digest=?', (account_ref, record_digest)).fetchall()
    if not rows:
        return True
    if any(row[0] is None for row in rows):
        raise OriginCheckerUnavailable('nlp_origin_checker_unavailable')
    return any(row[0] == 1 for row in rows)


def require_snapshot_origins(db, account_ref, keys, check=lambda: None):
    for key in keys:
        check()
        if not origin_live(db, account_ref, key):
            raise ValueError('nlp_origin_retired')
        check()
