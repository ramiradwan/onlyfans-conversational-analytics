# Capture E2E harness

This is the deterministic capture gate. It runs a real headed Chromium MV3 service worker and
unpacked extension, a one-worker FastAPI Brain with SQLite, and the compiled Bridge SPA. A
fail-closed Playwright fixture replaces only the upstream platform at its real HTTPS/WSS origins;
no live account, cookie, credential, or response is used.

## Run locally

From the product root:

```powershell
./.venv/Scripts/python -m pip install -r requirements-dev.txt
npm ci --prefix frontend
npm run build --prefix frontend
npm ci --prefix tools/e2e-capture
npm run install:browser --prefix tools/e2e-capture
npm test --prefix tools/e2e-capture
```

Linux CI runs headed Chromium beneath Xvfb. Set `OFCA_E2E_PYTHON` when the desired interpreter is
not the product `.venv` or `python` on `PATH`.

## Exact gate

A pass proves all of the following:

- The MAIN-world hook, isolated bridge, MV3 worker, fenced Agent session, required chat/message
  configuration, heartbeat, durable IndexedDB outbox, and one-minute production reconciliation
  alarm are active.
- The initial fixture creates exact source sequences 1–6: one explicit `chat.upsert`, three history
  `message.upsert` events, then an atomic synthesized parent `chat.upsert` plus `message.upsert` for
  a message-only WebSocket peer. The outbox reaches exactly `6/6`, with two chats, four messages,
  no pending entries, and no drops.
- SQLite contains exactly contiguous sequences `[1, 2, 3, 4, 5, 6]`, checkpoint 6, two canonical
  and read-model chats, and four canonical and read-model messages. The compiled Inbox reaches
  `Live`, renders both conversations and all four messages, and Brain observes a newer heartbeat on
  the same connection.
- With Brain stopped, a second message-only peer creates exact pending parent/message sequences
  `[7, 8]`; acknowledgment remains 6 and both distinct event IDs are retained.
- After a normal service-worker stop and Brain restart against the same SQLite file, the harness
  explicitly starts the registered worker without a page or extension reload. A new worker-lifetime
  nonce replays the same event IDs and reaches exactly `8/8`; SQLite contains only sequences 1–8,
  checkpoint/event count 8, three chats, and five messages.
- A second normally stopped and explicitly started worker lifetime resumes at acknowledgment 8
  without replaying acknowledged events.
  Event IDs, sequences, types, counts, checkpoints, cardinalities, and Bridge revision remain
  unchanged.
- A third normally stopped worker remains absent until Brain hard-retires its shortened test lease:
  `agent.state` is disconnected with null connection and heartbeat, and no replacement worker has
  appeared. The production alarm alone then creates a new worker-lifetime nonce and connection,
  restores heartbeat and `8/8`, leaves canonical state unchanged, and returns the Inbox to `Live`.
- The platform document token and route counts prove that no recovery step reloaded the page or
  repeated a platform read. Capture drops, page errors, mutation attempts, and unexpected network
  requests remain zero.

An empty observation set, sequence zero, missing pending 7–8, changed replay IDs, or recovery caused
by a page/extension reload is a failure, not resilience evidence.

Chromium may reuse a service-worker CDP target ID after a normal stop. The gate therefore requires
both a period with zero running extension worker targets and a changed per-lifetime nonce; target
identity alone is not lifecycle proof. The alarm-only phase intentionally makes a passing run take
roughly 90 seconds.

## Privacy, safety, and teardown

All identities and text are synthetic. External requests and non-GET platform traffic are aborted.
The test never reads cookies, exports authorization material, or performs platform mutations.
Screenshots, traces, video, and Brain access logs are disabled; bounded startup diagnostics remain
in memory.

Each run creates a unique OS temporary directory for its browser profile and SQLite database. In a
`finally` path it closes the tracked browser context, stops only its tracked Brain process, verifies
port 8000 is released, and removes that temporary directory. It refuses to reuse, stop, or delete
an unrelated listener, process, profile, database, or directory.
