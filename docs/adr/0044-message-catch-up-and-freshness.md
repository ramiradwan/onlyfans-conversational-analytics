# ADR 0044: Message catch-up and freshness

Status: Proposed

## Context

Passive ingestion establishes when activity was observed. It cannot establish that capture remained uninterrupted. A durable gap boundary is required before an account can claim current message coverage.

## Decision

The canonical database owns a per-account observation ledger, leased checks and daily request counters. A completed history generation seeds an open gap at its completion time. An account without that baseline remains never checked.

Session lease loss, worker replacement, observation loss, dropped captures, process restart and capture-authority changes invalidate continuity at their recorded boundaries. A channel replacement within the lease window preserves continuity. Ticket issuance and channel credential retirement are bookkeeping rather than capture-authority changes.

The authenticated companion channel accepts `capture.state.report` and `history.check.begin` using the current configuration ticket. Reports are deduplicated by worker and sequence. A runnable account with completed initial history can acquire a catch-up grant at most once per ten minutes. Renewal keeps its check identity. The daily cap suspends a check without abandonment until the next day. Other lease expiry abandons it.

Catch-up completion is accepted only for the active check after its final source sequence and reconciled inventory commit. A blind check never closes a gap. An intervening epoch leaves the gap open at the pending boundary. Snapshot evidence replay has no effect on check completion or the ledger.

An hourly canary evaluates stored heads and newly inserted probe messages, excluding heads newer than two minutes before its grant. Optional `ingest.delta.check_id` attributes inserts only to the named account's active check. Missing, unknown, finished and foreign check identities never prevent canonical ingestion and never attribute inserts to a canary. Overlapping canonical messages remain no-ops.

The status precedence is paused, checking, never checked, behind and current. Current requires a closed gap and live observation without a time expiry. Only bridges advertising `state.catchup_freshness` receive its snapshot field and replacement delta. Session and heartbeat shapes remain unchanged. The existing live freshness and readiness signals use this status only for accounts whose latest agent advertises `history.catchup.v1`.

Transport bindings sequence published state revisions so observation-only replacements can accompany projection snapshots. Canonical and projection revisions retain their existing meanings. Diagnostics expose aggregate daily counters without account identifiers.

## Consequences

Observation reports expire after 90 seconds. Defaults permit 1,000 automatic pages per account per UTC day, leases of 300 seconds and grants of at most 200 pages. Catch-up uses a 15-minute skew margin. Initial-history pacing is unchanged.

The extension and frontend contracts accept these shapes. Their producers and consumers are delivered separately. Existing strict bridges receive their original field sets.

## Validation

The ledger tests exercise trigger boundaries, grant preconditions, blind and stale epochs, cap suspension, lease expiry, canary evidence, source fencing and replay. A simulated day rotates the same worker's channel every 900 seconds and permits only 24 canaries. Mutation controls demonstrate that unsafe closure, attribution and negotiation rules fail these tests.
