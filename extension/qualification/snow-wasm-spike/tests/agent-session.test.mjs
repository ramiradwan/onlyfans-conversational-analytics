import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { loadWasm } from "./helpers.mjs";
import { NoiseSession } from "../web/noise-session.mjs";
import { sessionPrologue, fromHex } from "../web/noise-binding.mjs";
import {
  PAIRING_DIGEST_HEX,
  AGENT_PRIVATE_HEX,
  AGENT_PUBLIC_HEX,
  BRAIN_PRIVATE_HEX,
  BRAIN_PUBLIC_HEX,
} from "../web/fixture.mjs";
import { createCompanionSession } from "../../../transport/companion-noise-session.mjs";
import { loadGrantTrustSet } from "../../../transport/grant-verifier.mjs";

const vector = JSON.parse(
  await readFile(
    new URL("../../../test-fixtures/pairing/local-pairing-vector.json", import.meta.url),
    "utf8",
  ),
);
const trust = await loadGrantTrustSet(vector.trust_set, { allowNonProduction: true });
const NOW = vector.now,
  NOT_AFTER = vector.expected.grants_not_after;
const encoder = new TextEncoder();
const authorization = (name) =>
  encoder.encode(
    name ? vector.authorization_cases.find((c) => c.name === name).text : JSON.stringify(vector.session_authorization),
  );

async function pair({ start = NOW, signal, commit = null, commitFails = false, identity } = {}) {
  const SnowSession = await loadWasm(),
    digest = fromHex(PAIRING_DIGEST_HEX);
  let wall = start,
    mono = 0,
    invalidated,
    fireTimer;
  const commits = [];
  const store = {
    onInvalidate(fn) {
      invalidated = fn;
      return () => {};
    },
    async sessionMaterial() {
      return {
        privateKey: fromHex(AGENT_PRIVATE_HEX),
        peerKey: fromHex(BRAIN_PUBLIC_HEX),
        pairingDigest: digest,
        identity: identity ?? vector.expected.identity,
        commit,
      };
    },
    async commit(token) {
      commits.push(token);
      if (commitFails) throw new Error("fence");
    },
  };
  const a = await createCompanionSession({
    store,
    SnowSession,
    trust,
    signal,
    clock: () => wall,
    monotonic: () => mono,
    schedule: (fn) => {
      fireTimer = fn;
      return 1;
    },
    unschedule: () => {},
  });
  const b = new NoiseSession({
    SnowSession,
    initiator: false,
    localPrivateKey: fromHex(BRAIN_PRIVATE_HEX),
    remotePublicKey: fromHex(AGENT_PUBLIC_HEX),
    prologue: sessionPrologue(digest),
  });
  return {
    a,
    b,
    commits,
    setTime(w, m) {
      wall = w;
      mono = m;
    },
    revoke: () => invalidated(),
    timer: () => fireTimer(),
  };
}
function confirm(a, b) {
  b.readHandshake(a.writeHandshake());
  a.readHandshake(b.writeHandshake());
  b.finishHandshake();
  b.readConfirmation(a.writeConfirmation());
  a.readConfirmation(b.writeConfirmation());
}
async function ready(a, b, record = authorization()) {
  confirm(a, b);
  return a.authorize(b.seal(record));
}
const closedWith = (code) => (error) => error?.code === code;

test("Agent exchanges Snow records only after the session authorization verifies", async () => {
  const { a, b } = await pair();
  try {
    confirm(a, b);
    assert.equal(a.state, "authorizing");
    assert.throws(() => a.seal(new Uint8Array([1])));
  } finally {
    a.close();
    b.close();
  }
  const p = await pair();
  try {
    const result = await ready(p.a, p.b);
    assert.equal(result.notAfter, NOW + 900);
    assert.equal(result.installation_id, vector.expected.identity.installation_id);
    const message = new Uint8Array([1, 2, 3]);
    assert.deepEqual(p.b.open(p.a.seal(message)), message);
    assert.deepEqual(p.a.open(p.b.seal(message)), message);
    assert.deepEqual(p.commits, []);
  } finally {
    p.a.close();
    p.b.close();
  }
});

test("a probation session commits its pin once, after authorization", async () => {
  const token = { requestId: "r", epoch: 0 };
  let p = await pair({ commit: token });
  try {
    await ready(p.a, p.b);
    assert.deepEqual(p.commits, [token]);
    assert.equal(p.a.state, "ready");
  } finally {
    p.a.close();
    p.b.close();
  }
  p = await pair({ commit: token, commitFails: true });
  try {
    await assert.rejects(ready(p.a, p.b), closedWith("session_commit_refused"));
    assert.equal(p.a.state, "closed");
  } finally {
    p.b.close();
  }
  p = await pair({ commit: token });
  try {
    await assert.rejects(ready(p.a, p.b, authorization("other-account")));
    assert.deepEqual(p.commits, []);
  } finally {
    p.b.close();
  }
});

test("refused authorizations close the session", async () => {
  const cases = [
    ["other-account", {}],
    ["other-installation-key", {}],
    ["unknown-member", {}],
    [null, { start: NOT_AFTER }],
    [null, { identity: { ...vector.expected.identity, creator_account_id: "creator-account-pairing-002" } }],
  ];
  for (const [name, options] of cases) {
    const p = await pair(options);
    try {
      await assert.rejects(ready(p.a, p.b, authorization(name)), closedWith("session_authorization_refused"));
      assert.equal(p.a.state, "closed");
    } finally {
      p.b.close();
    }
  }
  for (const [record, code] of [
    [(b) => b.seal(encoder.encode("application")), "session_authorization_refused"],
    [(b) => b.seal(authorization()).map((x, i) => (i === 0 ? x ^ 1 : x)), "session_authentication_failed"],
  ]) {
    const p = await pair();
    try {
      confirm(p.a, p.b);
      await assert.rejects(p.a.authorize(record(p.b)), closedWith(code));
      assert.equal(p.a.state, "closed");
    } finally {
      p.b.close();
    }
  }
});

test("the session ends at the earliest grant limit, the active deadline, rollback, revocation, or cancellation", async () => {
  for (const scenario of ["grace", "active", "rollback", "revoke", "cancel", "timer"]) {
    const controller = new AbortController(),
      p = await pair({
        start: scenario === "grace" ? NOT_AFTER - 10 : NOW,
        signal: controller.signal,
      });
    try {
      const result = await ready(p.a, p.b);
      if (scenario === "grace") {
        assert.equal(result.notAfter, NOT_AFTER);
        p.setTime(NOT_AFTER, 10);
      }
      if (scenario === "active") p.setTime(NOW + 900, 900);
      if (scenario === "rollback") p.setTime(NOW - 1, 1);
      if (scenario === "revoke") p.revoke();
      if (scenario === "cancel") controller.abort();
      if (scenario === "timer") p.timer();
      assert.throws(() => p.a.seal(new Uint8Array([1])));
      assert.equal(p.a.state, "closed");
    } finally {
      p.a.close();
      p.b.close();
    }
  }
});

test("Agent malformed records, early data and absolute handshake deadline permanently refuse", async () => {
  for (const scenario of ["early", "text", "oversize", "deadline", "replay"]) {
    const p = await pair();
    try {
      if (scenario === "early") assert.throws(() => p.a.seal(new Uint8Array([1])));
      else if (scenario === "deadline") {
        p.setTime(NOW + 2, 2);
        assert.throws(() => p.a.writeHandshake());
      } else {
        await ready(p.a, p.b);
        if (scenario === "text") assert.throws(() => p.a.open("secret"));
        if (scenario === "oversize") assert.throws(() => p.a.open(new Uint8Array(4097)));
        if (scenario === "replay") {
          const frame = p.b.seal(new Uint8Array([3]));
          p.a.open(frame);
          assert.throws(() => p.a.open(frame));
        }
      }
      assert.equal(p.a.state, "closed");
    } finally {
      p.a.close();
      p.b.close();
    }
  }
});

test("invalidation while reading durable material cannot resurrect a session", async () => {
  const SnowSession = await loadWasm();
  let invalidate;
  const store = {
    onInvalidate(fn) {
      invalidate = fn;
      return () => {};
    },
    async sessionMaterial() {
      invalidate();
      return {
        privateKey: new Uint8Array(32),
        peerKey: new Uint8Array(32),
        pairingDigest: new Uint8Array(32),
        identity: vector.expected.identity,
        commit: null,
      };
    },
  };
  await assert.rejects(createCompanionSession({ store, SnowSession, trust }));
});
