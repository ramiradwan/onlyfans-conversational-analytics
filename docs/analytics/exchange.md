# Analytics exchange contract

<!-- CODE-VERIFY: app/analytics/exchange_contracts.py; app/analytics/exchange_codec.py; tools/analytics_exchange_records.py -->

`analytics-exchange.v1` is a versioned exchange of the active analytics model. Its `synthetic-conformance.v1` profile accepts named synthetic fixtures for compatibility testing. SQLite remains the local implementation. This contract does not add a production cloud backend, account management, upload, or synchronization.

The envelope contains a `RebuildArtifact`, a logical `QuestionSnapshot`, explicit question facts, question definition digests, a source facts digest, and a content digest. The artifact carries the current projection, enrichments, conversation metrics, nodes, and edges. It retains the pipeline's graph digest version, including `graph.segment-root.v1`. Import does not reinterpret graph order as authoritative message order or graph labels as source event kinds.

Each observation retains its source-linked `QuestionEvidence`, role, event kind, optional source order, ordering provenance, and declared classification. Each conversation retains its coverage value. Unknown values remain unknown. The classification records its method, configuration digest, language, and input version digest. A classification for another source version remains stale. The declared fixture method exercises pricing question mechanics and does not qualify a production classifier.

## Identity and values

Account, conversation, message, node, and edge references retain their active opaque identities. Nodes, edges, conversation facts, and observation facts have unique ascending identities. Enrichments and metrics retain their original array order. Edges require both endpoints in the same account's exchanged node set. The snapshot, projection, graph, source revision, pipeline identity, and question definitions must agree before publication.

Canonical JSON uses UTF-8, sorted object keys, compact separators, and finite numbers. Integers outside JavaScript's exact integer range use the sole-member object `{"$analytics_integer":"9007199254740992"}`. Its value is a canonical decimal string of at most 1,024 digits, with an optional minus sign. Leading zeros, plus signs, negative zero, safe integers expressed as tags, and additional members are invalid. The tag key is reserved and cannot appear in logical properties. Untagged wide integers are invalid on the wire. Python consumers recover integers before model validation. Floats retain their numeric type. Null, absent properties, empty strings, empty lists, and empty objects remain distinct. Duplicate JSON keys and non-finite numbers are invalid.

The content and facts digests use SHA-256 over domain-separated canonical wire JSON. The envelope's content digest excludes only its own `content_digest` field. The existing projection and graph digests retain their own definitions. An exchange is limited to 64 MiB and 100,000 total transport records, including the header, graph, enrichments, metrics, and question facts. Question facts have their own 10,000-conversation and 10,000-observation limits.

## Authority and publication

An imported document is a derived result. It cannot establish its own source authority. Import requires an independently supplied account, canonical revision, canonical content digest, and source facts digest. The conformance harness seeds each SQLite target's canonical store from the named fixture and reads its current identity through the canonical gateway. Imported question facts are persisted separately and read from the receiving target. The original target's sidecar is not a fallback.

The logical snapshot identity survives exchange. Each SQLite publication receives its own physical generation and passes the active projection activation checks. The sidecar becomes ready only after that publication succeeds and the source binding is checked again. Interrupted imports remain unreadable through the question adapter until resumed. Repeating the same complete import is idempotent. Different content under an existing identity is rejected.

Source changes, deletion, or reaching the earliest observation's 90-day retention boundary invalidate the result. Validation uses the current independent source witness, including the question facts digest. A transported projection or ready marker cannot make stale evidence current.

## Internal interfaces

`seal_exchange`, `encode_exchange`, and `decode_exchange` define the pure contract. `validate_exchange` accepts the independent expected source binding and clock. `SQLiteExchangeTarget` builds, imports, exports, and opens bounded question reads against fresh synthetic SQLite stores. `exchange_records` and `recover_exchange` map the same artifact to immutable transport records and validate complete membership on return. These tool interfaces are not product endpoints.

See [conformance execution](exchange-conformance.md) for the target adapter and the distinction between local tests and a recorded external run.
