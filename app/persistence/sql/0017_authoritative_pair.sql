CREATE TABLE authoritative_pair (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 auth_store_id TEXT NOT NULL CHECK(length(auth_store_id)=32),
 canonical_store_id TEXT NOT NULL CHECK(length(canonical_store_id)=32),
 installation_id TEXT NOT NULL CHECK(length(installation_id) BETWEEN 1 AND 128),
 incarnation TEXT NOT NULL CHECK(length(incarnation)=32),
 valid INTEGER NOT NULL CHECK(valid IN (0,1))
);
