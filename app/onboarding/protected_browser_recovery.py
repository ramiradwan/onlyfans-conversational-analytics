"""Single-send effects for the retained protected recovery presentation.

Runtime integration must supply the existing admitted desktop command guard
and journey ID. No renderer or ordinary ceremony can call this owner directly.
Every mutation is claimed transactionally before a single network effect.
Unknown replies are observed only through explicit noncreating read methods.
"""
from __future__ import annotations

from threading import RLock

from app.provisioning.owner_effects import OwnerEffects
from app.persistence.onboarding import require_journey
from app.security.hosted_grants import HostedGrantUnavailable


class ProtectedBrowserRecoveryEffects:
    def __init__(self, journal, ceremony_client, authentication_client):
        self.journal = journal
        self.store = journal.store
        self.ceremony_client = ceremony_client
        self.authentication_client = authentication_client
        self._effects = OwnerEffects()
        self._lock = RLock()

    async def _run(self, function, *args, journey_id, protected, **kwargs):
        require_journey(journey_id)
        return await self._effects.run(function, *args, journey_id=journey_id, protected=protected, **kwargs)

    def _ceremony(self, recovery_id, verb, request, *, authorize):
        def guard():
            with self.store.database.read() as connection:
                self.journal.guard_ceremony(connection, recovery_id, verb, request, authorize=authorize)
        with self.store.database.read() as connection:
            row = self.journal.guard_ceremony(connection, recovery_id, verb, request, authorize=authorize)
        result = self.ceremony_client.execute(verb, request, before_send=guard, expires_at=row["expires_at"])
        with self.store.database.transaction() as connection:
            return self.journal.record_ceremony(connection, recovery_id, verb, request, result, authorize=authorize)

    def _authentication(self, recovery_id, verb, request, *, authorize):
        def guard():
            with self.store.database.read() as connection:
                self.journal.guard_authentication(connection, recovery_id, verb, request, authorize=authorize)
        with self.store.database.read() as connection:
            row, _ = self.journal.guard_authentication(connection, recovery_id, verb, request, authorize=authorize)
        result = self.authentication_client.execute(verb, request, before_send=guard, expires_at=row["expires_at"])
        with self.store.database.transaction() as connection:
            return self.journal.record_authentication(connection, recovery_id, verb, request, result, authorize=authorize)

    def _prepare(self, request, *, authorize):
        recovery_id = request["recovery_id"]
        with self._lock:
            with self.store.database.transaction() as connection:
                source, _ = self.journal.recovery.exact(connection, request, authorize=authorize)
                if source["state"] != "prepared":
                    raise HostedGrantUnavailable("Protected recovery source unavailable")
                if self.journal.row(connection, recovery_id) is None:
                    self.journal.reserve(connection, request, authorize=authorize)
                command = self.journal.claim_prepare(connection, recovery_id, authorize=authorize)
            if command is None:
                # Lost preparation: read the original ID; never repeat prepare.
                return self._observe_ceremony(recovery_id, authorize=authorize)
            return self._ceremony(recovery_id, "prepare", command, authorize=authorize)

    async def prepare(self, request, *, journey_id, authorize):
        return await self._run(self._prepare, request, journey_id=journey_id, protected=True, authorize=authorize)

    def _observe_ceremony(self, recovery_id, *, authorize):
        with self._lock:
            with self.store.database.read() as connection:
                request = self.journal.read_request(connection, recovery_id, authorize=authorize)
            return self._ceremony(recovery_id, "read", request, authorize=authorize)

    async def observe_ceremony(self, recovery_id, *, journey_id, authorize):
        return await self._run(self._observe_ceremony, recovery_id, journey_id=journey_id, protected=False, authorize=authorize)

    def _cancel_ceremony(self, recovery_id, *, authorize):
        with self._lock:
            with self.store.database.transaction() as connection:
                request = self.journal.claim_cancel(connection, recovery_id, authorize=authorize)
            return self._ceremony(recovery_id, "cancel", request, authorize=authorize)

    async def cancel_ceremony(self, recovery_id, *, journey_id, authorize):
        return await self._run(self._cancel_ceremony, recovery_id, journey_id=journey_id, protected=True, authorize=authorize)

    def _observe_authentication(self, recovery_id, *, authorize, exact_child):
        with self._lock:
            with self.store.database.read() as connection:
                request = self.journal.auth_read(connection, recovery_id, authorize=authorize, exact_child=exact_child)
            return self._authentication(recovery_id, "read", request, authorize=authorize)

    async def observe_authentication(self, recovery_id, *, journey_id, authorize, exact_child=False):
        return await self._run(self._observe_authentication, recovery_id, journey_id=journey_id, protected=False,
            authorize=authorize, exact_child=exact_child)

    def _select(self, recovery_id, *, authorize):
        with self._lock:
            with self.store.database.transaction() as connection:
                request = self.journal.claim_select(connection, recovery_id, authorize=authorize)
            return self._authentication(recovery_id, "select", request, authorize=authorize)

    async def select(self, recovery_id, *, journey_id, authorize):
        return await self._run(self._select, recovery_id, journey_id=journey_id, protected=True, authorize=authorize)

    def _cancel_child(self, recovery_id, *, authorize):
        with self._lock:
            with self.store.database.transaction() as connection:
                request = self.journal.claim_child_cancel(connection, recovery_id, authorize=authorize)
            return self._authentication(recovery_id, "cancel", request, authorize=authorize)

    async def cancel_child(self, recovery_id, *, journey_id, authorize):
        return await self._run(self._cancel_child, recovery_id, journey_id=journey_id, protected=True, authorize=authorize)

    def _dispatch(self, recovery_id, *, authorize, dispatch):
        if not callable(dispatch):
            raise HostedGrantUnavailable("Protected browser task owner unavailable")
        with self._lock:
            with self.store.database.transaction() as connection:
                claim = self.journal.claim_browser_open(connection, recovery_id, authorize=authorize)
            # Only this live effect can spend the fresh local dispatch claim.
            # A crash or failed launch does not reopen it. The callback must
            # route through the existing owned-browser-task machinery.
            def guard():
                with self.store.database.read() as connection:
                    url = self.journal._guard_browser_open(connection, recovery_id, claim["attempt_id"], authorize=authorize)
                if url != claim["url"]:
                    raise HostedGrantUnavailable("Protected browser dispatch changed")
            guard()
            return dispatch(claim["attempt_id"], claim["url"], before_dispatch=guard)

    async def dispatch(self, recovery_id, *, journey_id, authorize, dispatch):
        return await self._run(self._dispatch, recovery_id, journey_id=journey_id, protected=True,
            authorize=authorize, dispatch=dispatch)
