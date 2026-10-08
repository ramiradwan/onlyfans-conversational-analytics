-- Compact identity lookup for enrichment references; payloads and primary key stay unchanged.
CREATE UNIQUE INDEX conversation_enrichment_unit_identity
ON conversation_enrichment_units(creator_account_id,unit_id);
