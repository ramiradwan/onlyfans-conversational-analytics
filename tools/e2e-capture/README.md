# Capture E2E harness

This is the deterministic capture gate. It runs a real headed Chromium MV3 service worker from the
audited `extension/dist` artifact, a one-worker FastAPI Brain with separate canonical and projection
SQLite databases at `http://bridge.localhost:17871`, and the compiled Bridge SPA. A
fail-closed Playwright fixture replaces only the upstream platform at its real HTTPS/WSS origins;
no live account, cookie, credential, or response is used.

## Run locally

From the product root:

```powershell
./.venv/Scripts/python -m pip install -r requirements-dev.txt
npm ci --prefix frontend
npm run build --prefix frontend
npm ci --prefix extension
npm run build --prefix extension
npm run audit --prefix extension
npm ci --prefix tools/e2e-capture
npm run install:browser --prefix tools/e2e-capture
npm test --prefix tools/e2e-capture
```

Linux CI runs headed Chromium beneath Xvfb. Set `OFCA_E2E_PYTHON` when the desired interpreter is
not the product `.venv` or `python` on `PATH`.

## Exact gate

A pass proves all of the following:

- The unbound Agent is paired with a one-time ticket from `POST /api/v1/agent/pairing`, delivered
  through Chrome external messaging from the exact Bridge origin to Chromium's actual unpacked
  extension ID. The protocol-v2 session then uses distinct reconnect and config credentials.
- The MAIN-world hook, isolated bridge, audited MV3 worker, fenced Agent session, required
  chat/message configuration, heartbeat, account-partitioned durable IndexedDB outbox, and
  one-minute production reconciliation alarm are active.
- The initial fixture creates exact source sequences 1–6: one explicit `chat.upsert`, three history
  `message.upsert` events, then an atomic synthesized parent `chat.upsert` plus `message.upsert` for
  a message-only WebSocket peer. The outbox reaches exactly `6/6`, with two chats, four messages,
  no pending entries, and no drops.
- Canonical SQLite contains exactly contiguous sequences `[1, 2, 3, 4, 5, 6]`, checkpoint 6, two
  account-keyed chats, and four account-keyed messages. Projection SQLite contains two conversation
  summaries, four paged messages, four NLP rows, and the matching LPG graph in the canonically
  activated slot. The prior inactive slot retains only three messages as an initial stale-data
  witness without leaking into Brain or Bridge reads. The WebSocket snapshot has no
  historical message arrays; the compiled Inbox fetches both conversations through authenticated
  REST pages and renders all four messages. Partial analytics render as qualified lower bounds.
- With Brain stopped, a second message-only peer creates exact pending parent/message sequences
  `[7, 8]`; acknowledgment remains 6 and both distinct event IDs are retained.
- After a normal service-worker stop and Brain restart against the same SQLite file, the harness
  explicitly starts the registered worker without a page or extension reload. A new worker-lifetime
  nonce replays the same event IDs and reaches exactly `8/8`; SQLite contains only sequences 1–8,
  checkpoint/event count 8, three chats, and five messages. Canonical activation selects the new
  3-chat/5-message projection slot while the retained inactive slot has only four messages and their
  NLP/LPG rows. Whether the immediately preceding slot includes the synthesized parent depends on
  legitimate durable-work coalescing; Brain summaries, REST pages, and Inbox rows must still match
  only the active slot. The proof also rejects more than two slots for an account.
- A second normally stopped and explicitly started worker lifetime resumes at acknowledgment 8
  without replaying acknowledged events.
  Event IDs, sequences, types, counts, checkpoints, active/inactive slot identities and
  cardinalities, NLP/LPG rows, and Bridge revision remain unchanged.
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

Each run creates a unique OS temporary directory for its browser profile and both SQLite databases. In a
`finally` path it closes the tracked browser context, stops only its tracked Brain process, verifies
port 17871 is released, and removes that temporary directory. It refuses to reuse, stop, or delete
an unrelated listener, process, profile, database, or directory.
