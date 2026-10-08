# ADR 0046: Preserve both canonical version-9 migration histories

- Status: accepted
- Date: 2026-10-02
- Amends: [ADR 0029](0029-source-verification-tokens.md), canonical source-token migration numbering.

<!-- CODE-VERIFY: Check persistence/migrations.py::resolve_migration_catalog, backup.py::_verify_database, retention_restore.py::restore_migration_backup_with_deletion_barriers, analytics/rebuild.py::ReadOnlyCanonicalDatabase.validate_schema, both canonical SQL catalogs, packaging/runtime-files.json, and test_canonical_migration_lineages.py before changing compatibility claims. -->

## Decision

Support exactly two historical canonical migration catalogs. Preserve every existing ledger row and apply the missing feature at version 10. New databases use the main catalog. Future canonical migrations start at version 11 and share one catalog tail.

| Validated database history | Version 9 | Version 10 | Version 11 onward |
| --- | --- | --- | --- |
| Fresh database, common prefix through version 8, or main history | `message_catchup` | `analytics_source_tokens` | Shared canonical catalog |
| Historical analytics history | `analytics_source_tokens` | `message_catchup` | The same shared catalog |

The default catalog is `app/persistence/sql/`. Its `0009_message_catchup.sql` retains the original main bytes. `0010_analytics_source_tokens.sql` contains the original analytics SQL bytes under its new default version.

The compatibility catalog combines the shared versions 1–8, two files under `app/persistence/sql/legacy_analytics_v9/`, and the default catalog's version-11-and-later files. The two compatibility files are `0009_analytics_source_tokens.sql` and `0010_message_catchup.sql`. Do not duplicate the common prefix or add a configurable catalog registry.

These historical identities are fixed SHA-256 fingerprints of the SQL bytes:

| Historical version-9 ledger name | SQL SHA-256 |
| --- | --- |
| `message_catchup` | `16dd7b5eabaff870a1313140123cd3081ccd50790ea1d3c3f6bf5dfadfbf7239` |
| `analytics_source_tokens` | `74043156dce6ef81f2fca8db3d4ccd47772f2cd862afdbd05baf00d621b4a772` |

Each corresponding version-10 resource must retain the same SQL bytes as its historical version-9 resource. Both complete catalogs must satisfy the existing filename, checksum, and contiguous-version rules.

## Why

Main and the analytics branch independently assigned canonical version 9. Versions 1–8 are identical. Both version-9 migrations are additive, but an installed database records the original version, name, checksum, and application time.

Renumbering the analytics file alone would reject databases that already applied it. Rewriting their ledger would erase migration history. The finite compatibility catalog preserves both histories while installing both features.

## Select and validate the complete history

`resolve_migration_catalog(connection, migrations_dir=None)` selects from the database connection being checked. An exact version-9 name and fingerprint identifies the historical catalog. Fresh databases and validated prefixes through version 8 use the default catalog.

Selection is only the first check. Validate the entire applied prefix against the selected catalog: contiguous versions, every name and checksum, no unknown later version, and `PRAGMA user_version` equal to the ledger maximum. A recognized version-9 row cannot excuse a changed earlier row, missing row, or inconsistent later history.

Unknown or inconsistent histories fail before pending migration writes. Do not infer a lineage from table existence or a similar schema. Do not reset the database, rewrite ledger rows, retry with another catalog after validation fails, or expose an environment override.

The compatibility rule applies only to the shipped canonical catalog, including callers explicitly passing that same directory. Authentication, Bridge projections, analytics projections, and other custom catalogs retain their existing exact-catalog behavior. Catalog selection remains persistence-owned and imports neither analytics nor transport.

## Use the same rule at every boundary

`MigrationRunner.run` resolves and validates the catalog within the existing migration locks. Pending SQL, its new ledger row, and `user_version` use the existing transactional application path and verified migration backup. Existing ledger records, canonical content, deletion barriers, and existing source-token values remain unchanged.

`backup._verify_database` resolves from the backup's own canonical ledger. Ordinary completed-backup verification still requires the complete current catalog, integrity checks, and foreign-key checks. This change does not admit old partial schemas as completed backups.

`retention_restore.restore_migration_backup_with_deletion_barriers` resolves from the staged migration backup, even when the active database has the other history. It upgrades the staged copy and preserves the existing deletion-barrier and retention reconciliation before publication. The source backup remains unchanged.

`ReadOnlyCanonicalDatabase.validate_schema` uses the same catalog rule for analytics rebuild input. It remains read-only and retains its exact-prefix checks: the ledger and `user_version` must agree, and the actual schema must match the recorded supported prefix. Catalog selection does not upgrade that input.

The runtime file manifest must include the exact bytes of all four version-9/version-10 resources, including the compatibility subtree. Package validation must check the recursive SQL file closure. Protected-impact mappings retain the source-token obligations for both its default and historical resource paths.

## Consequences and qualification

The two ledger shapes remain different after version 10. Future migrations must support both through the shared tail. Removing the compatibility resources would strand historical analytics databases and their backups. Downgrade support is unchanged: older binaries continue to reject schemas they cannot verify.

The two orders must converge on equivalent application schemas and behavior. Compare preservation of existing random source tokens within each database; independent databases need not mint equal token values.

Qualification must cover these conditions before publication:

- Fresh and populated version-8 databases, populated main-v9 databases, and populated analytics-v9 databases upgrade without changing acknowledged content, identity, or existing ledger rows. Migration backups retain their original versions and histories.
- Both version-10 histories reopen without another migration or backup and accept the same temporary version-11 test tail.
- An injected failure in either pending migration rolls back its schema changes, new ledger row, and `user_version` together.
- Altered fingerprints, earlier checksums, ledger gaps, inconsistent versions, unknown tails, and missing compatibility resources are rejected without normalizing the database.
- Completed backups from both histories verify and restore. Dedicated migration-backup restore preserves current deletion and retention authority across either history. Read-only rebuild validation accepts supported exact prefixes and refuses inconsistent histories or schema tampering.
- Packaging closure, architecture boundaries, and required Windows migration coverage remain enforced.

These are required assertions, not a claim that a qualification run passed. Record results against the exact tested source revision in the accompanying change. The focused coverage belongs in `tests/test_canonical_migration_lineages.py`, alongside the existing canonical persistence, source-token migration, backup, and retention-restore suites.

## Related

- [ADR 0009: Local-first topology and persistence](0009-local-first-topology-and-persistence.md)
- [ADR 0019: Encrypted local persistence](0019-encrypted-local-persistence.md)
- [ADR 0029: Source verification tokens](0029-source-verification-tokens.md)
- [ADR 0044: Message catch-up and freshness](0044-message-catch-up-and-freshness.md)
