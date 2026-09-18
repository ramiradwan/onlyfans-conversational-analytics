<!-- CODE-VERIFY: Check query_contracts.py, query_cursor.py, query_execution.py, query_service.py, runtime_policy.py, and their tests before changing interface or limit claims. -->

# Execute an analytics question

`AnalyticsQuestionService` validates approved questions and delivers bounded pages from a pinned, read-only projection. It does not register a database adapter, enable an HTTP endpoint, run inference, or rebuild projections.

The [question contract](questions.md) defines what the questions mean. The service owns execution constraints, not topic classification or source-text resolution.

## Compose the service

Supply a `QuestionReader` and explicit `RegisteredQuestion` entries. Each entry names an approved question, an implementation revision, and a handler. Unsupported or duplicate registrations are rejected. A question without a registered handler is unavailable, not an empty result.

Keep the service instance for the runtime lifetime. Its cursor key is random and process-local unless composition supplies a key. Restarting with a new key invalidates existing page tokens; no new key store is required.

Call `execute` with a fresh `RuntimePolicy` from the existing authenticated runtime path. Account selection uses `authorized_account` in the security kernel. The service does not authenticate sessions, renew grants, or authorize new analysis.

## Validate the request

`QuestionPlan` accepts only the two versioned question identifiers, offset-aware start/end instants, a recognized IANA timezone, declared filters, an optional historical cutoff, sort, page size, and cursor. It rejects account selectors, raw queries, client work limits, naive timestamps, and unknown fields.

Calendar dates must be resolved to offset-aware instants before constructing the plan. The plan normalizes those instants to UTC and preserves the timezone label. It does not interpret bare dates or guess an offset during a daylight-saving transition.

The service fixes the cutoff on the first page and preserves it across later pages. Selection end cannot exceed that cutoff, and the cutoff cannot be in the future. The result retains the requested interval and separately identifies retention clipping and evaluated coverage.

## Implement the read adapter

`QuestionReader.open(account_ref, budget)` returns a context-managed `QuestionReadSession`. Its snapshot contains the account, canonical revision and digest, projection generation and identity, derivation time, source count, and earliest required-input expiry.

`assert_current(snapshot, budget)` must recheck the active generation and its canonical witness using the existing publication rules. It must raise the existing unavailable/building/error failure when the snapshot is no longer permitted. The service calls it before and after the handler, including for empty results.

The snapshot's retention deadline covers all required inputs, not only the source references displayed on the current page. The service checks expiry before execution and before returning data. It also verifies displayed reference times and revisions. Source-version digests identify evidence; resolving or verifying its text belongs to the [canonical evidence reader](evidence.md).

Handlers must use indexed, account-scoped selection and keyset paging. They must not materialize an account through `nodes()`, `edges()`, or a full projection document. The `after` position is the last returned evidence time and conversation reference, not an offset into changing results.

Charge examined records with `budget.consume` and call `budget.check` inside bounded loops. Use `budget.remaining_seconds()` for downstream operation timeouts. Database progress handlers and bounded loops must also check cancellation and the shared deadline. These are cooperative limits: the service rejects late output but cannot preempt an arbitrary blocking adapter. Adapter qualification must prove that its actual reads obey them.

Default limits are 10,000 examined records and 1,000 milliseconds per request. Only composition can configure them, up to 100,000 records and 30,000 milliseconds. Pages contain at most 200 conversations, 32 evidence references per row, and 512 references overall. The default page size is 50.

## Return a page

`QuestionPage` carries ordered, distinct conversation rows, their source references, coverage, and page-local evaluated and undetermined counts. An optional total counts matches across the complete query, not just the page. Leave that total absent unless it is known exactly.

`has_more` means ordinary pagination can continue. `truncated` means the computation stopped short; it cannot carry an exact total or a continuation token. Missing classifications, incomplete history, and unknown ordering are not negative findings.

Each row must match the account, question reason, selected conversation filter, source revision, retention window, and cutoff. Pricing evidence must also lie inside the half-open selection interval. The service rejects duplicate or misordered rows and malformed nested records.

The service authenticates cursors with HMAC. Tokens bind the account, normalized plan, cutoff, handler definition, complete snapshot identity, and page position. They expire after 15 minutes or earlier source expiry. Continuing a page does not renew that lifetime. Changed filters, account, source, generation, or implementation revision invalidate the token.

Results contain no raw message text. Unexpected adapter exceptions become a fixed public error. Callers must not log full requests, cursor payloads, or validation inputs.

## Verify the boundary

```powershell
python -m pytest tests/test_analytics_question_contracts.py tests/test_analytics_question_service.py tests/test_analytics_question_packaging.py
```

The service tests use prepared rows derived from the hand-authored question cases. They verify delivery, isolation, pagination, metadata, and refusals. They do not execute pricing or reply-query algorithms and do not establish model quality or database capacity.

## Timezone data

`tzdata==2026.4` supplies IANA timezone data on systems without it. The installer includes the data and package metadata through the PyInstaller spec. It does not require a customer-side download. Timezone files are not model weights or an inference runtime.

Keep the [local analysis and package limits](local-analysis.md) unchanged for optional ML. Measure installer and installed-size deltas separately; a dependency's wheel size is not the complete application footprint.
