"""Protected previous-grant recovery admitted by the desktop coordinator.

The original local command is not an operator session. It admits only recovery
of independently retained installation evidence, under the admitted desktop
presentation and the Provider's separate fresh authentication requirement.
Ordinary enrollment and protected recovery never share an authority owner.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from uuid import uuid4

from app.onboarding.coordinator import FACTS, AcceptedContext, CommandRefused, DomainView, OperationResult
from app.persistence.onboarding import require_journey
from app.provisioning.owner_effects import OwnerEffects
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import timestamp
from app.security.installation_authority_recovery import AuthorityRecoveryRefused, scope_digest

PENDING = ("accepted", "waiting", "outcome_unknown")
MAX_READS = 12
READ_DELAYS = (15, 30, 60)


class ProtectedAuthoritySetupOwner:
    """Routes only marked recovery commands; all ordinary work retains its owner.

    A disabled adapter fences any retained recovery admitted during a qualified
    run. It neither resumes protected sends nor reinterprets them as enrollment.
    """
    def __init__(self, base, store, *, recovery=None, client=None, browser=None, verifier=None):
        self.base, self.store = base, store
        self.recovery, self.client, self.browser, self.verifier = recovery, client, browser, verifier
        supplied = tuple(value is not None for value in (recovery, client, browser, verifier))
        if any(supplied) and not all(supplied):
            raise ValueError("Incomplete protected authority owner")
        self.enabled = all(supplied)
        self._effects = OwnerEffects()

    def __getattr__(self, name):
        return getattr(self.base, name)

    @staticmethod
    def handles(operation):
        return "protected_recovery_intent" in json.loads(operation["owner_scope_json"])

    def _admissions(self, connection):
        return connection.execute(
            "SELECT a.*,r.state AS source_state,r.refusal_reason,o.state AS command_state "
            "FROM protected_recovery_desktop_admissions a "
            "JOIN installation_authority_recoveries r ON r.recovery_id=a.recovery_id "
            "LEFT JOIN desktop_setup_operations o ON o.operation_id=a.operation_id "
            "ORDER BY a.created_at DESC,a.operation_id").fetchall()

    def _no_competing_work(self, connection, *, operation_id=None):
        if connection.execute("SELECT 1 FROM desktop_setup_operations WHERE state IN "
                "('accepted','waiting','outcome_unknown') AND (? IS NULL OR operation_id<>?)",
                (operation_id,operation_id)).fetchone():
            raise HostedGrantUnavailable("Competing desktop setup operation")
        if connection.execute("SELECT 1 FROM onboarding_transfer_intents WHERE expires_at>? AND phase<>'continue-ready'",
                (self.store._now().timestamp(),)).fetchone():
            raise HostedGrantUnavailable("Competing setup transfer")

    def _offer(self, connection):
        if not self.enabled:
            return None
        try:
            self._no_competing_work(connection)
            evidence = self.recovery.eligible(connection)
            return scope_digest(evidence.scope)
        except (HostedGrantUnavailable, ValueError):
            return None

    def _view(self, connection, journey_id):
        retained = self._admissions(connection)
        pending = [item for item in retained if item["command_state"] in PENDING or item["source_state"] in
            {"reserved","prepare-claimed","prepared","committed"}]
        if pending:
            row = pending[0]
            if len(pending) != 1 or row["journey_id"] != journey_id or not self.enabled:
                return {"state":"blocked"}
            source = self.recovery.row(connection,row["recovery_id"])
            if source["state"] in {"refused","admitted"}:
                return {"state":"blocked" if source["state"]=="refused" else "checking"}
            try:
                self.recovery.exact(connection,json.loads(source["request_json"]),authorize=lambda **_: None)
            except (HostedGrantUnavailable,ValueError):
                return {"state":"blocked"}
            ceremony = self.browser.journal.row(connection,row["recovery_id"])
            if source["state"]=="committed" or ceremony is not None and ceremony["committed_seen"]:
                return {"state":"checking" if source["delivery_attempts"]<3 else "blocked"}
            if source["state"] in {"reserved","prepare-claimed"} and (source["prepare_attempts"]>=2
                    or self.store._now().timestamp()>=source["created_at"]+1800):
                return {"state":"blocked"}
            if source["state"]=="prepared" and ceremony is None and (
                    self.store._now()>=timestamp(source["source_expires_at"])
                    or self.store._now().timestamp()>=source["created_at"]+1800):
                return {"state":"blocked"}
            if (ceremony is not None and ceremony["ceremony_id"] is None
                    and (row["read_attempts"]>=MAX_READS or self.store._now().timestamp()>=source["created_at"]+1860)):
                return {"state":"blocked"}
            if ceremony is not None and ceremony["ceremony_id"] is not None:
                if (ceremony["entry_reference"] is None or ceremony["cancel_claimed"]
                        or self.store._now() >= timestamp(ceremony["expires_at"])):
                    return {"state":"checking" if ceremony["committed_seen"] else "blocked"}
                if ceremony["projection_json"] is not None:
                    return {"state":"sign-in-required"}
                if row["read_attempts"]>=MAX_READS:
                    return {"state":"blocked"}
            return {"state":"preparing"}
        offer = self._offer(connection)
        if offer is not None:
            return {"state":"available","scope_digest":offer}
        if retained and retained[0]["source_state"]=="refused":
            return {"state":"blocked"}
        return None

    def blocks_setup_transfer(self, connection, journey_id):
        return self._view(connection,journey_id) is not None

    def read(self, journey_id, *, actor=None, connection=None):
        with (self.store.database.read() if connection is None else nullcontext(connection)) as current:
            view = self._view(current,journey_id)
        if view is None:
            return self.base.read(journey_id,actor=actor,connection=connection)
        # No unauthenticated operator/account data is projected. The native
        # presentation is only allowed to initiate the specific protected task.
        facts = dict.fromkeys(FACTS,"unknown")
        facts["installation"]="missing"
        actions = frozenset({"account.connect"} if view["state"]=="available" else ())
        return DomainView(facts,{"protected_recovery":view},reason="authority_unavailable" if view["state"]=="blocked" else "none",
            permitted_actions=actions)

    def admit(self, connection, journey_id, *, action, scope, actor=None):
        if "protected_recovery" not in scope:
            return self.base.admit(connection,journey_id,action=action,scope=scope,actor=actor)
        command_id=scope.get("protected_recovery_command_id")
        offer=scope["protected_recovery"]
        if (not self.enabled or action!="account.connect" or offer.get("state")!="available"
                or command_id is None or self._offer(connection)!=offer.get("scope_digest")):
            raise CommandRefused("authority_unavailable")
        require_journey(command_id)
        journey_id=journey_id or str(uuid4())
        require_journey(journey_id)
        def authorize(*,connection):
            self._no_competing_work(connection)
            # Check the original evidence without requiring a local login which
            # the expired authority cannot authorize. Reserve checks its own
            # unresolved/refused lineage separately.
            from app.persistence.installation_authority_recovery import previous_authority
            if scope_digest(previous_authority(self.store,connection).scope)!=offer["scope_digest"]:
                raise HostedGrantUnavailable("Protected recovery evidence changed")
        try:
            authorize(connection=connection)
            request=self.recovery.reserve(connection,authorize=authorize)
            now=self.store._now().timestamp()
            connection.execute("INSERT OR IGNORE INTO desktop_setup_journeys (journey_id,revision,created_at) VALUES (?,0,?)",
                (journey_id,now))
            connection.execute("INSERT INTO protected_recovery_desktop_admissions "
                "(recovery_id,operation_id,journey_id,created_at,next_read_at) VALUES (?,?,?,?,?)",
                (request["recovery_id"],command_id,journey_id,now,now))
        except (HostedGrantUnavailable,ValueError):
            raise CommandRefused("authority_unavailable") from None
        return AcceptedContext(journey_id,{"protected_recovery_intent":{
            "recovery_id":request["recovery_id"],"scope_digest":offer["scope_digest"],
            "operation_id":command_id,"journey_id":journey_id}})

    def _saved(self, connection, operation, *, require_pending=True):
        intent=json.loads(operation["owner_scope_json"]).get("protected_recovery_intent")
        if (not isinstance(intent,dict) or set(intent)!={"recovery_id","scope_digest","operation_id","journey_id"}
                or operation["action"]!="account.connect" or intent["operation_id"]!=operation["operation_id"]
                or intent["journey_id"]!=operation["journey_id"]):
            raise HostedGrantUnavailable("Protected recovery command changed")
        row=connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",
            (operation["operation_id"],)).fetchone()
        binding=connection.execute("SELECT * FROM protected_recovery_desktop_admissions WHERE recovery_id=?",
            (intent["recovery_id"],)).fetchone()
        source=connection.execute("SELECT * FROM installation_authority_recoveries WHERE recovery_id=?",
            (intent["recovery_id"],)).fetchone()
        if (row is None or binding is None or source is None or row["owner_scope_json"]!=operation["owner_scope_json"]
                or row["action"]!=operation["action"] or row["journey_id"]!=operation["journey_id"]
                or (binding["operation_id"],binding["journey_id"],source["scope_digest"])!=(
                    intent["operation_id"],intent["journey_id"],intent["scope_digest"])
                or require_pending and row["state"] not in PENDING):
            raise HostedGrantUnavailable("Protected recovery command unavailable")
        return dict(binding),dict(source)

    def _authorize(self, operation, *, connection):
        if not self.enabled:
            raise HostedGrantUnavailable("Protected recovery is disabled")
        self._saved(connection,operation)
        self._no_competing_work(connection,operation_id=operation["operation_id"])

    def observe(self, operation):
        if not self.handles(operation):
            return self.base.observe(operation)
        try:
            with self.store.database.read() as connection:
                _,source=self._saved(connection,operation,require_pending=False)
                if source["state"]=="admitted":
                    return OperationResult("completed")
                if source["state"]=="refused":
                    return OperationResult("refused","operation_expired" if source["refusal_reason"]=="expired" else "authority_unavailable")
        except (HostedGrantUnavailable,ValueError):
            return OperationResult("outcome_unknown","authority_unavailable")
        return None

    def _authority_exchange(self, operation, verb):
        authorize=lambda **kw:self._authorize(operation,**kw)
        with self.store.database.transaction() as connection:
            self._authorize(operation,connection=connection)
            _,source=self._saved(connection,operation)
            request=json.loads(source["request_json"])
            attempt=self.recovery.claim(connection,request,verb,authorize=authorize)
        if attempt is None:
            return False
        def guard():
            with self.store.database.read() as connection:
                self.recovery.guard(connection,request,verb,attempt,authorize=authorize)
        try:
            result=getattr(self.client,verb)(request,before_send=guard)
        except AuthorityRecoveryRefused as refusal:
            with self.store.database.transaction() as connection:
                self.recovery.record_refusal(connection,request,verb,attempt,refusal,authorize=authorize)
            return True
        if verb=="prepare":
            with self.store.database.transaction() as connection:
                self.recovery.record_preparation(connection,request,attempt,result,authorize=authorize)
        else:
            self.recovery.adopt_delivery(request,attempt,result,verifier=self.verifier,authorize=authorize)
        return True

    def _claim_read(self, operation):
        with self.store.database.transaction() as connection:
            self._authorize(operation,connection=connection)
            binding,_=self._saved(connection,operation)
            now=self.store._now().timestamp()
            attempts=binding["read_attempts"]
            if attempts>=MAX_READS or now<binding["next_read_at"]:
                return False
            connection.execute("UPDATE protected_recovery_desktop_admissions SET read_attempts=?,next_read_at=? WHERE recovery_id=?",
                (attempts+1,now+READ_DELAYS[min(attempts,2)],binding["recovery_id"]))
            self._authorize(operation,connection=connection)
            return True

    def _state(self, operation):
        with self.store.database.read() as connection:
            binding,source=self._saved(connection,operation)
            request=json.loads(source["request_json"])
            ceremony=self.browser.journal.row(connection,request["recovery_id"])
            child=self.browser.journal.child(connection,request["recovery_id"])
            return binding,source,request,ceremony,child

    async def execute(self, operation, *, reconcile, actor=None):
        if not self.handles(operation):
            return await self.base.execute(operation,reconcile=reconcile,actor=actor)
        outcome=self.observe(operation)
        if outcome is not None:
            return outcome
        if not self.enabled:
            return OperationResult("outcome_unknown","authority_unavailable")
        journey=operation["journey_id"]
        authorize=lambda **kw:self._authorize(operation,**kw)
        try:
            _,source,request,ceremony,child=await asyncio.to_thread(self._state,operation)
            if source["state"] in {"reserved","prepare-claimed"}:
                await self._effects.run(self._authority_exchange,operation,"prepare",journey_id=journey)
                outcome=self.observe(operation)
                if outcome is not None:
                    return outcome
                _,source,request,ceremony,child=await asyncio.to_thread(self._state,operation)
            rid=request["recovery_id"]
            if source["state"]=="committed" or ceremony is not None and ceremony["committed_seen"]:
                await self._effects.run(self._authority_exchange,operation,"deliver",journey_id=journey)
                return self.observe(operation) or OperationResult("outcome_unknown","operation_unconfirmed")
            if source["state"]!="prepared":
                return OperationResult("outcome_unknown","operation_unconfirmed")
            if ceremony is None or not ceremony["prepare_claimed"]:
                # Deterministic preparation of the exact already-admitted
                # source. No child selection or browser launch is authorized.
                await self.browser.prepare(request,journey_id=journey,authorize=authorize)
                _,source,request,ceremony,child=await asyncio.to_thread(self._state,operation)
            elif ceremony["ceremony_id"] is None or child is not None and child["open_claimed"]:
                if await asyncio.to_thread(self._claim_read,operation):
                    await self.browser.observe_ceremony(rid,journey_id=journey,authorize=authorize)
                    _,source,request,ceremony,child=await asyncio.to_thread(self._state,operation)
            if ceremony is not None and ceremony["committed_seen"]:
                await self._effects.run(self._authority_exchange,operation,"deliver",journey_id=journey)
                return self.observe(operation) or OperationResult("outcome_unknown","operation_unconfirmed")
            if (ceremony is not None and ceremony["ceremony_id"] is not None
                    and ceremony["entry_reference"] is not None and ceremony["projection_json"] is None
                    and self.store._now()<timestamp(ceremony["expires_at"])
                    and child is None and await asyncio.to_thread(self._claim_read,operation)):
                await self.browser.observe_authentication(rid,journey_id=journey,authorize=authorize)
            return OperationResult("waiting")
        except (HostedGrantUnavailable,ValueError):
            return OperationResult("outcome_unknown","operation_unconfirmed")

    def automatic_recovery(self, operation):
        if not self.handles(operation):
            return getattr(self.base,"automatic_recovery",lambda _:False)(operation)
        if not self.enabled:
            return False
        try:
            binding,source,request,ceremony,child=self._state(operation)
            with self.store.database.read() as connection:
                self.recovery.exact(connection,request,authorize=lambda **kw:self._authorize(operation,**kw))
            now=self.store._now().timestamp()
            if source["state"] in {"reserved","prepare-claimed"}:
                return source["prepare_attempts"]<2 and now<source["created_at"]+1800
            if source["state"]=="committed" or ceremony is not None and ceremony["committed_seen"]:
                return source["delivery_attempts"]<3
            if source["state"]!="prepared":
                return False
            if ceremony is None or not ceremony["prepare_claimed"]:
                return now<timestamp(source["source_expires_at"]).timestamp() and now<source["created_at"]+1800
            if binding["read_attempts"]>=MAX_READS:
                return False
            return (ceremony["ceremony_id"] is None and now<source["created_at"]+1860
                or ceremony["ceremony_id"] is not None and (child is not None and bool(child["open_claimed"])
                    or ceremony["projection_json"] is None and ceremony["entry_reference"] is not None
                    and now<timestamp(ceremony["expires_at"]).timestamp()))
        except (HostedGrantUnavailable,ValueError):
            return False

    def probe_interval(self, operation):
        if self.handles(operation):
            return None
        return getattr(self.base,"probe_interval",lambda _:None)(operation)

    def confirmation_deadline(self, operation):
        if self.handles(operation):
            return None
        return getattr(self.base,"confirmation_deadline",lambda _:None)(operation)

    def waiting_for_sign_in(self, operation):
        if self.handles(operation):
            return False
        return getattr(self.base,"waiting_for_sign_in",lambda _:False)(operation)

    async def reconcile_hint(self, operation):
        if self.handles(operation):
            return None
        method=getattr(self.base,"reconcile_hint",None)
        return None if method is None else await method(operation)

    def settled(self, operation, result):
        if not self.handles(operation):
            method=getattr(self.base,"settled",None)
            if method is not None:
                method(operation,result)

    async def stop(self):
        self._effects.close_admission()
        await self._effects.drain()
        if self.browser is not None:
            self.browser._effects.close_admission()
            await self.browser._effects.drain()
        await self.base.stop()
