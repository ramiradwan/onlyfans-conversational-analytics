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
        scope=json.loads(operation["owner_scope_json"])
        return "protected_recovery_intent" in scope or "protected_recovery_child" in scope

    @staticmethod
    def _child_intent(operation):
        return json.loads(operation["owner_scope_json"]).get("protected_recovery_child")

    def _admissions(self, connection):
        return connection.execute(
            "SELECT a.*,r.state AS source_state,r.refusal_reason,o.state AS command_state "
            "FROM protected_recovery_desktop_admissions a "
            "JOIN installation_authority_recoveries r ON r.recovery_id=a.recovery_id "
            "LEFT JOIN desktop_setup_operations o ON o.operation_id=a.operation_id "
            "ORDER BY a.created_at DESC,a.operation_id").fetchall()

    def _no_competing_work(self, connection, *, operation_id=None, child_operation_id=None):
        if connection.execute("SELECT 1 FROM desktop_setup_operations WHERE state IN "
                "('accepted','waiting','outcome_unknown') AND (? IS NULL OR operation_id<>?) "
                "AND (? IS NULL OR operation_id<>?)",
                (operation_id,operation_id,child_operation_id,child_operation_id)).fetchone():
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
            if self._pending_child(connection,row["operation_id"]):
                return {"state":"checking" if row["read_attempts"]<MAX_READS
                    and self.store._now().timestamp()<source["created_at"]+1860 else "blocked"}
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
                    child = self.browser.journal.child(connection,row["recovery_id"])
                    projection = json.loads(ceremony["projection_json"])
                    actual = projection["authentication_attempt"]
                    # The current read, not an old child receipt, admits the next
                    # explicit gesture. The protected browser affordance has
                    # no ordinary browser operation locator.
                    if ((child is None and actual is None and projection["state"]=="selection-required")
                            or (child is not None and actual is not None
                                and actual["authentication_attempt_id"]==child["attempt_id"]
                                and child["state"]==projection["state"] and child["state"] in {"selected","retired"})):
                        return {"state":"sign-in-required","authentication":{
                            "operation_id":row["operation_id"],"browser_operation_id":None,
                            "state":projection["state"],
                            "choices":{name: name=="default" and allowed for name,allowed in projection["choices"].items()},
                            "actions":{"select":bool(projection["actions"]["select"] and
                                (child is None or child["sequence"]<32)),
                                "cancel":bool(projection["actions"]["cancel"]),
                                "open_browser":bool(child is not None and child["state"]=="selected"
                                    and not child["open_claimed"] and projection["actions"]["open_browser"]),
                                "switch_account":False},
                            "authentication_attempt":actual}}
                    return {"state":"checking"}
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
        authentication=view.get("authentication")
        actions = frozenset({"account.connect"} if view["state"]=="available" else
            {"auth."+verb for verb in ("select","cancel") if authentication is not None and authentication["actions"][verb]})
        return DomainView(facts,{"protected_recovery":view},reason="authority_unavailable" if view["state"]=="blocked" else "none",
            permitted_actions=actions)

    def admit(self, connection, journey_id, *, action, scope, actor=None):
        if "protected_recovery" not in scope:
            return self.base.admit(connection,journey_id,action=action,scope=scope,actor=actor)
        if action in {"auth.select","auth.cancel"}:
            return self._admit_child(connection,journey_id,action=action,scope=scope)
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

    def _selected_browser_child(self,connection,parent_id,attempt_id):
        rows=connection.execute("SELECT * FROM desktop_setup_operations WHERE action='auth.select' "
            "AND state='completed' AND json_extract(owner_scope_json,'$.protected_recovery_child.parent_operation_id')=? "
            "AND json_extract(owner_scope_json,'$.protected_recovery_child.request.authentication_attempt_id')=? LIMIT 2",
            (parent_id,attempt_id)).fetchall()
        if len(rows)!=1:
            raise HostedGrantUnavailable("Protected authentication selection unavailable")
        selection=dict(rows[0])
        intent,parent,binding,source=self._saved_child(connection,selection,require_pending=False)
        self._authorize(parent,connection=connection)
        child=self.browser.journal.child(connection,binding["recovery_id"])
        if (intent["verb"]!="select" or source["state"]!="prepared" or not self._was_claimed(intent,child)
                or child["state"]!="selected"):
            raise HostedGrantUnavailable("Protected authentication selection changed")
        return selection,parent,binding,source,child

    def admit_browser_launch(self,connection,session,*,tasks,parent_id,attempt_id):
        if not self.enabled:
            raise HostedGrantUnavailable("Protected recovery is disabled")
        selection,parent,binding,source,child=self._selected_browser_child(connection,parent_id,attempt_id)
        if session.journey_id!=parent["journey_id"]:
            raise HostedGrantUnavailable("Protected browser journey changed")
        authorize=lambda **kw:self._authorize(parent,**kw)
        rid=binding["recovery_id"]
        ceremony=self.browser.journal.row(connection,rid)
        if ceremony is None:
            raise HostedGrantUnavailable("Protected ceremony unavailable")
        claim=self.browser.journal.claim_browser_open(connection,rid,authorize=authorize)
        if claim["attempt_id"]!=attempt_id:
            raise HostedGrantUnavailable("Protected authentication attempt changed")
        # The native launcher receives only a local one-use code. The hosted
        # URL is recomputed and checked at consumption, never returned here.
        return tasks.prepare_protected(connection,session,selection=selection,recovery_id=rid,
            attempt_id=attempt_id,deadline=timestamp(ceremony["expires_at"]).timestamp())

    def resolve_browser_launch(self,connection,task,dispatch):
        if not self.enabled:
            raise HostedGrantUnavailable("Protected recovery is disabled")
        binding=connection.execute("SELECT * FROM protected_recovery_desktop_admissions WHERE recovery_id=?",
            (dispatch["recovery_id"],)).fetchone()
        if binding is None:
            raise HostedGrantUnavailable("Protected browser source unavailable")
        selection,parent,binding,source,child=self._selected_browser_child(connection,binding["operation_id"],dispatch["attempt_id"])
        if (task["operation_id"]!=selection["operation_id"] or task["journey_id"]!=parent["journey_id"]
                or task["scope_digest"]!=selection["scope_digest"] or task["source_revision"]!=selection["source_revision"]
                or task["purpose"]!="browser.authentication" or binding["recovery_id"]!=dispatch["recovery_id"]):
            raise HostedGrantUnavailable("Protected browser task changed")
        return self.browser.journal._guard_browser_open(connection,binding["recovery_id"],child["attempt_id"],
            authorize=lambda **kw:self._authorize(parent,**kw))

    def _entry_deadline(self,connection,child):
        if child is None or not child["open_claimed"]:
            return None
        now=self.store._now().timestamp()
        row=connection.execute("SELECT min(t.expires_at,p.expires_at,e.expires_at) FROM protected_recovery_browser_dispatches d "
            "JOIN desktop_browser_tasks t ON t.task_id=d.task_id JOIN desktop_presentations p ON p.generation=t.desktop_generation "
            "JOIN desktop_admission_entries e ON e.reference_id=t.task_id AND e.role='browser_task' "
            "AND e.generation=t.admission_generation "
            "WHERE d.attempt_id=? AND t.state='pending' AND t.expires_at>? AND p.state='active' AND p.expires_at>? "
            "AND e.expires_at>?",(child["attempt_id"],now,now,now)).fetchone()
        return None if row is None else row[0]

    def _entry_pending(self,connection,child):
        return self._entry_deadline(connection,child) is not None

    @staticmethod
    def _retire_browser_entry(connection,attempt_id):
        # Explicit cancellation irrevocably fences an unspent launch even if
        # the subsequent hosted cancellation has an unknown result.
        connection.execute("DELETE FROM desktop_admission_entries WHERE role='browser_task' AND reference_id IN "
            "(SELECT task_id FROM protected_recovery_browser_dispatches WHERE attempt_id=?)",(attempt_id,))
        connection.execute("UPDATE desktop_browser_tasks SET state='cancelled' WHERE state='pending' AND task_id IN "
            "(SELECT task_id FROM protected_recovery_browser_dispatches WHERE attempt_id=?)",(attempt_id,))

    def _pending_child(self,connection,parent_id):
        return connection.execute("SELECT 1 FROM desktop_setup_operations WHERE state IN "
            "('accepted','waiting','outcome_unknown') AND json_extract(owner_scope_json,"
            "'$.protected_recovery_child.parent_operation_id')=?", (parent_id,)).fetchone() is not None

    def _admit_child(self,connection,journey_id,*,action,scope):
        offer=scope["protected_recovery"]
        authentication=offer.get("authentication")
        command_id=scope.get("protected_recovery_command_id")
        verb=action.removeprefix("auth.")
        if (not self.enabled or command_id is None or journey_id is None or authentication is None
                or offer.get("state")!="sign-in-required" or not authentication["actions"][verb]
                or self._view(connection,journey_id)!=offer):
            raise CommandRefused("authority_unavailable")
        require_journey(command_id)
        parent_id=authentication["operation_id"]
        parent=connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=? AND journey_id=?",
            (parent_id,journey_id)).fetchone()
        if parent is None:
            raise CommandRefused("authority_unavailable")
        parent=dict(parent)
        try:
            binding,_=self._saved(connection,parent)
            self._authorize(parent,connection=connection)
            request=self.browser.journal.plan_authentication(connection,binding["recovery_id"],verb,
                authorize=lambda **kw:self._authorize(parent,**kw))
        except (HostedGrantUnavailable,ValueError):
            raise CommandRefused("authority_unavailable") from None
        if verb=="cancel":
            self._retire_browser_entry(connection,request["authentication_attempt_id"])
        return AcceptedContext(journey_id,{"protected_recovery_child":{
            "parent_operation_id":parent_id,"recovery_id":binding["recovery_id"],"verb":verb,
            "operation_id":command_id,"request":request}})

    def _saved_child(self, connection, operation, *, require_pending=True):
        intent=self._child_intent(operation)
        if (not isinstance(intent,dict) or set(intent)!={"parent_operation_id","recovery_id","verb","operation_id","request"}
                or intent["verb"] not in {"select","cancel"} or operation["action"]!="auth."+intent["verb"]
                or operation["operation_id"]!=intent["operation_id"]):
            raise HostedGrantUnavailable("Protected authentication command changed")
        row=connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",(operation["operation_id"],)).fetchone()
        parent=connection.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=? AND journey_id=?",
            (intent["parent_operation_id"],operation["journey_id"])).fetchone()
        if (row is None or parent is None or row["owner_scope_json"]!=operation["owner_scope_json"]
                or row["journey_id"]!=operation["journey_id"] or row["action"]!=operation["action"]
                or require_pending and row["state"] not in PENDING):
            raise HostedGrantUnavailable("Protected authentication command unavailable")
        parent=dict(parent)
        binding,source=self._saved(connection,parent,require_pending=require_pending)
        if binding["recovery_id"]!=intent["recovery_id"]:
            raise HostedGrantUnavailable("Protected authentication recovery changed")
        return intent,parent,binding,source

    def _authorize_child(self, operation, *, connection, mutation=False, require_claimed=False):
        if not self.enabled:
            raise HostedGrantUnavailable("Protected recovery is disabled")
        intent,parent,binding,_=self._saved_child(connection,operation)
        self._no_competing_work(connection,operation_id=parent["operation_id"],child_operation_id=operation["operation_id"])
        if mutation and self.store._now().timestamp()>=operation["expires_at"]:
            raise HostedGrantUnavailable("Protected authentication command expired")
        if require_claimed and not self._was_claimed(intent,self.browser.journal.child(connection,binding["recovery_id"])):
            raise HostedGrantUnavailable("Protected authentication attempt changed")

    def _child_state(self, connection, operation, *, require_pending=True):
        intent,parent,binding,source=self._saved_child(connection,operation,require_pending=require_pending)
        ceremony=self.browser.journal.row(connection,binding["recovery_id"])
        child=self.browser.journal.child(connection,binding["recovery_id"])
        return intent,parent,binding,source,ceremony,child

    @staticmethod
    def _was_claimed(intent,child):
        if child is None or child["attempt_id"]!=intent["request"]["authentication_attempt_id"]:
            return False
        if intent["verb"]=="select":
            return json.loads(child["request_json"])==intent["request"]
        return child["state"] in {"cancel-claimed","retired","admitted"}

    def _require_unclaimed(self,connection,intent,*,authorize):
        # An unrelated current child is not proof that this command never sent.
        # Require the same original actionable projection and exact plan.
        if intent["verb"]=="select" and connection.execute(
                "SELECT 1 FROM protected_recovery_authentication_children WHERE attempt_id=?",
                (intent["request"]["authentication_attempt_id"],)).fetchone():
            raise HostedGrantUnavailable("Protected authentication attempt changed")
        expected=self.browser.journal.plan_authentication(connection,intent["recovery_id"],intent["verb"],
            authorize=authorize,attempt_id=intent["request"]["authentication_attempt_id"])
        if expected!=intent["request"]:
            raise HostedGrantUnavailable("Protected authentication plan changed")

    def _observe_child(self,operation):
        if not self.enabled:
            return None
        try:
            with self.store.database.read() as connection:
                intent,_,_,source,ceremony,child=self._child_state(connection,operation,require_pending=False)
                if source["state"]=="refused":
                    return OperationResult("refused","authority_unavailable")
                if source["state"]!="admitted":
                    self.recovery.exact(connection,json.loads(source["request_json"]),authorize=lambda **_: None)
                if not self._was_claimed(intent,child):
                    return None
                state=child["state"]
                if state=="admitted":
                    return OperationResult("completed") if intent["verb"]=="select" else OperationResult("refused","operation_refused")
                # A mutation reply or exact read alone does not establish the
                # currently actionable child. A matching current read must exist.
                if ceremony is None or ceremony["projection_json"] is None:
                    return None
                projection=json.loads(ceremony["projection_json"])
                actual=projection["authentication_attempt"]
                if actual is None or actual["authentication_attempt_id"]!=child["attempt_id"] or projection["state"]!=state:
                    return None
                if state=="retired":
                    return OperationResult("completed") if intent["verb"]=="cancel" else OperationResult("refused","operation_refused")
                if state=="selected" and intent["verb"]=="select":
                    return OperationResult("completed")
        except (HostedGrantUnavailable,ValueError):
            return None
        return None

    async def _execute_child(self,operation,*,reconcile):
        outcome=self._observe_child(operation)
        if outcome is not None:
            return outcome
        if not self.enabled:
            return OperationResult("outcome_unknown","authority_unavailable")
        journey=operation["journey_id"]
        authorize=lambda **kw:self._authorize_child(operation,require_claimed=True,**kw)
        try:
            def read():
                with self.store.database.read() as connection:
                    self._authorize_child(operation,connection=connection)
                    current=self._child_state(connection,operation)
                    if not self._was_claimed(current[0],current[-1]):
                        self._require_unclaimed(connection,current[0],authorize=lambda **kw:self._authorize_child(operation,**kw))
                    return current
            intent,parent,binding,source,ceremony,child=await asyncio.to_thread(read)
            rid=binding["recovery_id"]
            if not self._was_claimed(intent,child):
                # A restart before the atomic journal claim proves the mutation
                # never began. Do not manufacture a new gesture on restart.
                if reconcile or self.store._now().timestamp()>=operation["expires_at"]:
                    return OperationResult("refused","operation_expired" if self.store._now().timestamp()>=operation["expires_at"]
                        else "operation_unconfirmed")
                await self.browser.send_admitted_authentication(rid,intent["verb"],intent["request"],
                    journey_id=journey,authorize=lambda **kw:self._authorize_child(operation,mutation=True,**kw))
            else:
                if self.store._now().timestamp()>=source["created_at"]+1860:
                    return OperationResult("outcome_unknown","operation_unconfirmed")
                if not await asyncio.to_thread(self._claim_read,parent,child_operation=operation):
                    return OperationResult("outcome_unknown","operation_unconfirmed")
                await self.browser.observe_authentication(rid,journey_id=journey,authorize=authorize,exact_child=True)
            outcome=self._observe_child(operation)
            if outcome is not None:
                return outcome
            await self.browser.observe_authentication(rid,journey_id=journey,authorize=authorize)
            return self._observe_child(operation) or OperationResult("outcome_unknown","operation_unconfirmed")
        except (HostedGrantUnavailable,ValueError):
            return OperationResult("outcome_unknown","operation_unconfirmed")

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
        if self._child_intent(operation) is not None:
            return self._observe_child(operation)
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

    def _claim_read(self, operation, *, child_operation=None):
        def authorize(connection):
            if child_operation is None:
                self._authorize(operation,connection=connection)
            else:
                self._authorize_child(child_operation,connection=connection)
        with self.store.database.transaction() as connection:
            authorize(connection)
            binding,_=self._saved(connection,operation)
            now=self.store._now().timestamp()
            attempts=binding["read_attempts"]
            if attempts>=MAX_READS or now<binding["next_read_at"]:
                return False
            connection.execute("UPDATE protected_recovery_desktop_admissions SET read_attempts=?,next_read_at=? WHERE recovery_id=?",
                (attempts+1,now+READ_DELAYS[min(attempts,2)],binding["recovery_id"]))
            authorize(connection)
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
        if self._child_intent(operation) is not None:
            return await self._execute_child(operation,reconcile=reconcile)
        outcome=self.observe(operation)
        if outcome is not None:
            return outcome
        if not self.enabled:
            return OperationResult("outcome_unknown","authority_unavailable")
        journey=operation["journey_id"]
        authorize=lambda **kw:self._authorize(operation,**kw)
        try:
            with self.store.database.read() as connection:
                if self._pending_child(connection,operation["operation_id"]):
                    return OperationResult("waiting")
            binding,source,request,ceremony,child=await asyncio.to_thread(self._state,operation)
            if source["state"] in {"reserved","prepare-claimed"}:
                await self._effects.run(self._authority_exchange,operation,"prepare",journey_id=journey)
                outcome=self.observe(operation)
                if outcome is not None:
                    return outcome
                binding,source,request,ceremony,child=await asyncio.to_thread(self._state,operation)
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
                binding,source,request,ceremony,child=await asyncio.to_thread(self._state,operation)
            elif ceremony["ceremony_id"] is None or child is not None and child["open_claimed"] and child["state"]=="selected":
                with self.store.database.read() as connection:
                    if self._entry_pending(connection,child):
                        return OperationResult("waiting")
                if await asyncio.to_thread(self._claim_read,operation):
                    if (ceremony["ceremony_id"] is not None and ceremony["entry_reference"] is not None
                            and self.store._now()<timestamp(ceremony["expires_at"])
                            and (ceremony["projection_json"] is not None or binding["read_attempts"]%2==0)):
                        # A missing child projection does not establish
                        # noncommit. Alternate original ceremony and child
                        # reads when uncertain, within the same durable budget.
                        await self.browser.observe_authentication(rid,journey_id=journey,authorize=authorize)
                    else:
                        await self.browser.observe_ceremony(rid,journey_id=journey,authorize=authorize)
                    binding,source,request,ceremony,child=await asyncio.to_thread(self._state,operation)
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
        if self._child_intent(operation) is not None:
            try:
                with self.store.database.read() as connection:
                    self._authorize_child(operation,connection=connection)
                    intent,_,binding,source,ceremony,child=self._child_state(connection,operation)
                    if source["state"]!="prepared" or ceremony is None:
                        return False
                    self.recovery.exact(connection,json.loads(source["request_json"]),
                        authorize=lambda **kw:self._authorize_child(operation,**kw))
                    if not self._was_claimed(intent,child):
                        self._require_unclaimed(connection,intent,authorize=lambda **kw:self._authorize_child(operation,**kw))
                        return True
                    return binding["read_attempts"]<MAX_READS and self.store._now().timestamp()<source["created_at"]+1860
            except (HostedGrantUnavailable,ValueError):
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
            if binding["read_attempts"]>=MAX_READS or now>=source["created_at"]+1860:
                return False
            if child is not None and child["open_claimed"] and child["state"]=="selected":
                with self.store.database.read() as connection:
                    return not self._entry_pending(connection,child)
            return (ceremony["ceremony_id"] is None
                or ceremony["ceremony_id"] is not None and child is None
                    and ceremony["projection_json"] is None and ceremony["entry_reference"] is not None
                    and now<timestamp(ceremony["expires_at"]).timestamp())
        except (HostedGrantUnavailable,ValueError):
            return False

    def probe_interval(self, operation):
        if self.handles(operation):
            return None
        return getattr(self.base,"probe_interval",lambda _:None)(operation)

    def confirmation_deadline(self, operation):
        if not self.handles(operation):
            return getattr(self.base,"confirmation_deadline",lambda _:None)(operation)
        if not self.enabled or self._child_intent(operation) is not None:
            return None
        try:
            with self.store.database.read() as connection:
                binding,_=self._saved(connection,operation)
                child=self.browser.journal.child(connection,binding["recovery_id"])
                return self._entry_deadline(connection,child)
        except (HostedGrantUnavailable,ValueError):
            return None

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
