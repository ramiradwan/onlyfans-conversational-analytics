# ADR 0022: Keep activation Legal evidence append-only in the Extension

- Status: Accepted
- Date: 2026-08-30

## Context

Legal issue #5 requires Product to implement the approved activation-event composition without changing Legal schema v2. The approved composition has exactly three user/legal meanings: Software Terms acceptance, canonical risk-disclosure acknowledgment, and the later Preview/Full data-handling choice. Terms and risk actions must survive abandonment before a mode exists, while schema v2 requires `selected_mode` and therefore cannot truthfully represent that pre-mode state.

The Extension is the component that owns Preview/Full choice and optional browser permissions. Preview must remain usable without Brain. Consequently, activation evidence cannot depend on Brain availability or on the account-scoped encrypted Full-mode databases.

## Decision

The Extension owns an installation-scoped IndexedDB evidence journal named `ofca_legal_evidence_v1`.

The journal stores append-only pre-mode Terms/risk records and append-only wrappers containing unchanged schema-v2 mode-choice envelopes once a truthful Preview or Full choice exists. Records have stable UUIDv4 identifiers and a transaction/correlation identifier. Idempotent retry uses deterministic record keys so retry returns the original record and timestamp rather than generating replacement assent/acknowledgment times.

A mode wrapper retains source Terms/risk event identifiers outside the schema-v2 envelope. The envelope remains schema-v2 exact and contains the original Terms/risk timestamps. Preview to Full creates a distinct `mode_upgrade` envelope and does not mutate Preview evidence.

Revocation, downgrade and pause change current consent state outside the evidence journal and do not rewrite historical evidence. Explicit **Delete all extension data** deletes all Extension-owned IndexedDB databases, including this journal; the Legal return must state that deletion boundary.

The journal is installation-scoped because Terms/risk actions precede Full account binding and Preview has no Brain dependency. It stores Legal-action metadata and document bindings only, never conversation content, platform identifiers, credentials or authentication material.

Product vendors a byte-pinned copy of Legal's `activation-evidence.schema.json` v2 solely as a validation dependency. Legal remains authoritative. A lock identifies the Legal revision, Git blob and SHA-256. Tests and the Legal evidence bundle verify those bytes and validate generated envelopes against unchanged schema v2.

Runtime Legal instrument bindings are release inputs: controlling Legal revision, schema blob, HTTPS public origin, and exact version/rendered-SHA-256/public-route/locale values for all four instruments. Development may have no binding and therefore fail closed for new activation; a production package must not be created without valid bindings.

`Activate Software` is orchestration only: it may verify persisted Terms/risk actions and advance to mode choice, but never changes consent mode or creates schema-v2 evidence.

The consent controller remains current-state owner. `ConsentController` requires an explicit active-mode authorization dependency. Before Preview/Full entry it asks that dependency to authorize the requested mode against the persisted evidence event; resume and reconciliation likewise require persisted evidence for the active mode. Missing or inconsistent authorization fails closed. The Legal activation controller composes evidence and supplies the resulting event identifier to the consent transition; it does not replace or monkey-patch consent-controller methods.

Product execution evidence is produced by the tests and browser E2E that exercise the behavior. A single AE-01..AE-09 scenario registry identifies the executing test and required artifacts. The Legal bundle generator is packaging/verification only: it cannot report Product `PASS` unless the exact returned revision has a preserved successful scenario result and every registry-required evidence artifact. Product status remains separate from Legal acceptance status.

## Consequences

- Abandoned Terms/risk actions remain durable without inventing `selected_mode`.
- Original timestamps survive retries and later envelope composition.
- Preview/Full events are independently auditable and Preview-to-Full history remains immutable.
- Full fails closed when Legal release bindings are missing or invalid.
- Active-mode authorization is an explicit, independently testable `ConsentController` dependency rather than initialization-order-sensitive method replacement.
- Legal evidence is Extension-local data and is erased by explicit delete-all.
- Generated Legal acceptance bundles are build/CI artifacts, not committed Product history. Legal is the durable system of record for accepted evidence and attestation results.
- Future schema-v2 semantic changes, evidence retention outside delete-all, or hosted evidence sinks require new architecture and Legal decisions.
