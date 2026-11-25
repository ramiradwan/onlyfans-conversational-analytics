"""Brain-owned use of a committed signed account-passkey binding."""
from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from datetime import timedelta

from app.persistence.account_passkey import AccountPasskeyBinding, AccountPasskeyBindings
from app.persistence.auth import (
    AuthenticationStateError, BridgeSessionIssue, RevocationKey, RevocationScopeType,
    SavedDataOwner, SQLiteAuthenticationStore, WebAuthnCredential,
)
from app.security.account_passkey import ACCESS_PROFILE, BINDING_PROFILE, WITHDRAWAL_PROFILE, AccountPasskeyRefused
from app.security.account_passkey_enrollment import RegisteredPasskey
from app.security.account_passkey_local import AccountPasskeyLocalRecords
from app.security.account_passkey_records import (
    AccountPasskeyRecordVerifier, BindingScope, VerifiedPasskeyRecord, require_initial_binding,
)
from app.security.account_passkey_webauthn import binding_public_key, verify_saved_data_assertion
from app.security.account_passkey_windows import MAX_CEREMONY_SECONDS, WindowsHelloBroker
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.signed_objects import SignedObjectRejected
from app.security.webauthn import SessionAuthority
from app.security.saved_data import InitialSavedDataAccess, SavedDataSessions


class AccountPasskeyAuthority:
    """Signs in to saved data locally. Hosted reads can deliver a withdrawal."""

    def __init__(
        self, store: SQLiteAuthenticationStore, records: AccountPasskeyLocalRecords,
        verifier: AccountPasskeyRecordVerifier, *, environment: str, broker: WindowsHelloBroker,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.bindings = AccountPasskeyBindings(store)
        self.records = records
        self.verifier = verifier
        self.environment = environment
        self.broker = broker
        self._monotonic = monotonic
        self._stopping = False
        self._sync_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self.bindings.accept_trust(environment=environment, sequence=verifier.sequence)

    def adopt(
        self, registration: RegisteredPasskey, *, creator_account_id: str,
        guard: Callable[..., None],
    ) -> SavedDataOwner:
        """Adopt only the binding for the completed native and local Connect operations."""
        binding = self.records.read_binding(registration, before_send=guard)
        payload = binding.payload
        with self.store.database.transaction() as connection:
            guard(connection=connection)
            self._require_trust(connection)
            previous = self.bindings.read(connection, payload["installation_id"], payload["rp_id"], payload["credential_id"])
            retained = self.bindings.install(
                connection, creator_account_id=creator_account_id,
                installation_id=payload["installation_id"], installation_key_jkt=payload["installation_key_jkt"],
                rp_id=payload["rp_id"], credential_id=payload["credential_id"], subject=payload["sub"],
                digest=binding.digest, generation=binding.generation, record=binding.compact,
            )
            if previous is None:
                owner = retained.owner
                self.store._register_webauthn_credential(connection, WebAuthnCredential(
                    credential_id=owner.credential_id, principal_id=owner.principal_id,
                    external_issuer=retained.external_issuer, external_subject=owner.principal_id,
                    installation_id=owner.installation_id,
                    public_key=binding_public_key(payload["credential_public_key_cose"]),
                    signature_count=0, enrolled_at=self.store._now(),
                ))
        return retained.owner

    def _require_trust(self, connection) -> None:
        self.bindings.trust(connection, environment=self.environment, sequence=self.verifier.sequence)

    def _verify(self, retained: AccountPasskeyBinding, compact: str, *, profile: str) -> VerifiedPasskeyRecord:
        owner = retained.owner
        scope = BindingScope(retained.subject, owner.installation_id, owner.installation_key_jkt,
                             owner.credential_id, retained.rp_id)
        record = self.verifier.verify(
            compact, profile=profile, scope=scope, now=int(self.store._now().timestamp()),
        )
        expected_digest, expected_generation = (
            (retained.binding_digest, retained.binding_generation) if profile == BINDING_PROFILE
            else (retained.withdrawal_digest, retained.withdrawal_generation)
        )
        if record.digest != expected_digest or record.generation != expected_generation:
            raise AuthenticationStateError("Account passkey local authority changed")
        return record

    def active(self, connection) -> tuple[AccountPasskeyBinding, VerifiedPasskeyRecord] | None:
        self._require_trust(connection)
        retained = self.bindings.active(connection)
        if retained is None or retained.binding_record is None:
            return None
        return retained, self._verify(retained, retained.binding_record, profile=BINDING_PROFILE)

    def prepare_initial_access(
        self, registration: RegisteredPasskey, owner: SavedDataOwner, *, generation: str, guard,
    ) -> InitialSavedDataAccess:
        """Request authority only for the fresh enrollment's installed binding."""
        with self.store.database.read() as connection:
            guard(connection=connection)
            active = self.active(connection)
            if self._stopping or active is None or active[0].owner != owner:
                raise AuthenticationStateError("Initial saved-data binding changed")
            _, binding = active
        proof = self.records.issue_initial_access(registration, binding, before_send=guard)
        return InitialSavedDataAccess(generation, owner, proof)

    def unlock(self, sessions: SavedDataSessions, *, authorize, lifetime: int,
               generation: str | None = None) -> tuple[str, str]:
        if generation is not None and sessions.has_initial(generation):
            with self.store.database.transaction() as connection:
                authorize(connection)
                active = self.active(connection)
                if self._stopping or active is None:
                    raise AuthenticationStateError("Saved data is locked")
                retained, binding = active
                first = sessions.issue_initial(generation, retained.owner, lifetime,
                    verify=lambda access: self._initial_is_current(access, binding, retained.owner))
                if first is not None:
                    return first
        return self._verify_local(lambda owner, connection: sessions.issue(owner, lifetime), authorize=authorize)

    def _initial_is_current(
        self, access: VerifiedPasskeyRecord, binding: VerifiedPasskeyRecord, owner: SavedDataOwner,
    ) -> bool:
        try:
            verified = self.verifier.verify(
                access.compact, profile=ACCESS_PROFILE,
                scope=BindingScope(binding.payload["sub"], owner.installation_id, owner.installation_key_jkt,
                                   owner.credential_id, binding.payload["rp_id"]),
                now=int(self.store._now().timestamp()),
            )
            require_initial_binding(
                verified, binding, enrollment_id=access.payload["enrollment_id"],
                attempt_id=access.payload["attempt_id"], nonce=access.payload["initial_access_nonce"],
            )
        except SignedObjectRejected:
            return False
        return True

    def requires_native(self, credential_id: str) -> bool:
        with self.store.database.read() as connection:
            return connection.execute(
                "SELECT 1 FROM account_passkey_authority WHERE credential_id=?",
                (credential_id,),
            ).fetchone() is not None

    def sign_in(self, authority: SessionAuthority, *, authorize, current_authority, lifetime: int):
        """Current grant policy is required in addition to the local proof."""
        def issue(owner, connection):
            if ((owner.credential_id, owner.principal_id, owner.creator_account_id)
                    != (authority.credential_id, authority.principal_id, authority.creator_account_id)
                    or current_authority(connection) != authority):
                raise AuthenticationStateError("Account access changed during verification")
            return self.store._issue_bridge_session(connection, BridgeSessionIssue(
                credential_id=owner.credential_id, principal_id=owner.principal_id,
                creator_account_id=owner.creator_account_id, role=authority.role,
                expires_at=self.store._now() + timedelta(seconds=lifetime),
                grant_reference_ids=authority.grant_reference_ids,
            ))

        return self._verify_local(issue, authorize=authorize)

    def _verify_local(self, issue, *, authorize):
        """Own and consume one fresh local challenge within this invocation."""
        with self.store.database.read() as connection:
            authorize(connection)
            active = self.active(connection)
        if active is None or self._stopping:
            raise AuthenticationStateError("Saved data is locked")
        retained, record = active
        owner = retained.owner
        challenge = secrets.token_bytes(32)
        deadline = self._monotonic() + MAX_CEREMONY_SECONDS
        assertion = self.broker.assert_local(
            rp_id=retained.rp_id, credential_id=owner.credential_id, challenge=challenge,
        )
        verify_saved_data_assertion(
            assertion, rp_id=retained.rp_id, credential_id=owner.credential_id,
            public_key=binding_public_key(record.payload["credential_public_key_cose"]),
            challenge=challenge, expected_user_handle=retained.subject,
        )
        with self.store.database.transaction() as connection:
            authorize(connection)
            if self._stopping or self._monotonic() >= deadline or self.active(connection) != active:
                raise AuthenticationStateError("Saved data authority changed during verification")
            return issue(owner, connection)

    def synchronize_withdrawals(self, sessions: SavedDataSessions) -> None:
        with self.store.database.read() as connection:
            self._require_trust(connection)
            records = self.bindings.delivery_records(connection)
        for retained in records:
            if self._stopping:
                return
            try:
                self._deliver(retained, sessions)
            except (AccountPasskeyRefused, HostedGrantUnavailable, SignedObjectRejected, AuthenticationStateError):
                # An unavailable service or unverifiable record supplies no new
                # authority. Unacknowledged exact withdrawals remain durable.
                continue

    def _deliver(self, retained: AccountPasskeyBinding, sessions: SavedDataSessions) -> None:
        owner = retained.owner

        def require_current(connection) -> None:
            self._require_trust(connection)
            if self._stopping or self.bindings.read(
                connection, owner.installation_id, retained.rp_id, owner.credential_id,
            ) != retained:
                raise AuthenticationStateError("Account passkey binding changed")

        def read_guard() -> None:
            with self.store.database.read() as connection:
                require_current(connection)

        if retained.binding_record is not None:
            active = self._verify(retained, retained.binding_record, profile=BINDING_PROFILE)
            withdrawal = self.records.read_withdrawal(active, before_send=read_guard)
            with self.store.database.transaction() as connection:
                require_current(connection)
                self.bindings.withdraw(
                    connection, retained, generation=withdrawal.generation,
                    digest=withdrawal.digest, record=withdrawal.compact,
                )
                self.store._revoke_in_transaction(
                    connection, RevocationKey(RevocationScopeType.WEBAUTHN_CREDENTIAL, owner.credential_id),
                    reason="account_passkey_withdrawn",
                )
                self.store._increment_authorization_epoch(connection)
        elif retained.pending_withdrawal is not None:
            withdrawal = self._verify(retained, retained.pending_withdrawal, profile=WITHDRAWAL_PROFILE)
        else:
            return

        sessions.invalidate(owner)

        def require_applied(confirmed: VerifiedPasskeyRecord) -> None:
            if self._stopping:
                raise AuthenticationStateError("Account passkey delivery is stopping")
            with self.store.database.read() as connection:
                self._require_trust(connection)
                self.bindings.require_withdrawn(
                    connection, retained, generation=confirmed.generation, digest=confirmed.digest,
                )

        self.records.acknowledge_withdrawal(withdrawal, require_applied=require_applied)
        with self.store.database.transaction() as connection:
            self.bindings.acknowledge(
                connection, retained, generation=withdrawal.generation, digest=withdrawal.digest,
            )

    async def _synchronize(self, sessions: SavedDataSessions) -> None:
        while not self._stopping:
            try:
                await asyncio.to_thread(self.synchronize_withdrawals, sessions)
            except (AuthenticationStateError, OSError):
                pass
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=30)
            except TimeoutError:
                pass

    def start(self, sessions: SavedDataSessions) -> None:
        if self._sync_task is None:
            self._sync_task = asyncio.create_task(self._synchronize(sessions))

    async def close(self) -> None:
        self._stopping = True
        self.broker.close()
        self._wake.set()
        if self._sync_task is not None:
            await self._sync_task
