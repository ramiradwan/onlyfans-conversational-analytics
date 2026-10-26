CREATE TABLE auth_store_identity (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 store_id TEXT NOT NULL UNIQUE CHECK(length(store_id)=32)
);
INSERT INTO auth_store_identity VALUES(1,lower(hex(randomblob(16))));
CREATE TRIGGER auth_store_identity_update BEFORE UPDATE ON auth_store_identity
BEGIN SELECT RAISE(ABORT,'auth_store_identity_immutable'); END;
CREATE TRIGGER auth_store_identity_delete BEFORE DELETE ON auth_store_identity
BEGIN SELECT RAISE(ABORT,'auth_store_identity_immutable'); END;
CREATE TABLE authoritative_pair (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 auth_store_id TEXT NOT NULL CHECK(length(auth_store_id)=32),
 canonical_store_id TEXT NOT NULL CHECK(length(canonical_store_id)=32),
 installation_id TEXT NOT NULL CHECK(length(installation_id) BETWEEN 1 AND 128),
 incarnation TEXT NOT NULL CHECK(length(incarnation)=32),
 valid INTEGER NOT NULL CHECK(valid IN (0,1))
);
