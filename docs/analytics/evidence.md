<!-- CODE-VERIFY: Check evidence.py, evidence_contracts.py, canonical_source.py, query_contracts.py, and tests/test_analytics_evidence.py before editing lookup or lifecycle claims. -->

# Resolve an analytics source

`EvidenceResolver` connects a question's source reference to an exact canonical message. It returns source text, native conversation/message identifiers, direction, and checked time. It does not classify text, rebuild analytics, or enable an HTTP route.

The [question contract](questions.md) defines evidence semantics. The [question service](question-service.md) owns query scope and projection freshness.

## Register and resolve

Compose one resolver with the live `HistoryAnalyticsSource`. Supply a fresh `RuntimePolicy` from the authenticated runtime for every `bind`, `resolve`, and `clear_account` call. Account authority comes from the security kernel, not request fields. The resolver does not validate sessions or renew grants.

A query adapter reads its selected source through `read_evidence_message` and creates a reference with `EvidenceMessage.reference`. Verify that its revision matches the selected query snapshot. The digest covers account, native identifiers, text, sender, direction, event time, stored content hash, upstream update time, and source-order metadata. The account revision is a separate reference field.

Call `bind(policy, reference, location, valid_until=...)` before returning the reference. Pass the earliest expiry of every input needed by the finding. Binding rechecks the supplied version; it must not create a new version to justify an old finding.

Call `resolve(policy, reference)` to open the source. Resolution checks the reference and reads the message twice through fresh canonical connections. Both reads must match the reference's account revision, source digest, identifiers, and timestamp. Any canonical revision change requires rerunning the question, even if that particular message is unchanged.

`ResolvedEvidence.location` identifies the conversation and message for local navigation. It is not a URL or an authorization grant. An HTTP adapter must use existing activation and session dependencies, send `Cache-Control: no-store`, and return no text on failure. Render text as text, not HTML. HTTP and interface composition remain separate from this component.

## Refusals

Deleted records, message/chat tombstones, account/conversation/message/participant deletion barriers, and retained participant deletion scopes prevent resolution. Parent conversation deletion also prevents access. The source-time 90-day limit applies independently of archive storage or the question cutoff.

A source edit, revision change, invalid span, missing row, or source-read failure returns a fixed unavailable error without content. Stale source checks clear account locators. An optional `request_refresh` callback can enqueue permitted recovery; it must not build inline or bypass analysis admission. A callback failure cannot restore a rejected result or expose its exception.

The final canonical read is the source validation point. It does not retract text already delivered before a later deletion. Callers must invalidate displayed results on account or revision changes.

## Locator lifetime

Only canonical identifiers and deadlines are cached, not source text. The process-local cache holds at most 4,096 entries by default, with a configurable ceiling of 32,768. A locator expires after 15 minutes or earlier input expiry. Reading it never renews the deadline. Restart, eviction, or expiry requires rerunning the question.

Call `clear_account` during account-switch and deletion handling, and `expire` from local maintenance. Clearing fences pending bindings and reads. Binding and resolution also remove expired entries. There is no new persistent lookup database or maintenance thread.

## Bounded reads and spans

Each canonical lookup uses the account/message primary key and checks the parent conversation. Database progress callbacks propagate cancellation and the remaining processing timeout. The gateway refuses a caller-supplied snapshot connection because it cannot establish live state.

One resolution performs two single-message reads within a one-second cooperative budget. Connection opening uses the existing database lifecycle; late completion is rejected, not forcibly interrupted. Messages exceeding 65,536 code points or 262,144 UTF-8 bytes are unavailable through this surface, without changing canonical data. Native identifiers are limited to 512 characters.

Source spans use Unicode code-point offsets into the exact text version. `browser_span` converts them to UTF-16 code-unit offsets for browser strings. Empty message text is valid. An out-of-range span is unavailable rather than silently clipped.

## Verify

```powershell
python -m pytest tests/test_analytics_evidence.py tests/test_analytics_question_service.py tests/test_analytics_question_contracts.py
```

The evidence tests exercise encrypted canonical storage, exact source text, version changes, deletion barriers, expiry, account isolation, callback failures, cache limits, concurrent invalidation, and indexed query plans. They do not establish classifier quality, HTTP activation, or laptop capacity. Runtime composition must connect the locator lifecycle and recovery hooks before enabling evidence access.
