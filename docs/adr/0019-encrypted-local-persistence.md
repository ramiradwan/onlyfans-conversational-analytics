# ADR 0019: Encrypt local Brain persistence with device-bound keys

- Status: accepted

## Decision

Encrypt every Brain-local store that can contain creator identity, conversation
data, authentication state, delivery state, or derived analytics.

Brain uses SQLCipher for `auth.sqlite3`, `canonical.sqlite3`,
`projections.sqlite3`, and `analytics-projections.sqlite3`. A random 256-bit
installation master key is protected with Windows DPAPI in CurrentUser scope.
HKDF-SHA256 derives independent keys for the `auth`, `canonical`, `projection`,
and `analytics-projection` scopes. The key file and every database sidecar keep
the existing owner-only ACL policy. Brain refuses startup if DPAPI, SQLCipher,
the wrapped master key, or the correct database key is unavailable; it never
falls back to standard unencrypted SQLite.

Online and pre-migration database backups remain encrypted. User-selected
backups carry a separate CurrentUser-protected key sidecar and are re-encrypted
under the destination installation key during restore. No backup operation may
materialize a plaintext SQLite file.

This decision covers Brain SQLite databases and their backups. Browser-extension
storage has a separate lifecycle and is not represented as protected by this
SQLCipher boundary.

This decision supersedes the **At-rest protection, export, and backup** section
of ADR 0009. ADR 0009 remains authoritative for topology, database authority,
durability, and restore coordination.

## Why

- Conversation text and local authentication state are sensitive even when no
  hosted service receives them.
- Full-disk encryption and ACLs do not cover copied database or backup files.
- One DPAPI-wrapped master avoids embedding or remotely escrowing a secret,
  while per-store derivation prevents one database key from opening another.
- A fail-closed driver boundary makes accidental plaintext database creation a
  testable runtime invariant.

## Consequences

- Production Brain packages are Windows-specific and must bundle the pinned
  SQLCipher driver and native library.
- DPAPI-bound data is recoverable only by the same Windows user context unless
  an explicit, separately designed portable export is used.
- Existing plaintext development databases are not opened automatically. They
  must be discarded or migrated through a one-time, offline, reviewed tool.
- Backup verification requires the protected key sidecar; losing either file
  makes that backup unusable.
- SQLCipher encryption protects data at rest, not data visible to an authorized
  running process or a compromised unlocked OS session.
- Non-Windows CI may use only the explicit `ENVIRONMENT=test` master key path;
  that path is rejected in every other environment.

## Related

- [ADR 0001: Agent owns raw ingestion](0001-agent-owned-raw-ingestion.md)
- [ADR 0008: Separate hosted provisioning from local runtime authentication](0008-production-authentication.md)
- [ADR 0009: Use a local-first runtime and persistence boundary](0009-local-first-topology-and-persistence.md)
- [ADR 0010: Use bounded account-scoped history acquisition](0010-signer-history-acquisition-and-bounded-state.md)
