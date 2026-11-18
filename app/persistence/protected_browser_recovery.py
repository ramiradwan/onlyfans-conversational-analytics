"""Durable protected ceremony and fresh-authentication presentation owner.

The installation recovery owner remains the only authority over recovered grants.
A read does not create a ceremony or child, and an exact historical child read
does not authorize a browser launch or a new child.
"""
from __future__ import annotations

import hashlib
import json
import secrets

from app.persistence.auth import _new_uuid7
from app.security.browser_ceremony import binding_digest, browser_entry_url
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp
from app.security.installation_continuation import canonical_request
from app.security.protected_browser_recovery import (
    recovery_binding, ceremony_request, authentication_request,
    validate_ceremony_result, validate_authentication_result,
)


def _encoded(value):
    return canonical_request(value).decode()


class ProtectedBrowserRecoveries:
    def __init__(self, recovery):
        self.recovery = recovery
        self.store = recovery.store

    def row(self, connection, recovery_id):
        row = connection.execute("SELECT * FROM protected_recovery_ceremonies WHERE recovery_id=?", (recovery_id,)).fetchone()
        return None if row is None else dict(row)

    def child(self, connection, recovery_id):
        row = connection.execute("SELECT * FROM protected_recovery_authentication_children WHERE recovery_id=? "
            "ORDER BY sequence DESC LIMIT 1", (recovery_id,)).fetchone()
        return None if row is None else dict(row)

    def _exact(self, connection, recovery_id, *, authorize):
        authorize(connection=connection)
        row = self.row(connection, recovery_id)
        source = self.recovery.row(connection, recovery_id)
        if row is None or source is None:
            raise HostedGrantUnavailable("Protected recovery ceremony is unavailable")
        request = json.loads(source["request_json"])
        source, _ = self.recovery.exact(connection, request, authorize=authorize)
        binding = json.loads(row["binding_json"])
        if (source["state"] != "prepared" or source["source_reference"] is None
                or row["binding_digest"] != binding_digest(binding)
                or binding != recovery_binding(request, {"reference": source["source_reference"],
                    "expires_at": source["source_expires_at"]}, attempt_id=binding["attempt_id"],
                    completion=binding["completion"])):
            raise HostedGrantUnavailable("Protected recovery source changed")
        return row, request, binding

    def reserve(self, connection, request, *, authorize):
        """An explicit trusted command reserves one presentation, not a sign-in."""
        source = self.recovery.browser_source(connection, request, authorize=authorize)
        recovery_id = request["recovery_id"]
        if self.row(connection, recovery_id) is not None:
            raise HostedGrantUnavailable("Original protected recovery ceremony already reserved")
        now = self.store._now()
        binding = recovery_binding(request, source, attempt_id=secrets.token_urlsafe(32),
            completion={"mode": "system-browser"})
        operation_id = _new_uuid7(now)
        ceremony_request(operation_id, binding)
        connection.execute("INSERT INTO protected_recovery_ceremonies "
            "(recovery_id,operation_id,binding_json,binding_digest,created_at) VALUES (?,?,?,?,?)",
            (recovery_id, operation_id, _encoded(binding), binding_digest(binding), now.timestamp()))
        authorize(connection=connection)
        return self.row(connection, recovery_id)

    def _live(self, row, binding):
        now = self.store._now()
        if (row["ceremony_id"] is None or row["presentation_state"] != "active"
                or row["domain_status"] != "awaiting-authentication" or row["entry_reference"] is None
                or row["cancel_claimed"] or row["committed_seen"]
                or now >= timestamp(row["expires_at"]) or now >= timestamp(binding["source_expires_at"])):
            raise HostedGrantUnavailable("Protected recovery presentation is unavailable")

    def claim_prepare(self, connection, recovery_id, *, authorize):
        row, request, binding = self._exact(connection, recovery_id, authorize=authorize)
        if row["prepare_claimed"]:
            return None
        self.recovery.browser_source(connection, request, authorize=authorize)
        connection.execute("UPDATE protected_recovery_ceremonies SET prepare_claimed=1 WHERE recovery_id=?", (recovery_id,))
        authorize(connection=connection)
        return ceremony_request(row["operation_id"], binding)

    def read_request(self, connection, recovery_id, *, authorize):
        row, _, binding = self._exact(connection, recovery_id, authorize=authorize)
        if not row["prepare_claimed"]:
            raise HostedGrantUnavailable("Protected recovery ceremony was never sent")
        return ceremony_request(row["operation_id"], binding, ceremony_id=row["ceremony_id"])

    def claim_cancel(self, connection, recovery_id, *, authorize):
        row, source, binding = self._exact(connection, recovery_id, authorize=authorize)
        self.recovery.browser_source(connection, source, authorize=authorize)
        self._live(row, binding)
        connection.execute("UPDATE protected_recovery_ceremonies SET cancel_claimed=1,entry_reference=NULL,projection_json=NULL "
            "WHERE recovery_id=?", (recovery_id,))
        authorize(connection=connection)
        return ceremony_request(row["operation_id"], binding, ceremony_id=row["ceremony_id"])

    def guard_ceremony(self, connection, recovery_id, verb, request, *, authorize):
        row, source, binding = self._exact(connection, recovery_id, authorize=authorize)
        expected = ceremony_request(row["operation_id"], binding, ceremony_id=None if verb=="prepare" else row["ceremony_id"])
        if request != expected or not row["prepare_claimed"]:
            raise HostedGrantUnavailable("Protected recovery ceremony command changed")
        if verb == "prepare":
            if row["ceremony_id"] is not None or row["cancel_claimed"]:
                raise HostedGrantUnavailable("Original ceremony preparation already resolved")
            self.recovery.browser_source(connection, source, authorize=authorize)
        elif verb == "cancel":
            if row["ceremony_id"] is None or not row["cancel_claimed"] or row["presentation_state"] != "active":
                raise HostedGrantUnavailable("Protected ceremony cancellation was not admitted")
        elif verb not in {"read", "recover"} or (verb=="recover" and row["ceremony_id"] is None):
            raise HostedGrantUnavailable("Protected recovery ceremony command unavailable")
        return row

    def record_ceremony(self, connection, recovery_id, verb, request, result, *, authorize):
        row = self.guard_ceremony(connection, recovery_id, verb, request, authorize=authorize)
        now = self.store._now()
        validate_ceremony_result(verb, request, result, now=now, expires_at=row["expires_at"])
        if verb != "prepare" and result["status"] == "unavailable":
            # Missing presentation is not a domain outcome and not permission
            # to resend the original preparation or create another ceremony.
            connection.execute("UPDATE protected_recovery_ceremonies SET entry_reference=NULL,projection_json=NULL "
                "WHERE recovery_id=?", (recovery_id,))
            authorize(connection=connection)
            return self.row(connection, recovery_id)
        view = result if verb == "prepare" else result["snapshot"]
        present = "active" if verb=="prepare" else view["presentation_state"]
        domain = "awaiting-authentication" if verb=="prepare" else view["domain_status"]
        revision = row["revision"] if verb=="prepare" else view["revision"]
        if (row["revision"] is not None and (revision is None or revision < row["revision"])
                or row["presentation_state"] in {"retired","expired"} and present=="active"
                or row["presentation_state"]=="expired" and present!="expired"
                or row["committed_seen"] and domain=="awaiting-authentication"
                or row["domain_status"]=="revoked" and domain!="revoked"
                or timestamp(view["expires_at"]).timestamp() > row["created_at"]+1860):
            raise HostedGrantUnavailable("Protected ceremony result regressed")
        entry = result if verb=="prepare" else result.get("entry")
        entry_digest = row["entry_digest"] if entry is None else hashlib.sha256(entry["entry_reference"].encode()).hexdigest()
        if row["entry_digest"] is not None and entry_digest != row["entry_digest"]:
            raise HostedGrantUnavailable("Protected ceremony entry changed")
        locator = None if entry is None or row["cancel_claimed"] else entry["entry_reference"]
        # Invalidate all previously actionable authentication projections on
        # every observation, especially on commit/retirement/expiry.
        connection.execute("UPDATE protected_recovery_ceremonies SET ceremony_id=?,expires_at=?,revision=?,"
            "presentation_state=?,domain_status=?,committed_seen=?,entry_reference=?,entry_digest=?,projection_json=NULL WHERE recovery_id=?",
            (view["ceremony_id"], view["expires_at"], revision, present, domain,
                int(bool(row["committed_seen"] or domain=="committed")), locator, entry_digest, recovery_id))
        authorize(connection=connection)
        return self.row(connection, recovery_id)

    def auth_read(self, connection, recovery_id, *, authorize, exact_child=False):
        row, _, binding = self._exact(connection, recovery_id, authorize=authorize)
        child = self.child(connection, recovery_id)
        if row["ceremony_id"] is None or (exact_child and child is None):
            raise HostedGrantUnavailable("Protected authentication is unavailable")
        return authentication_request("read", ceremony_request(row["operation_id"], binding, ceremony_id=row["ceremony_id"]),
            attempt_id=child["attempt_id"] if exact_child else None)

    def _projection(self, connection, recovery_id, *, authorize):
        row, source, binding = self._exact(connection, recovery_id, authorize=authorize)
        self.recovery.browser_source(connection, source, authorize=authorize)
        self._live(row, binding)
        if row["projection_json"] is None:
            raise HostedGrantUnavailable("Current protected authentication selection is unavailable")
        projection = json.loads(row["projection_json"])
        child = self.child(connection, recovery_id)
        expected_id = None if child is None else child["attempt_id"]
        actual_id = None if projection["authentication_attempt"] is None else projection["authentication_attempt"]["authentication_attempt_id"]
        if actual_id != expected_id:
            raise HostedGrantUnavailable("Protected authentication selection changed")
        return row, binding, child, projection

    def plan_authentication(self, connection, recovery_id, verb, *, authorize, attempt_id=None):
        """Plan a closed mutation without claiming it; used at desktop admission."""
        row, binding, child, projection = self._projection(connection, recovery_id, authorize=authorize)
        ceremony = ceremony_request(row["operation_id"], binding, ceremony_id=row["ceremony_id"])
        if verb == "select":
            if not projection["actions"]["select"] or (child is None and projection["state"]!="selection-required") or (
                    child is not None and (child["state"]!="retired" or projection["state"]!="retired" or child["sequence"]>=32)):
                raise HostedGrantUnavailable("Protected authentication selection is unavailable")
            return authentication_request("select", ceremony,
                attempt_id=attempt_id or _new_uuid7(self.store._now()), previous_id=None if child is None else child["attempt_id"])
        if verb == "cancel":
            if child is None or child["state"]!="selected" or not projection["actions"]["cancel"]:
                raise HostedGrantUnavailable("Protected authentication cancellation is unavailable")
            if attempt_id is not None and attempt_id != child["attempt_id"]:
                raise HostedGrantUnavailable("Protected authentication child changed")
            return authentication_request("cancel", ceremony, attempt_id=child["attempt_id"])
        raise ValueError("Unsupported protected authentication command")

    def claim_select(self, connection, recovery_id, *, authorize, expected=None):
        request = self.plan_authentication(connection, recovery_id, "select", authorize=authorize,
            attempt_id=None if expected is None else expected["authentication_attempt_id"])
        if expected is not None and request != expected:
            raise HostedGrantUnavailable("Protected authentication selection changed")
        child = self.child(connection, recovery_id)
        connection.execute("INSERT INTO protected_recovery_authentication_children "
            "(attempt_id,recovery_id,previous_attempt_id,sequence,request_json,state) VALUES (?,?,?,?,?,'select-claimed')",
            (request["authentication_attempt_id"], recovery_id, request["expected_previous_attempt_id"],
                1 if child is None else child["sequence"]+1, _encoded(request)))
        connection.execute("UPDATE protected_recovery_ceremonies SET projection_json=NULL WHERE recovery_id=?", (recovery_id,))
        authorize(connection=connection)
        return request

    def claim_child_cancel(self, connection, recovery_id, *, authorize, expected=None):
        request = self.plan_authentication(connection, recovery_id, "cancel", authorize=authorize,
            attempt_id=None if expected is None else expected["authentication_attempt_id"])
        if expected is not None and request != expected:
            raise HostedGrantUnavailable("Protected authentication cancellation changed")
        connection.execute("UPDATE protected_recovery_authentication_children SET state='cancel-claimed' WHERE attempt_id=?",
            (request["authentication_attempt_id"],))
        connection.execute("UPDATE protected_recovery_ceremonies SET projection_json=NULL WHERE recovery_id=?", (recovery_id,))
        authorize(connection=connection)
        return request

    def guard_authentication(self, connection, recovery_id, verb, request, *, authorize):
        row, source, binding = self._exact(connection, recovery_id, authorize=authorize)
        child = self.child(connection, recovery_id)
        if row["ceremony_id"] is None:
            raise HostedGrantUnavailable("Protected authentication ceremony is unavailable")
        ceremony = ceremony_request(row["operation_id"], binding, ceremony_id=row["ceremony_id"])
        if verb == "read":
            expected = authentication_request("read", ceremony, attempt_id=request["authentication_attempt_id"])
            if request["authentication_attempt_id"] is not None and (child is None or child["attempt_id"]!=request["authentication_attempt_id"]):
                raise HostedGrantUnavailable("Protected authentication child changed")
        elif verb == "select" and child is not None and child["state"]=="select-claimed":
            self.recovery.browser_source(connection, source, authorize=authorize)
            self._live(row, binding)
            expected = json.loads(child["request_json"])
        elif verb == "cancel" and child is not None and child["state"]=="cancel-claimed":
            self.recovery.browser_source(connection, source, authorize=authorize)
            self._live(row, binding)
            expected = authentication_request("cancel", ceremony, attempt_id=child["attempt_id"])
        else:
            raise HostedGrantUnavailable("Protected authentication command was not admitted")
        if request != expected:
            raise HostedGrantUnavailable("Protected authentication request changed")
        return row, child

    def record_authentication(self, connection, recovery_id, verb, request, result, *, authorize):
        row, child = self.guard_authentication(connection, recovery_id, verb, request, authorize=authorize)
        validate_authentication_result(verb, request, result, now=self.store._now(), expires_at=row["expires_at"])
        state = result["state"]
        attempt = result["authentication_attempt"]
        named = None if attempt is None else attempt["authentication_attempt_id"]
        if named is not None and (child is None or named != child["attempt_id"]):
            raise HostedGrantUnavailable("Protected authentication child was not reserved")
        if state == "selection-required" and child is not None:
            # A current read missing an already reserved child cannot prove that
            # a lost selection did not commit. Do not admit a new child.
            raise HostedGrantUnavailable("Original protected selection remains unresolved")
        if child is not None and named == child["attempt_id"] and state in {"selected","retired","admitted"}:
            old = child["state"]
            if (old=="retired" and state!="retired" or old=="admitted" and state!="admitted"):
                raise HostedGrantUnavailable("Protected authentication child regressed")
            if old=="cancel-claimed" and state=="selected":
                state = "cancel-claimed"  # cancellation remains unresolved
            connection.execute("UPDATE protected_recovery_authentication_children SET state=? WHERE attempt_id=?", (state, named))
        if result["state"]=="admitted":
            if row["domain_status"]=="revoked":
                raise HostedGrantUnavailable("Protected recovery authority was revoked")
            connection.execute("UPDATE protected_recovery_ceremonies SET domain_status='committed',committed_seen=1,"
                "entry_reference=NULL,projection_json=NULL WHERE recovery_id=?", (recovery_id,))
        else:
            # Only a current (null-child) read may authorize a successor or
            # browser dispatch. Select/cancel replies and exact reads observe
            # that child and must be followed by a current projection.
            projection = None
            if verb=="read" and request["authentication_attempt_id"] is None and (
                    child is None and state=="selection-required" or child is not None and named==child["attempt_id"]
                    and state in {"selected","retired"}):
                try:
                    self._live(row, json.loads(row["binding_json"]))
                    projection = _encoded(result)
                except HostedGrantUnavailable:
                    pass
            connection.execute("UPDATE protected_recovery_ceremonies SET projection_json=? WHERE recovery_id=?", (projection, recovery_id))
        authorize(connection=connection)
        return self.child(connection, recovery_id)

    def claim_browser_open(self, connection, recovery_id, *, authorize):
        """An explicit customer action claims one dispatch of the current child.

        Returns a fixed-origin URL, not authority. Restart cannot repeat the
        claimed dispatch; a cancelled/retired child needs explicit selection.
        """
        row, _, child, projection = self._projection(connection, recovery_id, authorize=authorize)
        if (child is None or child["state"]!="selected" or child["open_claimed"]
                or not projection["actions"]["open_browser"]):
            raise HostedGrantUnavailable("Protected authentication browser dispatch is unavailable")
        connection.execute("UPDATE protected_recovery_authentication_children SET open_claimed=1 WHERE attempt_id=?", (child["attempt_id"],))
        authorize(connection=connection)
        return {"attempt_id": child["attempt_id"], "url": browser_entry_url(self.recovery.origin.rstrip("/"), row["entry_reference"])}

    def _guard_browser_open(self, connection, recovery_id, attempt_id, *, authorize):
        row, _, child, projection = self._projection(connection, recovery_id, authorize=authorize)
        if (child is None or child["attempt_id"]!=attempt_id or child["state"]!="selected" or not child["open_claimed"]
                or not projection["actions"]["open_browser"]):
            raise HostedGrantUnavailable("Protected authentication browser dispatch changed")
        return browser_entry_url(self.recovery.origin.rstrip("/"), row["entry_reference"])
