"""Ephemeral hosted activation delivery; the local session remains independent."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.persistence.onboarding import OnboardingJourneyStore, require_journey
from app.provisioning.events import events
from app.provisioning.session import PROVISIONING_HOST
from app.security.capability_license_composition import CapabilityLicenseDeliveryReceipt
from app.security.initial_handoff import _decode
from app.security.hosted_grants import HostedGrantUnavailable

PATH = "/api/v1/onboarding/activation-return"
ENTRY_PATH = "/provisioning/activation"
COOKIE = "__Host-onboarding_activation_return"
HEADERS = {"Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer"}


@dataclass
class _Entry:
    entry_id: str
    journey_id: str
    scope: tuple[str, str, str, str, str]
    digest: str
    continuation: str | None
    expires_at: float
    retire_at: float
    state: str = "waiting"


class _ApprovalPending(ValueError):
    """The exact local selection exists but its signed approval is not ready."""


class ActivationReturn:
    def __init__(self, store, *, hosted_url: str, redeem, authorize, runtime: bool = False):
        parsed = urlsplit(hosted_url)
        if (parsed.scheme != "https" or parsed.path != "/public/onboarding/setup"
                or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Invalid hosted activation destination")
        self.store = store
        self.journeys = OnboardingJourneyStore(store)
        self.hosted_origin = "https://" + parsed.netloc
        self.redeem = redeem
        self.authorize = authorize
        self.runtime = runtime
        self._entries: dict[str, _Entry] = {}
        self._lock = Lock()
        self._tasks: set[asyncio.Task] = set()

    def _immutable_scope(self, journey_id):
        """Resolve saved coordinates without treating delivery as approval."""
        row = self.journeys.require_current(journey_id)
        key = self.store.installation_key_reference()
        if key is None:
            raise ValueError("Local installation is unavailable")
        if row["kind"] == "registered-continuation":
            from app.persistence.installation_continuation import InstallationContinuationStore
            value = InstallationContinuationStore(self.store).require_current(journey_id)
            target = value["target"]
            if (value["state"] in {"expired", "revoked"} or value["provider_state"] in {"expired", "revoked"}
                    or (value["provider_expires_at"] is not None
                        and datetime.fromisoformat(value["provider_expires_at"].replace("Z", "+00:00")) <= self.store._now())
                    or target.binding_revoked_at is not None or target.candidate.state.value not in {"pending", "approved"}
                    or target.key != key):
                raise ValueError("Setup is not approved")
            scope = (target.candidate.organization_id, target.candidate.installation_id,
                key.installation_key_id, key.installation_key_jkt, target.candidate.creator_account_id)
        else:
            value = json.loads(row["scope_json"])
            candidate = self.store.provisioning_candidate(value["association_request_id"])
            if (row["kind"] != "initial-enrollment" or row["state"] not in {"waiting", "completing", "unknown", "completed"}
                    or value["installation_id"] != row["installation_id"]
                    or value["installation_key_jkt"] != key.installation_key_jkt
                    or (candidate is not None and (candidate.state.value not in {"pending", "approved"}
                        or candidate.installation_id != row["installation_id"]
                        or candidate.organization_id != value["organization_id"]
                        or candidate.creator_account_id != value["intended_creator_id"]
                        or candidate.onboarding_transaction_id != value["onboarding_transaction_id"]))):
                raise ValueError("Setup is not approved")
            scope = (value["organization_id"], value["installation_id"], key.installation_key_id,
                key.installation_key_jkt, value["intended_creator_id"])
        return scope

    def _scope(self, journey_id):
        scope = self._immutable_scope(journey_id)
        row = self.journeys.require_current(journey_id)
        if row["kind"] == "registered-continuation":
            from app.persistence.installation_continuation import InstallationContinuationStore
            value = InstallationContinuationStore(self.store).require_current(journey_id)
            if (value["state"] != "completed" or value["provider_state"] != "approved"
                    or value["provider_expires_at"] is None or value["target"].candidate.state.value != "approved"):
                raise _ApprovalPending("Setup approval is pending")
        else:
            candidate = self.store.provisioning_candidate(json.loads(row["scope_json"])["association_request_id"])
            if row["state"] != "completed" or candidate is None or candidate.state.value != "approved":
                raise _ApprovalPending("Setup approval is pending")
        return scope

    def _expire_entry(self, entry):
        now = self.store._now().timestamp()
        if entry.expires_at <= now or entry.retire_at <= now:
            entry.continuation = None
            if entry.state in {"waiting", "ready"}:
                entry.state = "unconfirmed"

    def _cleanup(self):
        now = self.store._now().timestamp()
        for token, entry in tuple(self._entries.items()):
            self._expire_entry(entry)
            if entry.retire_at <= now:
                del self._entries[token]

    def _expire_secret(self, token: str):
        with self._lock:
            entry = self._entries.get(token)
            if entry is not None:
                entry.continuation = None
                if entry.state in {"waiting", "ready"}:
                    entry.state = "unconfirmed"
        events.publish()

    async def stage(self, request: Request):
        if (request.url.query or request.headers.getlist("host") != [PROVISIONING_HOST]
                or request.headers.getlist("origin") != [self.hosted_origin]
                or request.headers.getlist("sec-fetch-mode") != ["navigate"]
                or request.headers.get("sec-fetch-dest") not in {None, "document"}
                or request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded"):
            raise HTTPException(400, "Invalid activation return")
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 512:
                raise HTTPException(400, "Invalid activation return")
        try:
            pairs = parse_qsl(raw.decode("ascii"), strict_parsing=True, max_num_fields=2,
                encoding="ascii", errors="strict", keep_blank_values=True)
            body = dict(pairs)
            if len(pairs) != 2 or set(body) != {"journey_id", "activation_continuation"}:
                raise ValueError("Invalid activation return")
            journey = require_journey(body["journey_id"])
            continuation = body["activation_continuation"]
            if not re.fullmatch(r"clr1\.[A-Za-z0-9_-]{43}", continuation):
                raise ValueError("Invalid activation continuation")
            _decode(continuation[5:])
            scope = self._immutable_scope(journey)
            row = self.journeys.require_current(journey)
            retire_at = datetime.fromisoformat(row["expires_at"]).timestamp()
            deadline = min(self.store._now().timestamp() + 300, retire_at)
            digest = hashlib.sha256(continuation.encode()).hexdigest()
            with self._lock:
                self._cleanup()
                previous = next(((token, entry) for token, entry in self._entries.items()
                    if entry.journey_id == journey and entry.digest == digest), None)
                if previous is not None:
                    token, entry = previous
                    deadline = entry.expires_at
                    retire_at = entry.retire_at
                    if entry.scope != scope:
                        raise ValueError("Activation scope changed")
                else:
                    if len(self._entries) >= 128 or any(entry.journey_id == journey and entry.state in {"waiting", "ready", "pending", "checking"}
                            for entry in self._entries.values()):
                        raise ValueError("Activation is already pending")
                    token = secrets.token_urlsafe(32)
                    self._entries[token] = _Entry(str(uuid4()), journey, scope, digest, continuation, deadline, retire_at)
                    asyncio.get_running_loop().call_later(max(0, deadline-self.store._now().timestamp()), self._expire_secret, token)
        except (ValueError, TypeError, KeyError, HostedGrantUnavailable):
            raise HTTPException(400, "Invalid activation return") from None
        destination = "/" if self.runtime else "/provisioning"
        response = RedirectResponse(destination + "#journey=" + journey, status_code=303, headers=HEADERS)
        # The nonauthorizing receipt locator outlives the erased secret only
        # until this journey expires, so a late return can report uncertainty.
        response.set_cookie(COOKIE, token, max_age=max(1, int(retire_at-self.store._now().timestamp())),
            secure=True, httponly=True, samesite="lax", path="/")
        return response

    def _current(self, request: Request, *, mutation: bool):
        if request.url.query or request.headers.getlist("host") != [PROVISIONING_HOST]:
            raise HTTPException(400, "Invalid activation context")
        values = request.headers.getlist("x-onboarding-journey")
        try:
            if len(values) != 1:
                raise ValueError("Invalid journey")
            journey = require_journey(values[0])
            self.journeys.require_current(journey)
        except ValueError:
            raise HTTPException(409, "Activation context is unavailable") from None
        creator = self.authorize(request, journey, mutation)
        token = request.cookies.get(COOKIE)
        with self._lock:
            self._cleanup()
            entry = self._entries.get(token) if isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", token) else None
        if entry is not None:
            try:
                if entry.journey_id != journey or entry.scope != self._immutable_scope(journey) or (creator is not None and creator != entry.scope[4]):
                    raise ValueError("Activation scope changed")
            except (ValueError, TypeError, KeyError):
                with self._lock:
                    entry.continuation = None
                    entry.state = "unconfirmed"
                raise HTTPException(409, "Activation context is unavailable") from None
        return journey, token, entry

    def _refresh(self, request, entry, *, mutation):
        # Called under the consume lock. Scope and session reads can block;
        # the secret and original journey deadlines are checked after both.
        try:
            if entry.state in {"waiting", "ready"}:
                try:
                    if self._scope(entry.journey_id) != entry.scope:
                        raise ValueError("Activation scope changed")
                    entry.state = "ready"
                except _ApprovalPending:
                    entry.state = "waiting"
            creator = self.authorize(request, entry.journey_id, mutation)
            if creator is not None and creator != entry.scope[4]:
                raise ValueError("Activation scope changed")
        except (ValueError, TypeError, KeyError):
            entry.continuation = None
            entry.state = "unconfirmed"
            raise HTTPException(409, "Activation context is unavailable") from None
        finally:
            self._expire_entry(entry)

    def read(self, request: Request):
        journey, token, entry = self._current(request, mutation=False)
        with self._lock:
            if entry is not None:
                self._refresh(request, entry, mutation=False)
            state = ("unconfirmed" if entry.state == "pending" else entry.state) if entry is not None else "none" if token is None else "unconfirmed"
        return JSONResponse({"state": state, "journey_id": journey,
            "entry_id": None if entry is None else entry.entry_id}, headers=HEADERS)

    async def submit(self, request: Request):
        journey, _, entry = self._current(request, mutation=True)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise HTTPException(400, "Invalid activation request")
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 16:
                raise HTTPException(400, "Invalid activation request")
        if bytes(raw).strip() != b"{}":
            raise HTTPException(400, "Invalid activation request")
        journey, _, entry = self._current(request, mutation=True)
        identifiers = request.headers.getlist("x-onboarding-activation-entry")
        if entry is None or identifiers != [entry.entry_id]:
            raise HTTPException(409, "Activation entry changed")
        with self._lock:
            self._refresh(request, entry, mutation=True)
            if entry is None or entry.state != "ready" or entry.continuation is None:
                return JSONResponse({"state": "unconfirmed" if entry is None or entry.state == "pending" else entry.state, "journey_id": journey,
                    "entry_id": None if entry is None else entry.entry_id}, headers=HEADERS)
            secret = entry.continuation
            entry.continuation = None
            entry.state = "pending"
        async def deliver():
            try:
                result = await run_in_threadpool(self.redeem, continuation=secret, expected_scope=entry.scope[:4])
                state = "checking" if isinstance(result, CapabilityLicenseDeliveryReceipt) else "unconfirmed"
            except Exception:
                state = "unconfirmed"
            with self._lock:
                entry.state = state
            events.publish()
        task = asyncio.create_task(deliver())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # The server-owned task survives a lost browser reply. This endpoint
        # never repeats a consumed continuation; readiness supplies the result.
        await asyncio.shield(task)
        return JSONResponse({"state": entry.state, "journey_id": journey, "entry_id": entry.entry_id}, headers=HEADERS)


def install_routes(router, owner, *, dependencies=None):
    @router.post(ENTRY_PATH, include_in_schema=False, dependencies=dependencies)
    async def activation_return(request: Request):
        return await owner().stage(request)

    @router.get(PATH, include_in_schema=False, dependencies=dependencies)
    async def activation_context(request: Request):
        return owner().read(request)

    @router.post(PATH, include_in_schema=False, dependencies=dependencies)
    async def activate_return(request: Request):
        return await owner().submit(request)
