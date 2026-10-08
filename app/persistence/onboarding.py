"""Bounded local journey coordinates; none of these records grants authority."""
from __future__ import annotations

import hashlib
from datetime import timedelta
from uuid import UUID, uuid4

from app.persistence.auth import SQLiteAuthenticationStore, _new_uuid7


def require_journey(value: str) -> str:
    if str(UUID(value, version=4)) != value:
        raise ValueError("Invalid journey reference")
    return value


class OnboardingJourneyStore:
    def __init__(self, authentication: SQLiteAuthenticationStore) -> None:
        self.authentication = authentication

    def open(self, journey_id: str | None = None) -> dict:
        now = self.authentication._now()
        with self.authentication.database.transaction() as connection:
            if journey_id is not None:
                previous = connection.execute("SELECT expires_at FROM onboarding_journeys WHERE journey_id=?",(require_journey(journey_id),)).fetchone()
                if previous is not None and previous["expires_at"] <= now.isoformat():
                    self._cleanup(connection,now)
                    raise ValueError("Onboarding journey expired")
            self._cleanup(connection,now)
            if journey_id is None:
                row = connection.execute(
                    "SELECT * FROM onboarding_journeys WHERE expires_at > ? ORDER BY created_at DESC LIMIT 1",
                    (now.isoformat(),),
                ).fetchone()
                if row is not None:
                    return dict(row)
            else:
                require_journey(journey_id)
                row = connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id = ?", (journey_id,)).fetchone()
                if row is not None:
                    return dict(row)
            journey_id = journey_id or str(uuid4())
            if connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] >= 128:
                raise ValueError("Onboarding journey limit reached")
            connection.execute(
                "INSERT INTO onboarding_journeys (journey_id, created_at, expires_at, installation_id, state) VALUES (?,?,?,?, 'new')",
                (journey_id, now.isoformat(), (now + timedelta(minutes=30)).isoformat(), _new_uuid7(now)),
            )
            receipt=connection.execute("SELECT * FROM onboarding_uncertain_receipts WHERE expires_at>? ORDER BY retired_at DESC LIMIT 1",(now.isoformat(),)).fetchone()
            if receipt is not None:
                connection.execute("UPDATE onboarding_journeys SET installation_id=?,operation_id=?,scope_json=?,state='unknown',expires_at=?,recovery_deadline=? WHERE journey_id=?",
                    (receipt["installation_id"],receipt["operation_id"],receipt["scope_json"],min((now+timedelta(minutes=30)).isoformat(),receipt["expires_at"]),receipt["expires_at"],journey_id))
            return dict(connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id = ?", (journey_id,)).fetchone())

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

    def _cleanup(self,connection,now) -> None:
        moment=now.isoformat()
        connection.execute("DELETE FROM onboarding_uncertain_receipts WHERE expires_at<=?",(moment,))
        rows=connection.execute("SELECT installation_id,operation_id,scope_json,expires_at FROM onboarding_journeys WHERE expires_at<=? AND recovery_deadline IS NULL AND state IN ('unknown','completing') AND scope_json IS NOT NULL AND operation_id IS NOT NULL",(moment,)).fetchall()
        for row in rows:
            from datetime import datetime
            original_expiry=datetime.fromisoformat(row["expires_at"])
            if original_expiry+timedelta(minutes=30)>now:
                connection.execute("INSERT INTO onboarding_uncertain_receipts VALUES (?,?,?,?,?) ON CONFLICT(installation_id) DO NOTHING",
                    (row["installation_id"],row["operation_id"],row["scope_json"],original_expiry.isoformat(),(original_expiry+timedelta(minutes=30)).isoformat()))
        connection.execute("DELETE FROM provisioning_browser_sessions WHERE expires_at<=? OR journey_id IN (SELECT journey_id FROM onboarding_journeys WHERE expires_at<=?)",(now.timestamp(),moment))
        connection.execute("DELETE FROM onboarding_journeys WHERE expires_at<=?",(moment,))
        connection.execute("DELETE FROM onboarding_transfer_relays WHERE expires_at<=?",(now.timestamp(),))
        connection.execute("DELETE FROM onboarding_transfer_intents WHERE expires_at<=?",(now.timestamp(),))

    def update(self, journey_id: str, *, state: str, **changes) -> dict:
        allowed = {"operation_id", "handoff_reference", "handoff_expires_at", "scope_json", "prepare_json", "reason"}
        if not set(changes) <= allowed:
            raise ValueError("Invalid journey update")
        with self.authentication.database.transaction() as connection:
            names = ["state", *changes]
            values = [state, *changes.values(), require_journey(journey_id)]
            connection.execute("UPDATE onboarding_journeys SET " + ", ".join(name + " = ?" for name in names)
                               + ", revision = revision + 1 WHERE journey_id = ?", values)
            if state == "completed":
                connection.execute("DELETE FROM onboarding_uncertain_receipts WHERE installation_id IN (SELECT installation_id FROM onboarding_journeys WHERE journey_id=?)",(journey_id,))
        return self.get(journey_id)

    def new_operation(self) -> str:
        return _new_uuid7(self.authentication._now())

    def record_session(self, *, identifier: str, csrf: str, journey_id: str, expires_at: float) -> None:
        with self.authentication.database.transaction() as connection:
            connection.execute("DELETE FROM provisioning_browser_sessions WHERE expires_at <= ?", (self.authentication._now().timestamp(),))
            if connection.execute("SELECT count(*) FROM provisioning_browser_sessions").fetchone()[0] >= 128:
                raise ValueError("Onboarding browser session limit reached")
            connection.execute("INSERT INTO provisioning_browser_sessions VALUES (?,?,?,?)",
                               (_digest(identifier), _digest(csrf), require_journey(journey_id), expires_at))

    def session(self, identifier: str) -> dict | None:
        with self.authentication.database.read() as connection:
            row = connection.execute("SELECT * FROM provisioning_browser_sessions WHERE session_digest = ?", (_digest(identifier),)).fetchone()
            return None if row is None else dict(row)

    def verify_session_csrf(self, identifier: str, csrf: str) -> bool:
        row = self.session(identifier)
        return bool(row and row["csrf_digest"] == _digest(csrf))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
