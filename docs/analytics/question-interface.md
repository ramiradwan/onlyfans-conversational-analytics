<!-- CODE-VERIFY: Check ConversationQuestions.tsx, QuestionSourceDialog.tsx, questionStore.ts, questionApi.ts, questionContract.ts and questionWindow.ts before changing behavior claims. -->

# Use conversation questions

Analytics has Summary and Conversation questions tabs. Question dates apply only to the question tab. The authenticated [question endpoints](question-endpoints.md) supply results and availability.

## Questions and dates

The question menu follows the server's availability response. Pricing stays disabled until its language-quality gate passes. The interface does not infer missing message types.

Dates use the browser's local timezone. The end date includes that day up to the current time. Follow-up extends through the separately displayed analysis cutoff. Invalid, reversed, and future dates are rejected.

Previous and next controls fetch pages from the same snapshot. At most 50 page requests are retained for navigation. Only the current result page is stored. Conversation numbers identify rows on that page.

An empty result is separate from conversations whose status could not be determined. Partial history, expired history, and truncated calculations have separate notices. Internal identifiers and unverified server error text are not displayed.

## Sources and conversation history

Select a matching message to resolve its current source. Open conversation then loads saved messages from the existing history API. Older pages replace the current page. Text is rendered as text, never as HTML.

The matching message remains above the conversation history, even when it is outside the latest page. Missing or changed sources clear the result rather than displaying replacement text as evidence.

Account, session, source revision, connection, and view changes cancel pending requests and clear results. Page responses from a different snapshot are rejected. Late responses cannot restore cleared data. Leaving Analytics drops local result and source state; server source links retain their existing bounded lifetime.

Displayed results expire after 15 minutes or earlier source expiry. Paging does not extend that deadline. Source text is not written to browser storage, URLs, logs, or a client cache.

## Verify the interface

Run the frontend tests and build. `questionWire.test.ts` checks the client against synthetic responses from the authenticated SQLite endpoint fixture. Component and lifecycle tests cover unavailable and unknown results, pagination, source changes, expiry, and account isolation.

For the browser check, install the locked dependencies in `frontend` and `tools/visual-capture`. Start Vite from `frontend` on `127.0.0.1:5187` with automatic browser opening disabled. Then run from the repository root:

```sh
node tools/visual-capture/question-interface.mjs /absolute/path/to/new-output
```

The test page uses synthetic responses and is not a production entry point. The check exercises keyboard submission, pagination, source text, history loading, source invalidation, narrow layout, and accessibility. It records screenshots and a report.

These checks do not establish pricing accuracy, production message-type evidence, laptop capacity, or installer size. No model or dependency download is introduced by the interface.
