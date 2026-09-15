-- Companion pairing binds a Noise session to the exact grant bytes the hosted
-- plane signed, so the verified compact JWS is retained alongside its digest.
-- The column is nullable: references recorded before this migration keep their
-- existing authorization behaviour and are excluded from pairing until refreshed.
ALTER TABLE verified_grant_references ADD COLUMN compact_jws TEXT
    CHECK (compact_jws IS NULL OR length(compact_jws) <= 16384);
