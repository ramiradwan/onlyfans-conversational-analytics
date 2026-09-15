CREATE TABLE capability_license_pending_deliveries (
    operation_type TEXT NOT NULL
        CHECK (operation_type IN ('activate', 'finalize')),
    authority_reference_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_body TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_object_digest TEXT,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state = 'pending'),
    created_at TEXT NOT NULL,
    PRIMARY KEY (operation_type, authority_reference_id),
    CHECK (length(authority_reference_id) > 0),
    CHECK (length(endpoint) > 0),
    CHECK (length(idempotency_key) > 0),
    CHECK (length(request_digest) = 64),
    CHECK (
        response_object_digest IS NULL
        OR length(response_object_digest) = 64
    )
);

CREATE INDEX capability_license_pending_deliveries_result
    ON capability_license_pending_deliveries(response_object_digest)
    WHERE response_object_digest IS NOT NULL;

CREATE TRIGGER capability_license_pending_delivery_completed
AFTER INSERT ON capability_license_references
WHEN EXISTS (
    SELECT 1
    FROM capability_license_pending_deliveries
    WHERE response_object_digest = NEW.object_digest
)
BEGIN
    DELETE FROM capability_license_pending_deliveries
    WHERE response_object_digest = NEW.object_digest;
END;
