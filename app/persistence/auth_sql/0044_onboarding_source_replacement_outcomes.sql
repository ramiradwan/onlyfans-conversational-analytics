-- A verified replacement result is not an authorization grant. Prepared
-- successors are materialized atomically with this lineage record.
CREATE TABLE onboarding_source_replacement_outcomes (
    predecessor_operation_id TEXT PRIMARY KEY
        REFERENCES onboarding_source_replacement_reservations(predecessor_operation_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('prepared','accepted','committed','commercial-reconciliation-required')),
    successor_journey_id TEXT UNIQUE,
    original_domain_operation_id TEXT,
    CHECK ((status='prepared') = (successor_journey_id IS NOT NULL)),
    CHECK ((status IN ('accepted','committed')) = (original_domain_operation_id IS NOT NULL))
);

CREATE TRIGGER onboarding_source_replacement_outcome_immutable
BEFORE UPDATE ON onboarding_source_replacement_outcomes
BEGIN
    SELECT RAISE(ABORT, 'Source replacement outcome is immutable');
END;

-- Once the provider has prepared a distinct successor, stale original
-- callbacks cannot change its predecessor into a new active source.
CREATE TRIGGER onboarding_source_replaced_journey_fence
BEFORE UPDATE ON onboarding_journeys
WHEN EXISTS (SELECT 1 FROM onboarding_source_replacement_outcomes
             WHERE predecessor_operation_id=OLD.operation_id AND status='prepared')
BEGIN
    SELECT RAISE(ABORT, 'Replaced onboarding source is immutable');
END;

CREATE TRIGGER onboarding_source_replaced_context_fence
BEFORE UPDATE ON installation_continuation_contexts
WHEN EXISTS (SELECT 1 FROM onboarding_journeys j
             JOIN onboarding_source_replacement_outcomes r ON r.predecessor_operation_id=j.operation_id
             WHERE j.journey_id=OLD.journey_id AND r.status='prepared')
BEGIN
    SELECT RAISE(ABORT, 'Replaced continuation context is immutable');
END;

-- The published replacement profiles explicitly admit a fresh-proof exact
-- retry. Bound it durably to one reconciliation attempt for the same successor.
CREATE TABLE onboarding_source_replacement_reconciliations (
    predecessor_operation_id TEXT PRIMARY KEY
        REFERENCES onboarding_source_replacement_reservations(predecessor_operation_id) ON DELETE CASCADE,
    claimed_at REAL NOT NULL
);

CREATE TRIGGER onboarding_source_replacement_reconciliation_immutable
BEFORE UPDATE ON onboarding_source_replacement_reconciliations
BEGIN
    SELECT RAISE(ABORT, 'Source replacement reconciliation is immutable');
END;
