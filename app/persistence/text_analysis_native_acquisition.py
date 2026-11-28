"""Durable private journal for exact native acquisition retries."""
from dataclasses import dataclass, field
import hashlib

from app.security.text_analysis_contract import canonical, parse_json


class AcquisitionJournalError(ValueError):
    """A persisted operation is invalid, stale or unavailable."""


@dataclass(frozen=True)
class AcquisitionRecord:
    flow_id: str
    scope: str
    kind: str
    operation_id: str
    version: int
    created_at: str
    data: dict = field(repr=False)


_TABLE = "text_analysis_native_acquisitions"
_MAX_BYTES = 98304
_MAX_RECORDS = 64


def _decode(row):
    if row is None:
        return None
    raw = row["data"]
    if type(raw) is not bytes or len(raw) > _MAX_BYTES or hashlib.sha256(raw).hexdigest() != row["data_sha256"]:
        raise AcquisitionJournalError("acquisition_journal_corrupt")
    data = parse_json(raw, maximum=_MAX_BYTES, require_canonical=True)
    prepare = data.get("prepare") if type(data) is dict else None
    if type(prepare) is not dict or prepare.get("operation_id") != row["operation_id"]:
        raise AcquisitionJournalError("acquisition_journal_corrupt")
    return AcquisitionRecord(row["flow_id"], row["scope_sha256"], row["kind"],
                             row["operation_id"], row["version"], row["created_at"], data)


def _encoded(data):
    raw = canonical(data)
    if len(raw) > _MAX_BYTES:
        raise AcquisitionJournalError("acquisition_journal_capacity")
    return raw, hashlib.sha256(raw).hexdigest()


class TextAnalysisAcquisitionStore:
    def __init__(self, authentication):
        self.database = authentication.database

    def get(self, flow_id):
        with self.database.read() as db:
            return _decode(db.execute(f"SELECT * FROM {_TABLE} WHERE flow_id=?", (flow_id,)).fetchone())

    def latest(self, scope, kind):
        with self.database.read() as db:
            return _decode(db.execute(f"""SELECT * FROM {_TABLE} WHERE scope_sha256=? AND kind=?
                ORDER BY journal_id DESC LIMIT 1""", (scope, kind)).fetchone())

    def begin(self, record, *, previous):
        raw, digest = _encoded(record.data)
        with self.database.transaction() as db:
            current = _decode(db.execute(f"""SELECT * FROM {_TABLE} WHERE scope_sha256=? AND kind=?
                ORDER BY journal_id DESC LIMIT 1""", (record.scope, record.kind)).fetchone())
            if current != previous:
                raise AcquisitionJournalError("acquisition_changed")
            if db.execute(f"SELECT count(*) FROM {_TABLE}").fetchone()[0] >= _MAX_RECORDS:
                raise AcquisitionJournalError("acquisition_journal_capacity")
            db.execute(f"""INSERT INTO {_TABLE}
                (flow_id,scope_sha256,kind,operation_id,version,created_at,data,data_sha256)
                VALUES (?,?,?,?,?,?,?,?)""", (record.flow_id, record.scope, record.kind,
                    record.operation_id, 1, record.created_at, raw, digest))
        return record

    def update(self, record, **changes):
        raw, digest = _encoded(record.data | changes)
        with self.database.transaction() as db:
            changed = db.execute(f"""UPDATE {_TABLE} SET version=version+1,data=?,data_sha256=?
                WHERE flow_id=? AND scope_sha256=? AND version=?""",
                (raw, digest, record.flow_id, record.scope, record.version))
            if changed.rowcount != 1:
                raise AcquisitionJournalError("acquisition_changed")
            return _decode(db.execute(f"SELECT * FROM {_TABLE} WHERE flow_id=?", (record.flow_id,)).fetchone())


def validate_acquisition_journal_in(db):
    rows = db.execute(f"SELECT * FROM {_TABLE} LIMIT ?", (_MAX_RECORDS + 1,)).fetchall()
    if len(rows) > _MAX_RECORDS:
        raise AcquisitionJournalError("acquisition_journal_capacity")
    for row in rows:
        _decode(row)
