# Models

The active public contracts are split by boundary rather than collected in one model module.

## Wire protocol

`app/protocol/` is the source of truth for protocol version 2:

- role-specific Agent and Bridge message unions;
- bounded `ingest.snapshot` begin/chunk/commit frames;
- account-scoped raw chat, message, tombstone, and coverage evidence;
- bounded Bridge conversation summaries and readiness state;
- immutable Agent configuration documents.

Every stack validates the shared fixtures in `shared/fixtures/protocol/v2/`.

## History HTTP models

`history.py` defines authenticated REST request and response models for:

- creator-controlled history settings;
- paged conversation messages;
- acquisition coverage, projection readiness, and live freshness;
- partial analytics with explicit basis, range, sample size, and revision.

Transport authority supplies the creator account. Clients do not select an account in request parameters.

## Internal models

Domain-specific internal models remain local to their owning service or persistence module. Internal records do not become wire contracts merely because they are represented with Pydantic or dataclasses.

## Contract rules

- Use strict schemas and reject unknown fields at external boundaries.
- Keep protocol, configuration, extension, IndexedDB, signer, and SQLite versions independent.
- Preserve raw observations and canonical facts separately from derived projections.
- Represent unavailable or partial analysis explicitly; do not substitute sample values.
- Add or change public operations only through an accepted architecture decision and synchronized cross-language fixtures.
