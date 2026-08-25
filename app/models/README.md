<!-- CODE-VERIFY: Verify protocol locations, shared fixtures, history models, version rules, and public contract behavior against source before editing. -->

# Models and public contracts

Public data contracts are grouped by system boundary instead of collected in one model module.

## WebSocket protocol

`app/protocol/` defines protocol version 2 for Agent and Bridge communication, including role-specific messages, ingestion frames, bounded Bridge state, and Agent configuration.

Python, Agent, and Bridge implementations validate the shared fixtures under `shared/fixtures/protocol/v2/`.

## History HTTP models

`history.py` defines request and response models for history settings, paged messages, readiness state, and partial analytics.

The authenticated transport supplies the creator account. Clients do not choose an arbitrary account through request parameters.

## Internal models

Service and persistence modules may define internal Pydantic models or dataclasses for their own work. An internal record does not become a public contract merely because it uses a schema type.

## Contract rules

- Reject unknown fields at external boundaries where the contract is strict.
- Keep protocol, configuration, extension, signer, browser-storage, and SQLite versions independent.
- Keep raw observations and canonical facts separate from derived projections.
- Represent unavailable or partial analysis explicitly instead of substituting sample values.
- Coordinate public protocol changes with the relevant architecture decision and cross-language fixtures.
