# ADR 0033: Validate large projection documents one record at a time

- Status: accepted

## Decision

Keep the complete projection JSON and its existing digest. Derive a small stored validation header with a versioned deterministic SQLite function. The function validates the document syntax and consumes the two large arrays one item at a time. It does not retain their decoded contents.

The stored header is generated from the document, not supplied by the writer. Existing document identity checks and query metadata binding use that header. The full persisted document still undergoes independent model, digest, account, and graph verification before publication.

Register the JSON helper on every supported encrypted connection, including detached backup and migration connections. The helper belongs to the persistence layer and imports no analytics types. Missing function support or invalid JSON prevents insertion. Duplicate object keys and nonstandard numeric literals are rejected.

Cancellation checks run between array items during application-controlled writes. SQLite function errors do not expose document contents. The original cancellation exception is preserved at the operation boundary.

## Consequences

The analytics migration recreates the document table with a generated stored header. It preserves document bytes, generation references, immutable-row guards, foreign keys, and metadata. Migration backup and restart tests cover populated stores. Canonical records and their schema do not change.

Validation does not allocate a parse tree for every message at once. Memory still includes the input string, the largest individual array item, non-array header fields, and the existing build state. This is not a constant-memory graph builder or physical delta storage.

The runtime uses the Python standard library and the existing SQLCipher driver. No package, model, service, or network access is added. Older application versions must reject the newer disposable schema through normal version checks.

See [Projection document validation](../analytics/document-validation.md). Publication, deletion, retention, source identity, and authorization requirements remain unchanged.
