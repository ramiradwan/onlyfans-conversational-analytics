<!-- CODE-VERIFY: Check insights.py, insights_service.py, query_runtime.py, query_reader.py, query_canonical.py, query_identity.py, query_publication.py and their tests before changing route or behavior claims. -->

# Query conversations and open their sources

The insights API accepts the [closed question plans](questions.md) and returns source-linked, versioned pages. Reads use the active SQLite analytics publication and live canonical records. They do not run classifiers, construct a graph, or rebuild projections inline.

## Routes

| Method and path | Purpose |
|---|---|
| `GET /api/v1/insights/questions` | List question availability and limitations. |
| `POST /api/v1/insights/questions` | Execute a question plan. |
| `POST /api/v1/insights/questions/evidence` | Resolve a returned source reference. |
| `DELETE /api/v1/insights/questions/evidence` | Clear the current account's source links. |

Every route requires the existing activated, authenticated runtime. POST and DELETE also require the configured origin and session CSRF token in `X-CSRF-Token`. Account authority comes from the session. Request query parameters cannot select an account or override a plan.

POST accepts uncompressed `application/json` bodies of at most 16 KiB. Duplicate keys, malformed JSON, unsupported fields, and invalid windows are rejected without echoing the input. Body reads have a two-second timeout. Successful reads and handler failures use `Cache-Control: no-store`.

The question response contains opaque source references, not message text. Evidence responses contain the exact source text and native conversation/message identifiers for local navigation. Render that text as text, not HTML. The frontend must discard displayed results when the account or source revision changes.

## Available source information

`no_later_creator_reply.v1` is executable. It inspects follow-up through the cutoff, including replies outside the selected interval. Ambiguous latest messages are undetermined, not matches. A match does not mean a reply is required.

The production canonical gateway does not retain event kind or authoritative source-order confidence. It supplies unknown event kind, inferred ordering, and unknown history coverage. Under the question contract, a conversation whose latest event kind is unknown remains undetermined. Known event types in synthetic integration tests are not evidence that these fields exist in production data.

The deterministic feature cannot be advertised as a qualified no-reply list until sufficient source evidence is available. Changing that interpretation requires an explicit contract decision; the gateway must not infer missing event kinds from message text or direction.

`pricing_discussions.v1` has an implemented handler for versioned classification inputs, but production execution returns `analytics_pricing_not_qualified`. There is no request flag that enables it. Its [language-quality gate](qualification.md) requires authorized, representative held-out examples. Synthetic cases test query mechanics only. Pricing uses existing graph-topic relationships from the pinned publication. Missing graph records stay unclassified. The canonical adapter does not manufacture event kinds or language support.

## Read and publication limits

Question resources admit at most two concurrent reads. The question service enforces its shared record, time, pagination, and evidence limits. SQL progress handlers and lock timeouts use the remaining request budget. Cancellation propagates when a request or runtime closes.

The canonical gateway streams the exact account-content digest required by publication validation. It does not assemble the full account read model. This still examines account records, including records outside the selection, and charges them to the work budget. A narrow date range does not eliminate this verification cost.

After identity verification, indexed account/conversation queries select candidates and follow-up messages. Memory use is bounded by the record limit and the retained page candidates. SQLite's date conversion is only a coarse candidate filter; Python applies exact timezone-aware boundaries. Each continuation reevaluates the bounded scope rather than retaining source data between requests.

The adapter verifies the completed canonical witness, source digest, active generation, and configured pipeline. It extracts publication metadata without returning the full projection document. SQLite still parses that document internally. Neither metadata extraction nor connection opening has a constant-time guarantee.

The in-memory projection backend has no published-question adapter. It reports unavailable instead of silently selecting a different authority. The encrypted SQLite adapter is the production implementation. No new persistent store, model, or runtime dependency is required.

## Source-link lifecycle

Resources keep a bounded set of account bindings. A principal switching accounts clears the previous account's locators. Canonical commit notifications, Vault commands, retention cleanup, and runtime shutdown discard affected links. A local task removes expired locators every 30 seconds; reads enforce expiry immediately.

An invalid or unbound evidence reference does not request a rebuild. A stale registered source or unavailable publication may enqueue recovery through the existing scheduler. Recovery still passes the licensed pipeline's admission check. Scheduling failure cannot make a refused result readable.

## Verification

Run `tests/test_analytics_query_handlers.py` for the 36 hand-authored expected cases. Run `tests/test_analytics_query_endpoints.py` for encrypted storage, publication, endpoint, lifecycle, and source-resolution checks. The endpoint tests distinguish synthetic known event kinds from the production adapter's missing metadata.

These checks do not qualify pricing language accuracy, laptop capacity, installer size, or Cosmos compatibility. The [qualification procedure](qualification.md) defines those separate gates.
