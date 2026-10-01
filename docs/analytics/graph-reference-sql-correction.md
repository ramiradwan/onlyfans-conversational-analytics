<!-- CODE-VERIFY: Check migration 0024, the scoped graph reclaim statement, receipt/guard catalogs and test_graph_reference_sql.py before editing these guarantees. -->
# Graph-reference SQL correction

The correction avoids opening large membership payload rows when only a unit identity is needed. It changes no graph representation, content, source binding, retention rule, proof lifetime, cache limit or qualification threshold.

## Compact parent identity

Migration 0024 adds `conversation_graph_unit_identity`, a UNIQUE index on `(creator_account_id, unit_id)`. Those columns are already the primary key. The table and primary key remain authoritative; the new B-tree stores only compact keys rather than the table's large WITHOUT ROWID payload records.

The measured SQLite foreign-key program chooses the compact index when inserting references. Graph-reference closure queries also use it as a covering index. No `INDEXED BY`, `ANALYZE`, disabled foreign keys or planner override is introduced. If the index is removed, the existing primary key still enforces identity and reference checks; performance may regress, but correctness does not depend on the optional access path.

## Reference-first retirement

The existing temporary set still limits retirement to the outgoing generation's units. The revised statement checks for remaining references in the compact reference index before looking up unit payload rows. Only genuinely unreferenced listed units become deletion candidates. Account scope is supplied to both the outer deletion and the inner reference check.

The enclosing transaction, synchronous cleanup, shared-unit protection, referenced-unit delete trigger, retirement guards, content epoch, rollback history and proof-transition checks remain unchanged. The correction does not delay deletion or move cleanup outside visibility timing.

## Migration and verification

The checksummed forward migration uses the existing installation lock and encrypted backup mechanism. The package SQL catalog includes its SHA-256. Older catalogs reject the newer database rather than silently opening it. Failed migrations roll back the index and ledger changes.

No trigger changes in version 24. The explicit receipt/guard version tables carry the existing full version-23 signatures forward. Schema-cookie changes still invalidate prior receipts. Dropping the performance index does not create proof authority, and missing/corrupted references still fail validation.

## Measured selection

On a disposable SQLCipher fixture, the previous reference-copy program opened the large `conversation_graph_units` B-tree. The new program opens the compact identity index. The closure query changes from primary-key lookup to covering-index lookup. The retirement query checks compact references before accessing candidate units.

An equivalent NOT EXISTS closure rewrite alone and a wider nonunique metadata index did not remove the measured cost. They were not selected. The separate reference SQL component preserves all samples and independently checks stored bytes, memberships, shared-unit survival, orphan reclamation and foreign keys. It is not canonical graph equivalence or end-to-end visibility evidence.
