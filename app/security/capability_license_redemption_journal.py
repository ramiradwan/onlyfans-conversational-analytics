"""Exact opaque-redemption requests and protected results in Brain's auth database."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping

import httpx
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from app.core.resource_paths import resource_path
from app.persistence.database import AuthSQLite
from app.security.hosted_grants import TransportResponse
from contracts.loader import verify_snapshot_integrity


class RedemptionJournalError(RuntimeError):
    """The original delivery record is unavailable or has changed."""


def canonical(value: Mapping[str, object]) -> str:
    result = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(result.encode("utf-8")) > 8192:
        raise RedemptionJournalError("Delivery journal document exceeds its limit")
    return result


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def provider_origin(value: str) -> str:
    url = httpx.URL(value)
    if (url.scheme != "https" or not url.host or url.username or url.password
            or url.query or url.fragment or url.path not in {"", "/"}):
        raise ValueError("Delivery requires an HTTPS Provider origin")
    return str(url.copy_with(path="/"))


@dataclass(frozen=True)
class PendingRedemption:
    continuation_digest: str
    scope_json: str
    request: dict
    idempotency_key: str
    response: TransportResponse | None
    terminal_result: str | None = None


@lru_cache(maxsize=1)
def _error_validator():
    root = resource_path("contracts")
    verify_snapshot_integrity(root)
    documents = [json.loads((root / "schemas/common/v1" / name).read_text("utf-8"))
        for name in ("definitions.schema.json", "error.schema.json")]
    registry = Registry().with_resources((item["$id"], Resource.from_contents(item)) for item in documents)
    return Draft202012Validator(documents[1], registry=registry)


def _unique_members(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate error member")
        value[key] = item
    return value


def terminal_refusal(response: TransportResponse) -> str | None:
    if response.status_code not in {404, 410} or len(response.body) > 8192:
        return None
    try:
        value = json.loads(response.body.decode("utf-8"), object_pairs_hook=_unique_members)
        _error_validator().validate(value)
        return {
            (410, "expired", "continuation_expired"): "redemption_expired",
            (404, "not_found", "continuation_invalid"): "redemption_invalid",
        }.get((response.status_code, value["code"], value["detail"]))
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        return None


class SQLiteRedemptionJournal:
    def __init__(self, database: AuthSQLite, origin: str):
        self.database, self.origin = database, provider_origin(origin)

    def pending(self, continuation: str, scope: Mapping[str, object]) -> PendingRedemption | None:
        with self.database.read() as connection:
            row = connection.execute("SELECT * FROM capability_license_redemption_journal "
                "WHERE provider_origin=? AND continuation_digest=?", (self.origin, digest(continuation))).fetchone()
        return None if row is None else self._row(row, scope)

    def prepare(self, continuation: str, scope: Mapping[str, object], request: dict,
                idempotency_key: str) -> PendingRedemption:
        encoded = canonical(request)
        with self.database.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO capability_license_redemption_journal "
                "(provider_origin,continuation_digest,scope_json,request_json,request_digest,idempotency_key) "
                "VALUES (?,?,?,?,?,?)", (self.origin, digest(continuation), canonical(scope), encoded,
                    digest(encoded), idempotency_key))
            row = connection.execute("SELECT * FROM capability_license_redemption_journal "
                "WHERE provider_origin=? AND continuation_digest=?", (self.origin, digest(continuation))).fetchone()
            if row is None:
                raise RedemptionJournalError("Redemption request was not persisted")
            return self._row(row, scope)

    def complete(self, pending: PendingRedemption, response: TransportResponse) -> None:
        if response.status_code not in {200, 201} or len(response.body) > 8192:
            raise RedemptionJournalError("Invalid protected redemption result")
        encoded = response.body.decode("utf-8")
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM capability_license_redemption_journal "
                "WHERE provider_origin=? AND continuation_digest=?", (self.origin, pending.continuation_digest)).fetchone()
            if row is None or row["idempotency_key"] != pending.idempotency_key:
                raise RedemptionJournalError("Original redemption request changed")
            if row["terminal_json"] is not None:
                raise RedemptionJournalError("Terminal redemption cannot become a protected success")
            if row["response_json"] is not None:
                if row["response_json"] != encoded or row["response_status"] != response.status_code:
                    raise RedemptionJournalError("Protected redemption result changed")
                return
            connection.execute("UPDATE capability_license_redemption_journal "
                "SET response_status=?,response_json=?,response_digest=? "
                "WHERE provider_origin=? AND continuation_digest=?", (response.status_code, encoded,
                    digest(encoded), self.origin, pending.continuation_digest))

    def refuse(self, pending: PendingRedemption, response: TransportResponse) -> str | None:
        result = terminal_refusal(response)
        if result is None:
            return None
        encoded = response.body.decode("utf-8")
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM capability_license_redemption_journal "
                "WHERE provider_origin=? AND continuation_digest=?", (self.origin, pending.continuation_digest)).fetchone()
            if row is None or row["idempotency_key"] != pending.idempotency_key or row["response_json"] is not None:
                raise RedemptionJournalError("Terminal refusal cannot replace protected delivery")
            if row["terminal_json"] is not None:
                return self._row(row, json.loads(pending.scope_json)).terminal_result
            connection.execute("UPDATE capability_license_redemption_journal "
                "SET terminal_status=?,terminal_json=?,terminal_digest=? "
                "WHERE provider_origin=? AND continuation_digest=?", (response.status_code, encoded,
                    digest(encoded), self.origin, pending.continuation_digest))
        return result

    def bind_delivery(self, continuation, scope, operation, reference):
        pending = self.pending(continuation, scope)
        if pending is None or pending.response is None or operation not in {"activate", "finalize"} or not reference:
            raise RedemptionJournalError("Protected redemption must precede issuance")
        with self.database.transaction() as connection:
            connection.execute("UPDATE capability_license_redemption_journal "
                "SET delivery_operation=?,authority_reference_id=? WHERE provider_origin=? AND continuation_digest=?",
                (operation, reference, self.origin, pending.continuation_digest))

    def completed_delivery(self, operation, reference, scope, delivery) -> tuple[str, str] | None:
        with self.database.read() as connection:
            rows = connection.execute("SELECT * FROM capability_license_redemption_journal "
                "WHERE provider_origin=? AND delivery_operation=? AND authority_reference_id=? "
                "AND completed_reference_id IS NOT NULL", (self.origin, operation, reference)).fetchall()
        completed = set()
        for row in rows:
            pending = self._row(row, scope)
            try:
                if pending.response is None or pending.terminal_result is not None:
                    raise ValueError()
                document = json.loads(pending.response.body, object_pairs_hook=_unique_members)
                if (set(document) != {"profile", "redemption_id", "result", "authorization_state", "delivery"}
                        or document["profile"] != "urn:bridge-clean:capability-license-redemption:v1"
                        or document["redemption_id"] != pending.request["request"]["redemption_id"]
                        or document["authorization_state"] != "delivery_available"
                        or (pending.response.status_code, document["result"]) not in {
                            (201, "accepted"), (200, "already_completed")}
                        or document["delivery"] != delivery
                        or not isinstance(row["completed_reference_id"], str)
                        or not row["completed_reference_id"]
                        or not isinstance(row["response_object_digest"], str)
                        or len(row["response_object_digest"]) != 64):
                    raise ValueError()
                completed.add((row["completed_reference_id"], row["response_object_digest"]))
            except (KeyError, ValueError, TypeError, UnicodeError):
                raise RedemptionJournalError("Completed delivery association is invalid") from None
        if len(completed) > 1:
            raise RedemptionJournalError("Completed delivery association is ambiguous")
        return next(iter(completed), None)

    @staticmethod
    def _row(row, scope) -> PendingRedemption:
        try:
            if row["scope_json"] != canonical(scope) or digest(row["request_json"]) != row["request_digest"]:
                raise ValueError()
            request = json.loads(row["request_json"])
            if not isinstance(request, dict):
                raise ValueError()
            response = None
            if row["response_json"] is not None:
                if digest(row["response_json"]) != row["response_digest"]:
                    raise ValueError()
                response = TransportResponse(row["response_status"], row["response_json"].encode("utf-8"), "application/json")
            terminal = None
            if row["terminal_json"] is not None:
                if response is not None or digest(row["terminal_json"]) != row["terminal_digest"]:
                    raise ValueError()
                terminal = terminal_refusal(TransportResponse(row["terminal_status"],
                    row["terminal_json"].encode("utf-8"), "application/json"))
                if terminal is None:
                    raise ValueError()
            return PendingRedemption(row["continuation_digest"], row["scope_json"], request,
                row["idempotency_key"], response, terminal)
        except (KeyError, ValueError, TypeError, UnicodeError):
            raise RedemptionJournalError("Invalid redemption journal record") from None
