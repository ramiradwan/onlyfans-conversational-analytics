-- Installation authority survives disposable data and account removal.
CREATE TABLE nlp_dependency_authority (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 state_json TEXT NOT NULL CHECK(length(state_json)<=4194304), state_sha256 TEXT NOT NULL
);
CREATE TABLE nlp_dependency_profiles (
 digest TEXT PRIMARY KEY, document BLOB NOT NULL CHECK(length(document)<=8388608),
 closure_digest TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('planned','available','removing','removed'))
) WITHOUT ROWID;
CREATE TABLE nlp_dependency_operations (
 operation_id TEXT PRIMARY KEY, document TEXT NOT NULL CHECK(length(document)<=1048576)
) WITHOUT ROWID;

INSERT INTO nlp_dependency_authority VALUES(1,'{"catalog_jws":null,"catalog_sha256":null,"clock_high_water":0,"epoch":0,"schema":"nlp-dependency-authority.v1","sequence":0,"trust_sha256":null,"withdrawals":[]}','7cc3e090303ae141a01842d43852445634e73a6430adc51bb1ef08b18e930053');
