ALTER TABLE provisioning_claim_submissions
    ADD COLUMN claim_profile TEXT NOT NULL DEFAULT 'urn:bridge-clean:installation-claim:v1';

ALTER TABLE provisioning_claim_submissions
    ADD COLUMN enrolled_at TEXT;
