# History kind adoption

<!-- CODE-VERIFY: extension/qualification/signer-release.mjs SIGNER_RELEASE, extension/transport/read-only-history-coordinator.mjs -->

The product pins connector `0.5.0-rc.2`. It retains `safe-reload` as the default.
Opt-in observe-only capture is enabled separately by onboarding.

The reviewed prerelease delivers a versioned client category with each history
record. It is a bounded client parity standard. It supplies no backend guarantee
and enables no pricing capability. The Agent preserves every record and commits
the metadata with the record through its existing page transaction and outbox.

Brain stores the metadata with the account and conversation binding. Metadata
changes participate in canonical hashes, merges, source identities and answer
invalidation. Passive observations without metadata preserve an existing kind.
Immutable record content, deletion barriers and account isolation still apply.

Both canonical question adapters require the per-record schema
`connector-history-kind/v1`, mapping `onlyfans-event-kind/0.2.0` and evidence
standard `client-parity`. Supported human categories become messages, including
media-only records. Supported non-message categories become systems. Missing,
unfamiliar and unsupported metadata remains unknown. The legacy observation
sidecar keeps its unknown interpretation and cannot substitute for this field.

The bounded reply selector excludes systems before choosing the latest message.
Unknowns remain ordering contenders. Later and tied unknowns block a positive
no-reply result. Tied known messages without authoritative order also block it.
An empty classified set provides no no-reply evidence. Cutoff, retention, coverage,
work budgets and publication checks continue to constrain each answer.

## Question version proposal

Propose `no_later_creator_reply.client_parity.v1` for latest retained client-category
message semantics. Keep it separate from the approved latest imported inbox entry
question. Its definition digest should bind the per-record schema, mapping,
evidence standard and supported pin set alongside the existing time and coverage
rules. Queue and mass-message reply decisions remain outside this proposal.

Activating and claiming acceptance for that question requires an owner-approved
acceptance-manifest change. This adoption leaves the manifest and public question
identifier unchanged. The implementation definition revision advances to
`canonical.client-parity.v1` so old cursors and answer definitions cannot carry
over to the changed semantics. These regression tests establish consumer behavior and
do not establish analytics qualification. No qualification job is requested.
