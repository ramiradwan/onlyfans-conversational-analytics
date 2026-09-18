<!-- CODE-VERIFY: Check canonical_source.py, models/analytics.py, historical_derivation.py, insights.py, and the question fixtures before changing integration claims or field meanings. -->

# Analytics question contract

This specification defines `no_later_creator_reply.v1` and `pricing_discussions.v1`. It does not enable endpoints. The synthetic cases in `tests/fixtures/analytics/questions/` contain manually chosen expected answers, not model-quality evidence.

## Scope, time, and counting

The authenticated runtime supplies the account. A question, source reference, or cursor cannot select another account. Account ownership, participant role, and physical database partition are separate concepts.

Each question returns distinct conversations, not distinct people or matching-message counts. Identical source IDs in two accounts never share results.

Store instants in UTC. Resolve calendar dates in the requested IANA timezone before execution, including daylight-saving transitions. Reject missing timezone information, reversed intervals, and `start == end`. Selection is half-open: `start <= sent_at < end`.

The service fixes an analysis cutoff and source revision for each result. Selection end cannot exceed the cutoff. Follow-up includes messages at the cutoff, but nothing later. Do not claim the cutoff is a complete-history watermark.

Eligibility also uses the current retention clock, independently of the query cutoff. Participant analytics admits source times strictly later than that clock minus 90 days. Moving the query date or rebuilding cannot restore expired data. Show both requested and effective coverage; never silently present a clipped interval as complete.

## Ordering and coverage

Use source ordering evidence when it establishes order. Arrival sequence, message ID, and a deterministic sort do not establish upstream chronology. Retain inferred ordering as inferred. If plausible orderings change an answer, classify the conversation as undetermined rather than choosing an answer from the tie-breaker.

Known system events do not count as participant messages. An attachment-only message with a known human sender does count. Missing body text does not imply a system event. Unknown sender role or event kind must not be coerced to inbound, outbound, or system; when relevant to the answer it makes the result undetermined.

Keep coverage (`complete`, `partial`, `unknown`) separate from availability and ordering confidence. Missing older history, gaps, and stale capture remain explicit. An empty list means no matches in the evaluated data, not proof about unobserved history.

## No later creator reply

Select conversations with relevant observed activity in the selected interval. Inspect all eligible relevant messages in each selected conversation through the analysis cutoff, including messages after selection end.

Return a conversation when its latest relevant observed message is from the other participant. Link that message as evidence. A later creator message excludes the conversation, even when it falls outside the selection interval. A later participant message may become the evidence even outside that interval.

A match describes retained, imported messages. It does not mean a reply is required or that no reply occurred outside those messages. Partial coverage can accompany a match, but ambiguous order or role cannot be presented as a match.

## Pricing discussions

Return a conversation with at least one eligible, selected message whose current, versioned classification positively identifies a pricing discussion. Both human message directions are eligible. Return matching source references, deduplicate conversations, and report matching-message counts separately when needed.

A negative classification, uncertainty, unsupported language, missing analysis, and analyzer failure are different states. If there is no positive match and some selected messages lack a usable classification, the conversation is undetermined, not a confident negative. A positive match remains valid with reduced classification coverage for other messages.

Amounts, commercial keywords, and stated buying intent are not transaction records. Pricing quality is evaluated separately from query correctness using the [qualification policy](qualification.md). No supported language is inferred merely from a tokenizer accepting text.

## Result and evidence records

The result envelope identifies the question/version, counting unit, resolved interval/timezone, cutoff, source revision, projection generation, query-definition digest, availability, coverage, ordering limitations, and pagination state. Include counts of evaluated and undetermined conversations. Unknown totals are absent, not zero.

Each row contains an account-scoped conversation reference, relevant source references, reason code, and coverage. Sort by latest relevant evidence time descending, then opaque conversation reference ascending. Default page size is 50; maximum is 200. Bind a cursor to account, normalized filters, cutoff, question/version, and generation. Reject changed-generation cursors rather than mixing snapshots.

Use existing availability meanings: `available`, `building`, `unavailable`, and `error`. A complete empty result is `available`. Partial history is coverage, not a new availability value. A timed-out or truncated computation must not claim an exact total or complete answer.

An evidence reference identifies the account, conversation, message, source revision, and digest of the source version actually analyzed. The digest covers relevant text and metadata, not only the message ID. Optional text spans use Unicode code-point offsets `[start, end)` into that exact text version; adapters must convert to browser string offsets explicitly.

Resolve text through authenticated local canonical reads, not graph properties. Recheck account, deletion, retention, and source version. Changed or unavailable evidence cannot display replacement text as support for an old finding. Source edits invalidate dependent findings; deletion and expiry also invalidate caches, pagination, and retained generations that would expose them.

An observation records a source event. A finding records an interpretation, its input references, method/model/configuration/taxonomy versions, and uncertainty. An outcome records an independently defined event or measurement and observation window. A shared topic is distinct from its occurrence in a particular message.

For multi-message findings, record all dependencies. Stop serving the finding when any required input becomes ineligible; recomputation from a reduced input set is a new finding. Human corrections, when supported, are separate versioned inputs, not mutations of source messages.

## Integration boundaries

`app/api/endpoints/insights.py` owns authenticated analytics HTTP access. `app/services/insights_service.py` consumes the configured analytics runtime. Read handlers must not build projections inline or bypass analysis admission.

`HistoryAnalyticsSource` is the approved canonical gateway. Its current read model sorts by timestamp, winning stream epoch, winning source sequence, and message ID. It does not expose source-order confidence, coverage intervals, event kind, or per-message version digests. Do not manufacture these fields in a handler. Add them through the gateway where source evidence exists; otherwise preserve unknown states. This contract does not authorize capture or canonical-schema redesign.

`EnrichmentStage` and the analyzer interfaces own classifications. `GraphReader` and the projection stores own bounded reads and publication. Extend indexed lookup only for demonstrated question needs. Reuse the existing generation and canonical-witness checks.

Local SQL and reviewed Gremlin templates must obey the same question semantics. Keep portable IDs, scalar properties, explicit missing values, UTC time, and relationship endpoints separate from engine encodings. A later Cosmos conformance run must compare the fixed expected cases; a local mock is not compatibility evidence.

Natural-language interpretation may select these approved questions and typed filters. It cannot supply account authority, execute arbitrary queries, write data, or override time and work limits.
