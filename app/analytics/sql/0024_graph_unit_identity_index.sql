-- Keep reference checks out of the large WITHOUT ROWID membership payload.
-- The table primary key remains authoritative; this compact UNIQUE copy lets
-- SQLite enforce the same composite foreign key without reading blob pages.
CREATE UNIQUE INDEX conversation_graph_unit_identity
ON conversation_graph_units(creator_account_id,unit_id);
