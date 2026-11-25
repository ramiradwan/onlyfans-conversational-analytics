"""Durable installation-owned account-passkey authority and withdrawal fence."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.persistence.auth import AuthenticationStateError, SavedDataOwner, SQLiteAuthenticationStore


@dataclass(frozen=True, slots=True)
class AccountPasskeyBinding:
    owner: SavedDataOwner = field(repr=False)
    rp_id: str
    subject: str = field(repr=False)
    external_issuer: str
    binding_digest: str
    binding_generation: int
    binding_record: str | None = field(repr=False)
    withdrawal_generation: int | None
    withdrawal_digest: str | None
    pending_withdrawal: str | None = field(repr=False)


def _record(row) -> AccountPasskeyBinding:
    return AccountPasskeyBinding(
        owner=SavedDataOwner(
            credential_id=row["credential_id"],
            principal_id=row["principal_id"],
            creator_account_id=row["creator_account_id"],
            installation_id=row["installation_id"],
            installation_key_id=row["installation_key_id"],
            installation_key_jkt=row["installation_key_jkt"],
            organization_id=row["organization_id"],
            account_passkey_binding=row["binding_digest"],
        ),
        rp_id=row["rp_id"],
        subject=row["subject"],
        external_issuer=row["external_issuer"],
        binding_digest=row["binding_digest"],
        binding_generation=row["binding_generation"],
        binding_record=row["binding_record"],
        withdrawal_generation=row["withdrawal_generation"],
        withdrawal_digest=row["withdrawal_digest"],
        pending_withdrawal=row["pending_withdrawal"],
    )


class AccountPasskeyBindings:
    def __init__(self, store: SQLiteAuthenticationStore) -> None:
        self.store = store

    def trust(self, connection, *, environment: str, sequence: int) -> None:
        row = connection.execute(
            "SELECT sequence FROM account_passkey_trust_sequence WHERE environment=?", (environment,)
        ).fetchone()
        if row is not None and row["sequence"] > sequence:
            raise AuthenticationStateError("Account passkey trust sequence rolled back")

    def accept_trust(self, *, environment: str, sequence: int) -> None:
        with self.store.database.transaction() as connection:
            self.trust(connection, environment=environment, sequence=sequence)
            connection.execute(
                """
                INSERT INTO account_passkey_trust_sequence(environment, sequence) VALUES (?, ?)
                ON CONFLICT(environment) DO UPDATE SET sequence=excluded.sequence
                WHERE sequence < excluded.sequence
                """,
                (environment, sequence),
            )

    def has_retained_authority(self, connection) -> bool:
        return connection.execute(
            "SELECT EXISTS(SELECT 1 FROM account_passkey_authority)"
        ).fetchone()[0] == 1

    def active(self, connection) -> AccountPasskeyBinding | None:
        row = connection.execute(
            """
            SELECT p.* FROM account_passkey_authority p
            JOIN installation_key_reference k
            ON k.singleton=1 AND k.activated_at IS NOT NULL
               AND k.installation_key_id=p.installation_key_id
               AND k.installation_key_jkt=p.installation_key_jkt
            WHERE p.binding_record IS NOT NULL
            """
        ).fetchall()
        if len(row) != 1:
            return None
        binding = _record(row[0])
        if not self.store.account_passkey_owner_current(
            connection, binding.owner, external_issuer=binding.external_issuer,
        ):
            return None
        return binding

    def read(self, connection, installation_id: str, rp_id: str, credential_id: str) -> AccountPasskeyBinding | None:
        row = connection.execute(
            """
            SELECT * FROM account_passkey_authority
            WHERE installation_id=? AND rp_id=? AND credential_id=?
            """,
            (installation_id, rp_id, credential_id),
        ).fetchone()
        return None if row is None else _record(row)

    def delivery_records(self, connection) -> tuple[AccountPasskeyBinding, ...]:
        rows = connection.execute(
            """
            SELECT p.* FROM account_passkey_authority p
            JOIN installation_key_reference k
            ON k.singleton=1 AND k.activated_at IS NOT NULL
               AND k.installation_key_id=p.installation_key_id
               AND k.installation_key_jkt=p.installation_key_jkt
            WHERE p.binding_record IS NOT NULL OR p.pending_withdrawal IS NOT NULL
            """
        ).fetchall()
        return tuple(_record(row) for row in rows)

    def install(self, connection, *, creator_account_id: str, installation_id: str, installation_key_jkt: str,
                rp_id: str, credential_id: str, subject: str, digest: str, generation: int, record: str) -> AccountPasskeyBinding:
        owner, issuer = self.store.account_passkey_owner(
            connection, creator_account_id=creator_account_id, installation_id=installation_id,
            installation_key_jkt=installation_key_jkt, credential_id=credential_id, binding_digest=digest,
        )
        candidate = AccountPasskeyBinding(owner, rp_id, subject, issuer, digest, generation, record, None, None, None)
        retained = self.read(connection, installation_id, rp_id, credential_id)
        if retained is not None:
            if retained != candidate:
                raise AuthenticationStateError("Account passkey binding generation changed")
            return retained
        if self.active(connection) is not None or connection.execute(
            "SELECT 1 FROM account_passkey_authority WHERE installation_id=? AND binding_record IS NOT NULL",
            (installation_id,),
        ).fetchone() is not None:
            raise AuthenticationStateError("Installation already has an account passkey binding")
        connection.execute(
            """
            INSERT INTO account_passkey_authority(
              installation_id,rp_id,credential_id,subject,installation_key_id,installation_key_jkt,
              creator_account_id,principal_id,external_issuer,organization_id,
              binding_digest,binding_generation,binding_record
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                installation_id, rp_id, credential_id, subject, owner.installation_key_id, installation_key_jkt,
                creator_account_id, owner.principal_id, candidate.external_issuer, owner.organization_id,
                digest, generation, record,
            ),
        )
        return candidate

    def withdraw(self, connection, binding: AccountPasskeyBinding, *,
                 generation: int, digest: str, record: str) -> None:
        current = self.read(connection, binding.owner.installation_id, binding.rp_id, binding.owner.credential_id)
        if current is None or current.binding_digest != binding.binding_digest:
            raise AuthenticationStateError("Account passkey withdrawal no longer matches the binding")
        if current.withdrawal_generation == generation and current.withdrawal_digest == digest:
            return
        if current != binding or current.binding_record is None or generation <= current.binding_generation:
            raise AuthenticationStateError("Account passkey withdrawal generation changed")
        connection.execute(
            """
            UPDATE account_passkey_authority
            SET binding_record=NULL,withdrawal_generation=?,withdrawal_digest=?,pending_withdrawal=?
            WHERE installation_id=? AND rp_id=? AND credential_id=?
            """,
            (generation, digest, record, binding.owner.installation_id, binding.rp_id, binding.owner.credential_id),
        )

    def require_withdrawn(self, connection, binding: AccountPasskeyBinding, *, generation: int, digest: str) -> None:
        current = self.read(connection, binding.owner.installation_id, binding.rp_id, binding.owner.credential_id)
        if (current is None or current.binding_record is not None or current.owner != binding.owner
                or current.withdrawal_generation != generation or current.withdrawal_digest != digest):
            raise AuthenticationStateError("Account passkey withdrawal is not durably effective")

    def acknowledge(self, connection, binding: AccountPasskeyBinding, *, generation: int, digest: str) -> None:
        self.require_withdrawn(connection, binding, generation=generation, digest=digest)
        connection.execute(
            """
            UPDATE account_passkey_authority SET pending_withdrawal=NULL
            WHERE installation_id=? AND rp_id=? AND credential_id=?
            """,
            (binding.owner.installation_id, binding.rp_id, binding.owner.credential_id),
        )
