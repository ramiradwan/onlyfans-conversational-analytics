-- Explicit internal integrity checksum version; existing graph data is unchanged.
ALTER TABLE conversation_graph_units ADD COLUMN checksum_version INTEGER NOT NULL DEFAULT 1
    CHECK(checksum_version IN (1,2));
ALTER TABLE conversation_graph_units ADD COLUMN integrity_metadata BLOB
    CHECK((checksum_version=1 AND integrity_metadata IS NULL) OR
          (checksum_version=2 AND typeof(integrity_metadata)='blob'
           AND length(integrity_metadata)>0 AND length(integrity_metadata)<=262144));
