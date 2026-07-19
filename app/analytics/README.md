# Canonical analytics pipeline

Analytics is a derived consumer of the canonical `IngestionRepository`; it is
not an ingestion or source-of-truth database. For each creator account the
pipeline reads one `AccountReadModel`, enriches its messages, derives metrics,
projects a local property graph, and records the canonical `view_revision` on
every output.

## Data flow

1. `AnalyticsPipeline` reads a canonical account snapshot from either the
   in-memory or SQLite repository backend.
2. `EnrichmentStage` invokes exactly three narrow ports: `SentimentAnalyzer`,
   `TopicEntityAnalyzer`, and `EngagementAnalyzer`.
3. Pure metric functions build per-conversation and creator aggregates.
4. `RelationshipGraphProjector` emits engine-neutral nodes and temporal edges.
5. A bounded post-canonical-commit coordinator coalesces revisions per account
   across an owned fixed worker pool. Background stages return an immutable
   candidate and cannot write either active store.
6. Scheduler-controlled publication reserves and completes the canonical
   witness, then activates the matching immutable SQLite generation. Memory
   adapters retain the same candidate/publication separation.
7. Insights services and endpoints read only the active projection. Missing,
   building, or failed projections report their stable availability state;
   HTTP reads never schedule or perform projection work.

Startup recovery and accepted canonical commits are the only scheduling paths.
Shutdown atomically closes admission and publication first, cooperatively
cancels owned work, and joins it only within the monotonic deadline. A
non-cooperative Python callable cannot be killed; it is detached from every
publication path and its eventual candidate is discarded.

No stage uses wall-clock time, random values, network I/O, model downloads, or
mutable ingestion queues. Timestamps must be timezone-aware, and stable time
sorting compares UTC instants and uses the canonical source ordinal when
timestamps tie. Outputs include a
projection generation and deterministic SHA-256 content digest. Replaying the
same pipeline, adapter revisions, and adapter configuration digests over the
same canonical revision therefore produces byte-equivalent JSON.

Canonical rows without a recorded source ordinal are assigned deterministic
message-identifier order by migration `0002`. An earlier capture order is not
represented in those rows and cannot be reconstructed.

## Analyzer adapters

The default analyzers are deliberately small, dependency-free rule baselines.
Production model adapters can be supplied through the three protocols without
changing pipeline, persistence, metric, graph, or endpoint code. An adapter
must declare a stable name, revision, configuration digest, analysis mode, and
calibration status, be deterministic for that identity, and must not persist
data inside the analyzer. Projections expose those identities together with
sample coverage and meaningful confidence summaries. The built-in adapters and
priority/response formulas are explicitly uncalibrated baselines.

## GraphStore contract

`GraphStore` supports keyed node and edge upserts, exact revisioned partition
replacement, neighborhood lookup, bounded path search, degree queries, and
centrality/community hooks. Every traversal supplies its account, allowed node
and edge kinds, time window, hop, queue, result, visited-state, edge-scan, and
wall-clock limits and reports truncation. Both timestamps of every adjacent
endpoint and the edge timestamp must be in range. Null-time nodes and edges are
excluded unless `include_timeless` is explicitly enabled; opt-in treats them as
eligible structural records within the other account/type/work bounds, not as
events at every time. An out-of-window root is either rejected by
`require_in_scope` or returned alone by `include_only`.

Shortest-path lookup uses bounded BFS and bounded predecessor storage. It stops
expansion at the first target depth and reconstructs only shortest paths; it
does not queue a path copy per branch. The SQLite adapter filters endpoints,
kinds, and time before applying edge limits. Rooted NetworkX slices use bounded
hop expansion; unrooted slices are capped directly by node and edge limits.
NetworkX is checked for cancellation/deadline before and after materialization
and the non-cooperative call. A result is rejected if cancellation, deadline,
or generation replacement occurs, and cache rows are generation-scoped.

The graph includes participant, conversation, message, topic, explicit entity,
affect-state, and engagement-state nodes. Message and conversation `precedes`
edges carry temporal intervals. Raw message text remains in the canonical store
and is not copied into graph node properties.

Persisted graph properties are deny-by-default. The complete allowlist is:

- participant: `role` (`creator` or `counterpart`);
- conversation: derived counts, mean sentiment, and response coverage;
- message: direction, canonical source ordinal, and character count;
- topic: a fixed built-in taxonomy ID and its fixed label;
- entity: entity class and a domain-separated account-scoped SHA-256 identity;
- affect/engagement: fixed enum labels and numeric score/confidence;
- edges: fixed roles, confidence, or derived interval/scope where applicable.

Display names, user-supplied labels, text, URLs, mentions, hashtags, raw entity
values, and raw platform/conversation/message identifiers are not graph
properties. Node and edge primary keys are opaque domain-separated digests.
Schema triggers make validated, pending, and active generation rows immutable;
public production graph mutation is available only through a new generation.

No embedded graph engine is selected by this package. A future local engine
must pass `tests/test_graph_store_contract.py` and remain behind `GraphStore`.

## SQLite generation activation

`canonical.sqlite3` is authoritative. `projections.sqlite3` is disposable and
may be deleted at any time. A build records one canonical revision plus exact
canonical-content digest, projection and graph digests, and the complete
pipeline/analyzer/config identity:

```text
building (owner + lease) -> validated -> activation_pending -> active
                                  |             |              |
                                  +-- reserve canonical intent |
                                                +-- complete canonical witness
                                                       -> local CAS activation
active/rejected/abandoned generations -----------------------> retired -> GC
```

Validation recomputes projection coverage, graph coverage, every digest,
foreign keys, and SQLite integrity from rows. Reservation is a canonical CAS;
completion is a second canonical CAS. Local activation rechecks the completed
witness and the expected prior active generation inside its transaction. Thus
a delayed build cannot replace a newer winner and no claim depends on an atomic
commit across the two files.

Every read rechecks the caller's canonical revision/content identity and the
full completed witness. A canonical advance immediately makes the old graph and
projection unavailable. Startup distrusts local `active` status: null,
missing, cancelled, mismatched, or tampered witnesses are quarantined. An exact
completed witness can finish an interrupted local activation; reserved stale
work is cancelled. Non-expired owner leases protect live building/validated
work from another process. Expired work is reclaimable. GC keeps the active and
pending generations plus a configurable small retired rollback retention and
deletes only a bounded batch per pass.

## Backup, restore, and private files

Online canonical and optional projection backups run SQLite integrity/FK and
migration checksum checks. Projection verification recomputes row coverage and
digests rather than trusting generation metadata. The external manifest binds
the canonical identities, complete witnesses, generation/intent IDs, graph and
projection digests, and pipeline identity. Its file SHA-256 detects accidental
or uncoordinated modification; it is integrity metadata, not authentication.

Restore requires the application lifecycle lock and no application connection,
SQLite sidecar, alias, symlink, or reparse-point target. Files are copied to a
private temporary database, reverified, atomically replaced, and followed by a
directory sync where supported. A canonical-only or mismatched pair restore
deletes the disposable projection file. Matching pairs still become visible
only after startup verifies their full completed witness.

POSIX database, temporary, backup, manifest, WAL, and SHM files are verified as
owner-only. On Windows they receive and verify a protected owner-only DACL;
`BEGIN IMMEDIATE` forces sidecar creation and verification before caller data is
written. The service account must be allowed to create files and set that DACL
on the installation directory/filesystem, otherwise startup or the operation
fails closed. Raw handles opened outside this process cannot be coordinated by
the application lock; sidecars are rejected and Windows atomic replacement
provides the final refusal boundary.

The synthetic benchmark can run the retained 5k/10k scaling guard:

```powershell
python tools/benchmark_sqlite_graph.py --size 5000 --compare-doubling
```

It rejects a doubling ratio at or above 3.5 for build, bounded query, or peak
memory. Million-record validation remains future scale work; the current claim
is bounded correctness and the fast 5k/10k regression guard, not 1–5m capacity.

## Rebuild

Use the module entry point against a canonical SQLite file:

```powershell
python -m app.analytics.rebuild `
  --canonical-path path/to/canonical.sqlite3 `
  --account-id creator-account-id `
  --output analytics-projection.json
```

The source file must already exist and cannot be reached through a symbolic
link, reparse point, or parent-path alias. Rebuild pins one read-only SQLite
connection and transaction from file-identity validation through replay. The
applied migration prefix must match repository versions, names, checksums, and
complete table/index/constraint definitions. Trusted input must also pass full
SQLite `integrity_check` and `foreign_key_check`; diagnostic rows never cross
the sanitized rebuild boundary. Source identity is checked after open and
immediately before publication.

File output is published by atomic replace. POSIX output is verified as mode
`0600`; Windows output receives a protected DACL containing one full-access ACE
for its owner and is verified before data is written and after publication.
Permission failure refuses output. The artifact is derived-only and can always
be discarded and rebuilt.

## Frontend reconciliation prerequisite

The backend live schemas and routes in this branch are authoritative. The
`feat/analytics-frontend` final adapter work must replace or reconcile
`frontend/src/config/endpoints.ts`, `frontend/src/types/backend.ts`, and
`frontend/src/types/backend-wss.ts`: remove double-prefixed/bootstrap-user
routes and the stale `analytics_update` event. Backend compatibility aliases
must not be reintroduced.
