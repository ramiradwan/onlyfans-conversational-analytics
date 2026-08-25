# Architecture decision records

ADRs record durable architecture decisions and proposals. Accepted ADRs are authoritative for the parts of the system they cover.

## Add a decision

Start from [the ADR template](template.md). Use the next number after the highest existing ADR. Do not renumber historical ADRs or fill old numbering gaps.

## Accepted

- [ADR 0001: Agent owns raw ingestion](0001-agent-owned-raw-ingestion.md)
- [ADR 0002: Brain owns derived presence](0002-brain-derived-presence.md)
- [ADR 0003: Bind role and account identity to each socket](0003-immutable-socket-identity.md)
- [ADR 0004: Use durable delivery and explicit resynchronization](0004-durable-reconnect-resync.md)
- [ADR 0005: Brain owns versioned Agent configuration](0005-agent-configuration-versioning.md)
- [ADR 0006: Use the canonical communication matrix](0006-canonical-communication-matrix.md)
- [ADR 0008: Separate hosted provisioning from local runtime authentication](0008-production-authentication.md)
- [ADR 0009: Use a local-first runtime and persistence boundary](0009-local-first-topology-and-persistence.md)
- [ADR 0010: Use bounded account-scoped history acquisition](0010-signer-history-acquisition-and-bounded-state.md)

## Proposed

- [ADR 0012: Define internal Brain boundaries](0012-brain-internal-boundaries.md)
- [ADR 0013: Define conversational-analytics scope](0013-conversational-analytics-scope.md)
- [ADR 0014: Define the local authorization foundation](0014-local-authorization-foundation.md)
- [ADR 0015: Define the non-expiring capability licence profile](0015-non-expiring-capability-licence-profile.md)
- [ADR 0016: Define the single-execution permit profile](0016-single-execution-permit-profile.md)
- [ADR 0017: Select packaged boot mode from runtime configuration](0017-configuration-selected-boot-modes.md)

## Superseded

- [ADR 0007: Use a static authentication ticket for local development](0007-stub-auth-for-dev.md) — superseded by ADR 0008.
