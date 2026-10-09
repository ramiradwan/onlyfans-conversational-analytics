"""Bounded local journey coordinates; none of these records grants authority."""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from app.persistence.auth import SQLiteAuthenticationStore, _new_uuid7


def require_journey(value: str) -> str:
    if str(UUID(value, version=4)) != value:
        raise ValueError("Invalid journey reference")
    return value


class OnboardingJourneyUnavailable(ValueError):
    """A bounded existing journey cannot authorize another operation."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OnboardingJourneyStore:
    def __init__(self, authentication: SQLiteAuthenticationStore) -> None:
        self.authentication = authentication

    def open(self, journey_id: str | None = None) -> dict:
        with self.authentication.database.transaction() as connection:
            return self._open(connection, journey_id, self.authentication._now())

    def _open(self, connection, journey_id, now) -> dict:
        if journey_id is not None:
            previous = connection.execute("SELECT expires_at FROM onboarding_journeys WHERE journey_id=?",
                                          (require_journey(journey_id),)).fetchone()
            if previous is not None and previous["expires_at"] <= now.isoformat():
                raise OnboardingJourneyUnavailable("journey_expired")
        self._cleanup(connection, now)
        if journey_id is None:
            row = connection.execute(
                "SELECT * FROM onboarding_journeys WHERE expires_at > ? ORDER BY created_at DESC LIMIT 1",
                (now.isoformat(),)).fetchone()
            if row is not None:
                return dict(row)
        else:
            row = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?", (journey_id,)).fetchone()
            if row is not None:
                return dict(row)
            if connection.execute("SELECT 1 FROM onboarding_workspace_recovery WHERE previous_journey_id=?",
                                  (journey_id,)).fetchone() is not None:
                raise OnboardingJourneyUnavailable("journey_expired")
        # Receipt recovery needs an exact former workspace, never a latest row.
        if connection.execute("SELECT 1 FROM onboarding_uncertain_receipts WHERE expires_at>?",
                              (now.isoformat(),)).fetchone() is not None:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        journey_id = journey_id or str(uuid4())
        if connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] >= 128:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        connection.execute(
            "INSERT INTO onboarding_journeys (journey_id,created_at,expires_at,installation_id,state) VALUES (?,?,?,?,'new')",
            (journey_id, now.isoformat(), (now+timedelta(minutes=30)).isoformat(), _new_uuid7(now)))
        return dict(connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?", (journey_id,)).fetchone())

    def create_session(self, *, journey_id: str | None, identifier: str, csrf: str,
                       expires_at: float, authorize=None) -> dict:
        with self.authentication.database.transaction() as connection:
            if authorize is not None:
                authorize()
            journey = self._open(connection, journey_id, self.authentication._now())
            expires_at = min(expires_at, datetime.fromisoformat(journey["expires_at"]).timestamp())
            self.record_session(identifier=identifier, csrf=csrf, journey_id=journey["journey_id"],
                expires_at=expires_at, authorize=authorize, connection=connection)
            return {"journey_id": journey["journey_id"], "expires_at": expires_at}

    def get(self, journey_id: str) -> dict | None:
        require_journey(journey_id)
        now=self.authentication._now()
        with self.authentication.database.read() as connection:
            row = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id = ?", (journey_id,)).fetchone()
            value=None if row is None else dict(row)
        if value is not None and value["expires_at"]<=now.isoformat():
            with self.authentication.database.transaction() as connection:
                self._cleanup(connection,now)
            return None
        return value

    def require_current(self, journey_id: str) -> dict:
        """Read an existing draft only; a stale command can never recreate it."""
        with self.authentication.database.read() as connection:
            row = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?",
                                     (require_journey(journey_id),)).fetchone()
        if row is None:
            raise OnboardingJourneyUnavailable("journey_unavailable")
        if datetime.fromisoformat(row["expires_at"]) <= self.authentication._now():
            raise OnboardingJourneyUnavailable("journey_expired")
        return dict(row)

    def _cleanup(self,connection,now) -> None:
        moment=now.isoformat()
        connection.execute("DELETE FROM onboarding_workspace_recovery WHERE expires_at<=?", (moment,))
        connection.execute("DELETE FROM onboarding_uncertain_receipts WHERE expires_at<=?",(moment,))
        rows=connection.execute("SELECT * FROM onboarding_journeys WHERE expires_at<=?",(moment,)).fetchall()
        for row in rows:
            original_expiry=datetime.fromisoformat(row["expires_at"])
            deadline = row["recovery_deadline"] or (original_expiry+timedelta(minutes=30)).isoformat()
            if deadline > moment:
                transferring = connection.execute(
                    "SELECT 1 FROM onboarding_transfer_intents WHERE journey_id=? UNION ALL "
                    "SELECT 1 FROM onboarding_transfer_proofs WHERE journey_id=? LIMIT 1",
                    (row["journey_id"], row["journey_id"])).fetchone() is not None
                retired_state = ("transfer" if transferring and row["state"] in
                    {"new", "preparing", "prepare-unknown", "waiting"} else row["state"])
                connection.execute(
                    "INSERT INTO onboarding_workspace_recovery VALUES (?,?,?,?,?,?,?,?,NULL) "
                    "ON CONFLICT(previous_journey_id) DO NOTHING",
                    (row["journey_id"], row["installation_id"], row["operation_id"], retired_state,
                     row["scope_json"], row["prepare_json"] if row["state"] in {"preparing", "prepare-unknown"} else None,
                     row["expires_at"], deadline))
            if (deadline > moment and row["state"] in {"unknown", "completing"}
                    and row["scope_json"] is not None and row["operation_id"] is not None):
                connection.execute("INSERT INTO onboarding_uncertain_receipts VALUES (?,?,?,?,?) ON CONFLICT(installation_id) DO NOTHING",
                    (row["installation_id"],row["operation_id"],row["scope_json"],original_expiry.isoformat(),deadline))
        connection.execute("DELETE FROM provisioning_browser_sessions WHERE expires_at<=? OR journey_id IN (SELECT journey_id FROM onboarding_journeys WHERE expires_at<=?)",(now.timestamp(),moment))
        connection.execute("DELETE FROM onboarding_journeys WHERE expires_at<=?",(moment,))
        connection.execute("DELETE FROM onboarding_transfer_relays WHERE expires_at<=?",(now.timestamp(),))
        connection.execute("DELETE FROM onboarding_transfer_intents WHERE expires_at<=?",(now.timestamp(),))

    def update(self, journey_id: str, *, state: str, require_current: bool = False, **changes) -> dict:
        allowed = {"operation_id", "handoff_reference", "handoff_expires_at", "scope_json", "prepare_json", "reason"}
        if not set(changes) <= allowed:
            raise ValueError("Invalid journey update")
        with self.authentication.database.transaction() as connection:
            def check_current():
                if not require_current:
                    return
                row = connection.execute("SELECT expires_at FROM onboarding_journeys WHERE journey_id=?",
                                         (journey_id,)).fetchone()
                if row is None:
                    raise OnboardingJourneyUnavailable("journey_unavailable")
                if datetime.fromisoformat(row["expires_at"]) <= self.authentication._now():
                    raise OnboardingJourneyUnavailable("journey_expired")
            check_current()
            names = ["state", *changes]
            values = [state, *changes.values(), require_journey(journey_id)]
            connection.execute("UPDATE onboarding_journeys SET " + ", ".join(name + " = ?" for name in names)
                               + ", revision = revision + 1 WHERE journey_id = ?", values)
            if state == "completed":
                connection.execute("DELETE FROM onboarding_uncertain_receipts WHERE installation_id IN (SELECT installation_id FROM onboarding_journeys WHERE journey_id=?)",(journey_id,))
            check_current()
        return self.get(journey_id)

    def new_operation(self) -> str:
        return _new_uuid7(self.authentication._now())

    def record_session(self, *, identifier: str, csrf: str, journey_id: str, expires_at: float,
                       authorize=None, connection=None) -> None:
        with (self.authentication.database.transaction() if connection is None else nullcontext(connection)) as connection:
            if authorize is not None:
                authorize()
            def current_scope():
                now = self.authentication._now()
                journey = connection.execute("SELECT expires_at FROM onboarding_journeys WHERE journey_id=?",
                                             (require_journey(journey_id),)).fetchone()
                if journey is None:
                    raise OnboardingJourneyUnavailable("journey_unavailable")
                deadline = datetime.fromisoformat(journey["expires_at"]).timestamp()
                if deadline <= now.timestamp() or expires_at <= now.timestamp():
                    raise OnboardingJourneyUnavailable("journey_expired")
                if expires_at > deadline:
                    raise OnboardingJourneyUnavailable("journey_unavailable")
                return now
            now = current_scope()
            connection.execute("DELETE FROM provisioning_browser_sessions WHERE expires_at <= ?", (now.timestamp(),))
            if connection.execute("SELECT count(*) FROM provisioning_browser_sessions").fetchone()[0] >= 128:
                raise ValueError("Onboarding browser session limit reached")
            connection.execute("INSERT INTO provisioning_browser_sessions VALUES (?,?,?,?)",
                               (_digest(identifier), _digest(csrf), require_journey(journey_id), expires_at))
            if authorize is not None:
                authorize()
            current_scope()

    def session(self, identifier: str) -> dict | None:
        with self.authentication.database.read() as connection:
            row = connection.execute("SELECT * FROM provisioning_browser_sessions WHERE session_digest = ?", (_digest(identifier),)).fetchone()
            return None if row is None else dict(row)

    def current_session(self, identifier: str) -> dict | None:
        """Recheck durable session and journey together, including cached callers."""
        with self.authentication.database.read() as connection:
            row = connection.execute(
                "SELECT s.*, j.expires_at AS journey_expires_at FROM provisioning_browser_sessions s "
                "JOIN onboarding_journeys j ON j.journey_id=s.journey_id WHERE s.session_digest=?",
                (_digest(identifier),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        deadline = min(result["expires_at"], datetime.fromisoformat(result.pop("journey_expires_at")).timestamp())
        if deadline <= self.authentication._now().timestamp():
            return None
        result["expires_at"] = deadline
        return result

    def renew_native_session(self, *, previous_journey_id: str, identifier: str, csrf: str,
                             existing_identifier: str | None, ttl_seconds: float, authorize) -> dict:
        """Resolve one exact workspace and issue its session in the same transaction.

        A tombstone preserves the original operation/deadline; no latest receipt
        lookup, authority import or hosted call is permitted here.
        """
        prior = require_journey(previous_journey_id)
        with self.authentication.database.transaction() as connection:
            authorize()
            now = self.authentication._now()
            self._cleanup(connection, now)
            original = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?",
                                          (prior,)).fetchone()
            retired = connection.execute("SELECT * FROM onboarding_workspace_recovery WHERE previous_journey_id=?",
                                         (prior,)).fetchone()
            resolved = original
            if resolved is None and retired is not None and retired["resolved_journey_id"] is not None:
                resolved = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?",
                                              (retired["resolved_journey_id"],)).fetchone()
                if resolved is None:
                    raise OnboardingJourneyUnavailable("journey_unavailable")

            existing = None if not existing_identifier else connection.execute(
                "SELECT s.*, j.expires_at AS journey_expires_at FROM provisioning_browser_sessions s "
                "JOIN onboarding_journeys j ON j.journey_id=s.journey_id WHERE s.session_digest=?",
                (_digest(existing_identifier),)).fetchone()
            if existing is not None and (existing["expires_at"] <= now.timestamp()
                    or datetime.fromisoformat(existing["journey_expires_at"]) <= now):
                existing = None
            if existing is not None and (resolved is None or existing["journey_id"] != resolved["journey_id"]):
                raise OnboardingJourneyUnavailable("journey_unavailable")

            if resolved is None:
                if retired is None or retired["expires_at"] <= now.isoformat():
                    raise OnboardingJourneyUnavailable("journey_unavailable")
                state = retired["state"]
                if state in {"unknown", "completing"} and retired["operation_id"] and retired["scope_json"]:
                    state = "unknown"
                    operation, scope, preparation = retired["operation_id"], retired["scope_json"], None
                    installation = retired["installation_id"]
                elif (state in {"preparing", "prepare-unknown"} and retired["operation_id"]
                      and retired["prepare_json"] and retired["scope_json"] is None):
                    state = "prepare-unknown"
                    operation, scope, preparation = retired["operation_id"], None, retired["prepare_json"]
                    installation = retired["installation_id"]
                elif ((state == "new" and retired["operation_id"] is None and retired["scope_json"] is None)
                      or (state == "expired" and retired["scope_json"] is None)):
                    state, operation, scope, preparation = "new", None, None, None
                    installation = _new_uuid7(now)
                else:
                    # An unconfirmed prepared/registered scope is not permission
                    # to start another operation or discard its receipt.
                    raise OnboardingJourneyUnavailable("journey_unavailable")
                if connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] >= 128:
                    raise OnboardingJourneyUnavailable("journey_unavailable")
                successor = str(uuid4())
                deadline = min((now + timedelta(minutes=30)).isoformat(), retired["expires_at"])
                connection.execute(
                    "INSERT INTO onboarding_journeys (journey_id,created_at,expires_at,installation_id,state,"
                    "operation_id,scope_json,prepare_json,recovery_deadline) VALUES (?,?,?,?,?,?,?,?,?)",
                    (successor, now.isoformat(), deadline, installation, state, operation, scope,
                     preparation, retired["expires_at"]))
                connection.execute("UPDATE onboarding_workspace_recovery SET resolved_journey_id=? WHERE previous_journey_id=?",
                                   (successor, prior))
                resolved = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?", (successor,)).fetchone()

            deadline = datetime.fromisoformat(resolved["expires_at"]).timestamp()
            if deadline <= self.authentication._now().timestamp():
                raise OnboardingJourneyUnavailable("journey_expired")
            if existing is not None:
                selected_identifier = existing_identifier
                expires_at = min(existing["expires_at"], deadline)
            else:
                selected_identifier = identifier
                expires_at = min(now.timestamp() + ttl_seconds, deadline)
                if connection.execute("SELECT count(*) FROM provisioning_browser_sessions").fetchone()[0] >= 128:
                    raise OnboardingJourneyUnavailable("journey_unavailable")
                connection.execute("INSERT INTO provisioning_browser_sessions VALUES (?,?,?,?)",
                    (_digest(identifier), _digest(csrf), resolved["journey_id"], expires_at))
            authorize()
            if (expires_at <= self.authentication._now().timestamp()
                    or (retired is not None and retired["expires_at"] <= self.authentication._now().isoformat())):
                raise OnboardingJourneyUnavailable("journey_expired")
            return {"identifier": selected_identifier, "expires_at": expires_at,
                    "journey_id": resolved["journey_id"]}

    def verify_session_csrf(self, identifier: str, csrf: str) -> bool:
        row = self.session(identifier)
        return bool(row and row["csrf_digest"] == _digest(csrf))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
