from contextlib import contextmanager
from types import SimpleNamespace

from app.provisioning import progress
from app.provisioning.app import _validated_progress


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, statement, parameters):
        assert "FROM provisioning_candidates" in statement
        assert parameters == ("installation-1",)
        return _Rows(self.rows)


class _Database:
    def __init__(self, rows):
        self.rows = rows

    @contextmanager
    def read(self):
        yield _Connection(self.rows)


class _Store:
    def __init__(self, *, unresolved=(), consumed=(), rows=()):
        self._unresolved = tuple(unresolved)
        self._consumed = tuple(consumed)
        self.database = _Database(rows)

    def unresolved_claim_submissions(self):
        return self._unresolved

    def consumed_claim_submissions(self):
        return self._consumed


def _reader(monkeypatch, store):
    monkeypatch.setattr(progress, "SQLiteAuthenticationStore", _Store)
    return progress.durable_provisioning_progress(lambda: store)


def test_progress_requires_registration_before_any_consumed_claim(monkeypatch):
    read = _reader(monkeypatch, _Store())
    assert read() == {
        "stage": "registration_required",
        "association_request_id": None,
        "creator_account_id": None,
    }


def test_progress_blocks_claim_replay_when_hosted_outcome_is_unresolved(monkeypatch):
    read = _reader(monkeypatch, _Store(unresolved=(SimpleNamespace(),)))
    assert read()["stage"] == "recovery_required"


def test_progress_resumes_creator_confirmation_after_consumed_registration(monkeypatch):
    claim = SimpleNamespace(installation_id="installation-1")
    read = _reader(monkeypatch, _Store(consumed=(claim,)))
    assert read() == {
        "stage": "creator_confirmation_required",
        "association_request_id": None,
        "creator_account_id": None,
    }


def test_progress_resumes_pending_approval_with_hidden_coordinates(monkeypatch):
    claim = SimpleNamespace(installation_id="installation-1")
    row = {
        "association_request_id": "request-1",
        "creator_account_id": "creator-1",
        "state": "pending",
    }
    read = _reader(monkeypatch, _Store(consumed=(claim,), rows=(row,)))
    assert read() == {
        "stage": "creator_approval_pending",
        "association_request_id": "request-1",
        "creator_account_id": "creator-1",
    }


def test_progress_resumes_finalization_after_durable_approval(monkeypatch):
    claim = SimpleNamespace(installation_id="installation-1")
    row = {
        "association_request_id": "request-1",
        "creator_account_id": "creator-1",
        "state": "approved",
    }
    read = _reader(monkeypatch, _Store(consumed=(claim,), rows=(row,)))
    assert read()["stage"] == "finalization_ready"


def test_progress_fails_closed_when_more_than_one_active_candidate_exists(monkeypatch):
    claim = SimpleNamespace(installation_id="installation-1")
    rows = (
        {
            "association_request_id": "request-1",
            "creator_account_id": "creator-1",
            "state": "pending",
        },
        {
            "association_request_id": "request-2",
            "creator_account_id": "creator-1",
            "state": "approved",
        },
    )
    read = _reader(monkeypatch, _Store(consumed=(claim,), rows=rows))
    assert read()["stage"] == "recovery_required"


def test_status_progress_contract_rejects_coordinates_on_non_coordinate_stages():
    assert _validated_progress(
        {
            "stage": "creator_approval_pending",
            "association_request_id": "request-1",
            "creator_account_id": "creator-1",
        }
    )["stage"] == "creator_approval_pending"

    try:
        _validated_progress(
            {
                "stage": "registration_required",
                "association_request_id": "request-1",
                "creator_account_id": None,
            }
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("registration state accepted hidden coordinates")
