<!-- CODE-VERIFY: Check conversation_enrichment_units.py, conversation_enrichment_unit_sql.py, enrichment_proof_transition.py, conversation_reuse.py, enrichment_cache.py, projection_encoding.py, projection_verification.py, sqlite_projection_store.py, retention_store.py, retention_restore.py, validation_receipt.py, sql/0017_conversation_enrichment_units.sql and test_incremental_enrichment_units.py and test_enrichment_proof_transition.py before changing reuse, fallback, digest or bounds claims. -->

# ADR 0040: Reuse immutable conversation enrichment units

- Status: accepted

## Decision

The built-in SQLite analytics path may retain one immutable conversation enrichment unit for each conversation in a completed generation. An unchanged conversation may contribute a witnessed unit reference to a later build instead of reconstructing its MessageEnrichment models.

Schema version 17 stores compressed canonical message-enrichment rows, the conversation metrics needed by account aggregation, exact confidence totals for analyzer provenance, and optional analyzer-cache records. The generation-owned reference binds the unit to the conversation input/configuration identity and retention window.

The public `AnalyticsProjection` model remains schema version 3. For pipeline revisions containing `enrichment.units.v1`, the projection digest uses a versioned composable definition built from the canonical non-array projection header, the ordered conversation-metrics component and each ordered conversation enrichment component `(conversation_ref, message_count, canonical_digest)`. A complete schema-17 unit manifest therefore allows an incremental update to recompute the projection digest without reading unchanged message payloads.

When that manifest is complete, SQLite stores a compact projection document whose `message_enrichments` array is empty. Explicit projection/artifact reads verify and materialize the referenced units and return an ordinary complete `AnalyticsProjection`. If the unit manifest is unavailable or incomplete, the same pipeline revision may store the full projection document and validate it through the normal streamed path.

## Eligibility and fallback

Reference reuse is available only while the exact active predecessor has a same-process enrichment-unit proof created after successful generation validation. The referenced unit header must match the predecessor proof and the current conversation input/configuration digest. Unit content is immutable while referenced.

A restart, missing proof, schema change, malformed unit, retention mismatch, changed conversation input, budget failure or cache inconsistency disables this shortcut. The build then uses the existing page/fragment/canonical path and may create a replacement unit. The optional unit cache is never canonical authority and is not required to read an already published projection.

A changed conversation may load its predecessor unit's analyzer-cache records. Normal enrichment cache keys still bind each result to the exact current message/context input, analyzer revision and configuration. A cache miss runs the analyzer.

## Integrity

New unit content is written inside the generation staging transaction. Existing content with the same unit identity must match the bytes and summary metadata exactly. Updates are blocked. Referenced content cannot be deleted even when foreign-key enforcement is disabled.

The store preserves a valid enrichment proof through its activation and retired-generation cleanup transactions. Before either transition, the full content stamp must match and the database trigger definitions must match the reviewed catalog. The same write transaction must preserve the selected generation, unit references, ordering, input/configuration identities and retention bounds. Referenced payloads remain immutable.

The renewed proof enters the cache only after commit, and only if the original proof is still cached. A stale proof, unknown trigger definition, schema change, changed selection or rollback prevents renewal. Writes after commit still invalidate the proof through the full content stamp.

The generation reference manifest must cover the same ordered conversations and message count as the projection metrics. Reused references must come from the exact proven predecessor. Incremental validation may trust unchanged unit headers only under that exact process-local proof; changed units are decompressed and independently checked. Missing proof, restart and explicit artifact reads perform full unit validation/materialization.

The compact projection document is streamed through the normal projection verifier. Its versioned digest is recomputed from the verified document metadata, the ordered conversation-metrics digest and the verified schema-17 enrichment components. Publication, canonical witness checks, graph validation, activation receipts and clean-rebuild equivalence remain required.

## Bounds

At most 4,096 enrichment units are retained per build. Newly retained unit payloads are bounded to 256 MiB in aggregate. One compressed message payload or analyzer payload is bounded to 128 MiB, and one unit contains at most four million message enrichments. Conversation-local analyzer retention remains bounded independently of the account-wide staging cache.

Exceeding a bound disables optional unit reuse for the affected build rather than changing analytics results.

## Consequences

A small source change can keep message-enrichment model reconstruction, analyzer-cache recovery and enrichment-unit validation proportional to changed conversations rather than total account messages. Unchanged units contribute only bounded metadata/digest components during the hot build and validation path.

Cold builds populate the optional unit cache and therefore perform additional bounded writes. Restart without process-local proof uses the existing materialized reuse path until a newly validated generation establishes another proof. Explicit reads and backup/restore validation can materialize the complete message-enrichment set from units.

The change adds one rebuildable analytics migration and no external service, dependency, database file or writer process.
