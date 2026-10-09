"""Receiving workflow proof; the relay has no authority without the local session."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.persistence.onboarding import require_journey
from app.security.initial_handoff import canonical, _decode, _object, timestamp
from app.security.hosted_grants import HostedGrantUnavailable

ENTRY_COOKIE = "__Host-provisioning_transfer_entry"
PROFILE = "urn:bridge-clean:onboarding-transfer:v1"
PROOF_PROFILE = "urn:bridge-clean:onboarding-proof:v1"
CONTINUATION_PROFILE = "urn:bridge-clean:onboarding-continuation:v1"
HEADERS = {"Cache-Control": "no-store, private", "Referrer-Policy": "no-referrer"}


def continuation(value: object, now) -> dict:
    if (not isinstance(value, dict) or set(value) != {"profile", "reference", "return_target", "expires_at"}
            or value["profile"] != CONTINUATION_PROFILE or value["return_target"] != "desktop-setup"):
        raise ValueError("Invalid continuation")
    _decode(value["reference"])
    if not 0 < (timestamp(value["expires_at"]) - now).total_seconds() <= 1800:
        raise ValueError("Expired continuation")
    return value


class DesktopSetupTransfer:
    def __init__(self, enrollment) -> None:
        self.enrollment = enrollment
        self.store = enrollment.store
        self.journeys = enrollment.journeys
        parsed = urlsplit(enrollment.hosted_start_url)
        if parsed.scheme != "https" or parsed.path != "/public/onboarding/setup/start" or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("Invalid hosted setup destination")
        self.hosted_origin = f"https://{parsed.netloc}"
        self.receive_url = self.hosted_origin + "/public/onboarding/setup/receive"
        self.return_url = self.hosted_origin + "/public/onboarding/setup"
        self._entry_key = secrets.token_bytes(32)

    def _intent(self, journey_id: str) -> dict | None:
        with self.store.database.read() as connection:
            row = connection.execute("SELECT * FROM onboarding_transfer_intents WHERE journey_id=?", (journey_id,)).fetchone()
            return None if row is None or row["expires_at"] <= self.store._now().timestamp() else dict(row)

    async def prepare(self, journey_id: str, setup_code: object) -> dict:
        if not isinstance(setup_code, str) or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{12}", setup_code):
            raise ValueError("Invalid setup code")
        row = self.journeys.get(journey_id)
        if row is None or row["kind"] != "initial-enrollment":
            raise ValueError("Journey expired")
        previous = self._intent(journey_id)
        if previous is not None and json.loads(previous["request_json"])["setup_code"] == setup_code:
            request = json.loads(previous["request_json"])
        else:
            if row["state"] not in {"new", "preparing", "prepare-unknown", "waiting", "transfer"} or row["scope_json"] is not None:
                raise ValueError("Initial enrollment is already in progress")
            await self.enrollment.pause_for_transfer(journey_id)
            key = self.enrollment.client.key.ensure_ready()
            public = json.loads(key.public_key_jwk)
            request = {"profile": PROFILE, "operation_id": self.journeys.new_operation(), "purpose": "resume-onboarding",
                "setup_code": setup_code, "destination": {"kind": "desktop", "destination_id": row["installation_id"],
                "public_key": {name: public[name] for name in ("crv", "kty", "x", "y")}}}
            with self.store.database.transaction() as connection:
                connection.execute("DELETE FROM onboarding_transfer_intents WHERE expires_at<=?", (self.store._now().timestamp(),))
                connection.execute("INSERT INTO onboarding_transfer_intents (journey_id,request_json,previous_state,expires_at) VALUES (?,?,?,?) ON CONFLICT(journey_id) DO UPDATE SET request_json=excluded.request_json,continuation_json=NULL,phase='prepared',result_json=NULL",
                    (journey_id, canonical(request).decode(), previous["previous_state"] if previous else row["state"],
                     min(self.store._now().timestamp()+1800, __import__('datetime').datetime.fromisoformat(row["expires_at"]).timestamp())))
        return {"journey_id": journey_id, "request": request, "hosted_start_url": self.receive_url}

    def sign(self, journey_id: str, request: object, challenge: object, relay_request: Request) -> dict:
        row = self.journeys.require_current(journey_id)
        if row["kind"] != "initial-enrollment":
            raise ValueError("Setup context is unavailable")
        intent = self._intent(journey_id)
        if intent is None or canonical(request).decode() != intent["request_json"]:
            raise ValueError("Transfer intent changed")
        self._challenge(request,challenge)
        self._consume_relay(relay_request, journey_id, {"journey_id": journey_id, "request": request, "challenge": challenge}, proof=challenge["challenge"])
        key = self.enrollment.client.key.ensure_ready()
        public = json.loads(key.public_key_jwk)
        if {name: public[name] for name in ("crv", "kty", "x", "y")} != request["destination"]["public_key"]:
            raise ValueError("Transfer destination changed")
        fields = (_decode(challenge["challenge"]), b"POST", b"/v1/onboarding/transfers:redeem", hashlib.sha256(canonical(request)).digest(),
            b"transfer-redeem", _decode(key.installation_key_jkt), b"urn:bridge-clean:commercial-control-plane:onboarding")
        message = b"BRIDGE-CLEAN-ONBOARDING-PROOF-V1\0" + b"".join(len(field).to_bytes(4, "big") + field for field in fields)
        proof = self.enrollment.client.key.sign_challenge(message)
        if proof.installation_key_id != key.installation_key_id or proof.algorithm != "ES256" or len(proof.signature) != 64:
            raise ValueError("Transfer proof unavailable")
        if self.store._now() >= timestamp(challenge["expires_at"]):
            raise ValueError("Transfer proof expired")
        return {"challenge": challenge["challenge"], "signature": base64.urlsafe_b64encode(proof.signature).rstrip(b"=").decode()}

    def _challenge(self,request,challenge) -> None:
        if (not isinstance(challenge, dict) or set(challenge) != {"profile", "challenge", "request_digest", "expires_at"}
                or challenge["profile"] != PROOF_PROFILE or challenge["request_digest"] != hashlib.sha256(canonical(request)).hexdigest()
                or not 0 < (timestamp(challenge["expires_at"]) - self.store._now()).total_seconds() <= 60):
            raise ValueError("Transfer proof expired")
        _decode(challenge["challenge"])

    async def stage(self, request: Request):
        if request.url.query or request.headers.getlist("origin") != [self.hosted_origin] or request.headers.get("host") != "bridge.localhost:17871":
            raise HTTPException(400, "Invalid setup relay")
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            raise HTTPException(400, "Invalid setup relay")
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 8192:
                raise HTTPException(400, "Invalid setup relay")
        try:
            pairs = parse_qsl(raw.decode("ascii"), strict_parsing=True, max_num_fields=3, encoding="ascii", errors="strict", keep_blank_values=True)
            body = dict(pairs)
            if len(body) != len(pairs) or set(body) not in ({"journey_id", "setup_code"}, {"journey_id", "request", "challenge"}, {"journey_id", "continuation"}):
                raise ValueError("Invalid relay")
            require_journey(body["journey_id"])
            current = self.journeys.get(body["journey_id"])
            if current is None or current["kind"] != "initial-enrollment":
                raise ValueError("Unknown journey")
            if "setup_code" in body:
                if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{12}", body["setup_code"]):
                    raise ValueError("Invalid setup code")
            elif "continuation" in body:
                body["continuation"] = continuation(_object(body["continuation"]), self.store._now())
                if self._intent(body["journey_id"]) is None:
                    raise ValueError("Missing receiving intent")
            else:
                body["request"], body["challenge"] = _object(body["request"]), _object(body["challenge"])
                intent = self._intent(body["journey_id"])
                if intent is None or canonical(body["request"]).decode() != intent["request_json"]:
                    raise ValueError("Transfer intent changed")
                self._challenge(body["request"],body["challenge"])
            nonce = secrets.token_urlsafe(32)
            with self.store.database.transaction() as connection:
                connection.execute("DELETE FROM onboarding_transfer_relays WHERE expires_at<=?", (self.store._now().timestamp(),))
                if connection.execute("SELECT count(*) FROM onboarding_transfer_relays").fetchone()[0] >= 256:
                    raise ValueError("Relay capacity")
                connection.execute("INSERT INTO onboarding_transfer_relays (nonce_digest,journey_id,payload_json,expires_at) VALUES (?,?,?,?)",
                    (hashlib.sha256(nonce.encode()).hexdigest(), body["journey_id"], canonical(body).decode(), self.store._now().timestamp()+60))
            token = nonce + "." + base64.urlsafe_b64encode(hmac.digest(self._entry_key, b"public-setup-relay-v1\0"+nonce.encode(), "sha256")).rstrip(b"=").decode()
        except (ValueError, UnicodeError, HostedGrantUnavailable):
            raise HTTPException(400, "Invalid setup relay") from None
        response = RedirectResponse("/provisioning#journey="+body["journey_id"], status_code=303, headers=HEADERS)
        response.set_cookie(ENTRY_COOKIE, token, max_age=60, secure=True, httponly=True, samesite="lax", path="/")
        return response

    def _relay(self, request: Request, journey_id: str):
        token = request.cookies.get(ENTRY_COOKIE)
        if not token:
            return None
        nonce, signature = token.split(".")
        _decode(nonce)
        if not hmac.compare_digest(hmac.digest(self._entry_key, b"public-setup-relay-v1\0"+nonce.encode(), "sha256"), _decode(signature)):
            raise ValueError("Invalid relay")
        with self.store.database.read() as connection:
            row = connection.execute("SELECT * FROM onboarding_transfer_relays WHERE nonce_digest=?", (hashlib.sha256(nonce.encode()).hexdigest(),)).fetchone()
        if row is None or row["journey_id"] != journey_id or row["expires_at"] <= self.store._now().timestamp():
            raise ValueError("Invalid relay")
        return dict(row)

    def _consume_relay(self, request: Request, journey_id: str, payload: dict, *, proof: str | None = None) -> None:
        relay = self._relay(request, journey_id)
        if relay is None or relay["payload_json"] != canonical(payload).decode():
            raise ValueError("Setup relay changed")
        with self.store.database.transaction() as connection:
            result = connection.execute("UPDATE onboarding_transfer_relays SET consumed_at=? WHERE nonce_digest=? AND consumed_at IS NULL AND expires_at>?",
                (self.store._now().timestamp(), relay["nonce_digest"], self.store._now().timestamp()))
            if result.rowcount != 1:
                raise ValueError("Setup relay already handled")
            if proof is not None:
                digest = hashlib.sha256(proof.encode()).hexdigest()
                if connection.execute("SELECT 1 FROM onboarding_transfer_proofs WHERE journey_id=? AND challenge_digest=?", (journey_id,digest)).fetchone():
                    raise ValueError("Setup proof already handled")
                if connection.execute("SELECT count(*) FROM onboarding_transfer_proofs WHERE journey_id=?", (journey_id,)).fetchone()[0] >= 128:
                    raise ValueError("Setup proof capacity")
                connection.execute("INSERT INTO onboarding_transfer_proofs VALUES (?,?)", (journey_id,digest))
                connection.execute("UPDATE onboarding_transfer_intents SET phase='proof-unknown' WHERE journey_id=?", (journey_id,))
            elif "continuation" in payload:
                connection.execute("UPDATE onboarding_transfer_intents SET phase='continuing' WHERE journey_id=?", (journey_id,))

    def context(self, request: Request, session) -> dict:
        try:
            journey = self.journeys.get(session.journey_id)
            # Stale entry cookies cannot rewind an initial owner that has moved on.
            if journey is None or journey["scope_json"] is not None or journey["state"] == "completed":
                return {"state": "none"}
            intent = self._intent(session.journey_id)
            try:
                relay = self._relay(request, session.journey_id)
            except (ValueError, UnicodeError, HostedGrantUnavailable):
                if intent is None or intent["phase"] not in {"proof-unknown", "continuing", "continue-ready"}:
                    raise
                relay = None
            if relay is not None and relay["consumed_at"] is None:
                body = json.loads(relay["payload_json"])
                result = {**body, "csrf_token": session.csrf_token}
                if "setup_code" in body:
                    return {**result, "state": "code_entry"}
                if intent is None:
                    raise ValueError("Transfer intent expired")
                if "continuation" in body:
                    return {**result, "state": "continue"}
                if canonical(body["request"]).decode() != intent["request_json"]:
                    raise ValueError("Transfer intent changed")
                with self.store.database.read() as connection:
                    used = connection.execute("SELECT 1 FROM onboarding_transfer_proofs WHERE journey_id=? AND challenge_digest=?", (session.journey_id,hashlib.sha256(body["challenge"]["challenge"].encode()).hexdigest())).fetchone()
                if used is None:
                    self._challenge(body["request"], body["challenge"])
                    return {**result, "state": "proof", "hosted_return_url": self.return_url}
            if intent is not None and intent["result_json"] is not None:
                return {"state": "continue_ready", "journey_id": session.journey_id, "result": json.loads(intent["result_json"])}
            if intent is not None and intent["phase"] in {"proof-unknown", "continuing"}:
                return {"state": "unconfirmed", "journey_id": session.journey_id, "hosted_return_url": self.return_url}
            return {"state": "none"}
        except (ValueError, UnicodeError, HostedGrantUnavailable):
            raise HTTPException(409, "Setup relay is unconfirmed") from None

    def save_continuation_result(self, journey_id: str, result: dict) -> None:
        with self.store.database.transaction() as connection:
            connection.execute("UPDATE onboarding_transfer_intents SET phase='continue-ready',result_json=? WHERE journey_id=?", (canonical(result).decode(),journey_id))

    def adopt_continuation(self, journey_id: str, value: dict) -> None:
        continuation(value, self.store._now())
        intent = self._intent(journey_id)
        if intent is None:
            raise ValueError("Transfer intent expired")
        with self.store.database.transaction() as connection:
            connection.execute("UPDATE onboarding_transfer_intents SET continuation_json=? WHERE journey_id=?", (canonical(value).decode(), journey_id))
        row = self.journeys.get(journey_id)
        if row is None:
            raise ValueError("Journey expired")
        if row["state"] == "transfer":
            self.journeys.update(journey_id, state=intent["previous_state"])


def install_routes(application, sessions, transfer: DesktopSetupTransfer):
    def scoped_session(request,session):
        sessions.require_initial_context(session)
        if request.url.query or request.headers.getlist("x-onboarding-journey") not in ([],[session.journey_id]):
            raise HTTPException(403,"Setup journey changed")
        return session
    @application.post("/provisioning", include_in_schema=False)
    async def entry(request: Request):
        return await transfer.stage(request)

    @application.get("/api/v1/provisioning/setup-transfer/context", include_in_schema=False)
    async def read_context(request: Request):
        session = scoped_session(request,sessions.require_session(request))
        value = transfer.context(request, session)
        response = JSONResponse(value, headers=HEADERS)
        if value["state"] in {"none", "unconfirmed", "continue_ready"}:
            response.delete_cookie(ENTRY_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    async def body(request: Request, expected: set[str]) -> tuple[object,dict]:
        session = scoped_session(request,sessions.require_mutation(request))
        if request.headers.get("content-type", "").split(";",1)[0] != "application/json":
            raise HTTPException(400, "Invalid setup request")
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw)>8192:
                raise HTTPException(400, "Invalid setup request")
        try:
            value = _object(raw.decode())
            if set(value)!=expected or session.journey_id is None:
                raise ValueError("Invalid request")
            return session,value
        except (ValueError,HostedGrantUnavailable):
            raise HTTPException(400,"Invalid setup request") from None

    def failed(message: str):
        response=JSONResponse({"detail": message},status_code=409,headers=HEADERS)
        response.delete_cookie(ENTRY_COOKIE,path="/",secure=True,httponly=True,samesite="lax")
        return response

    @application.post("/api/v1/provisioning/setup-transfer/prepare", include_in_schema=False)
    async def prepare(request: Request):
        session,value=await body(request,{"setup_code"})
        try:
            result=await transfer.prepare(session.journey_id,value["setup_code"])
            if request.cookies.get(ENTRY_COOKIE):
                transfer._consume_relay(request,session.journey_id,{"journey_id":session.journey_id,"setup_code":value["setup_code"]})
        except (ValueError,HostedGrantUnavailable):
            return failed("Setup is unconfirmed")
        response=JSONResponse(result,headers=HEADERS)
        response.delete_cookie(ENTRY_COOKIE,path="/",secure=True,httponly=True,samesite="lax")
        return response

    @application.post("/api/v1/provisioning/setup-transfer/sign", include_in_schema=False)
    async def sign(request: Request):
        session,value=await body(request,{"request","challenge"})
        try:
            result=transfer.sign(session.journey_id,value["request"],value["challenge"],request)
        except (ValueError,HostedGrantUnavailable):
            return failed("Setup proof is unconfirmed")
        response=JSONResponse(result,headers=HEADERS)
        response.delete_cookie(ENTRY_COOKIE,path="/",secure=True,httponly=True,samesite="lax")
        return response

    @application.post("/api/v1/provisioning/setup-transfer/continue", include_in_schema=False)
    async def continue_setup(request: Request):
        session,value=await body(request,{"continuation"})
        try:
            row=transfer.journeys.get(session.journey_id)
            if row is None or row["scope_json"] is not None or row["state"] == "completed":
                raise ValueError("Initial setup already resumed")
            transfer._consume_relay(request,session.journey_id,{"journey_id":session.journey_id,"continuation":value["continuation"]})
            transfer.adopt_continuation(session.journey_id,value["continuation"])
            result=await transfer.enrollment.prepare(session.journey_id)
            result["continuation_reference"]=value["continuation"]["reference"]
            transfer.save_continuation_result(session.journey_id,result)
        except (ValueError,HostedGrantUnavailable):
            return failed("Setup is unconfirmed")
        response=JSONResponse(result,headers=HEADERS)
        response.delete_cookie(ENTRY_COOKIE,path="/",secure=True,httponly=True,samesite="lax")
        return response
