# Brain services

This package contains application workflows behind the typed HTTP/WebSocket endpoints. Canonical correctness lives in `app/persistence`; services coordinate policy and never become a second source of conversation truth.

## Active responsibilities

- `agent_configuration.py` publishes immutable, account-scoped Agent-config-v2 documents and tracks required versus applied revisions.
- `command_execution.py` records and delivers explicitly allow-listed commands with account/session fencing and idempotent results.
- The projection pipeline consumes normalized canonical entities regardless of passive or signer origin. Analytics, NLP, and LPG material are rebuildable and carry canonical/projection revision context.

The signer itself is not a Brain service. It runs inside Agent for exactly one authenticated read page. Agent owns history scheduling, retries, opaque upstream cursors, atomic page commits, durable jobs, and sequencing before Brain receives ordinary `ingest.delta` events or bounded `ingest.snapshot` repair frames.

## Invariants

- API endpoints validate and authorize; services apply policy; persistence owns atomic state transitions.
- No service writes raw platform response bodies or conversation payloads to diagnostic files.
- Account authority comes from the bound local session, never from a client-selected account claim.
- Desired history settings are persisted before a new Agent configuration revision is published, and effective state changes only after `config.applied`.
- Projection-unavailable analytics are `null`/unavailable, never sample or static zero fallbacks.

Only services connected through the canonical persistence and protocol-v2 application seams are authoritative. Redis, hosted graph databases, direct platform reads, and protocol-v1 message names are not runtime service dependencies.
