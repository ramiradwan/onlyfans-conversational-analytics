import test from "node:test";
import assert from "node:assert/strict";
import { loadWasm } from "./helpers.mjs";
import { NoiseSession } from "../web/noise-session.mjs";
import {
  spikePrologue,
  fromHex,
  receiptBinding,
} from "../web/noise-binding.mjs";
import {
  FIXED_CLAIMS,
  AGENT_PRIVATE_HEX,
  AGENT_PUBLIC_HEX,
  BRAIN_PRIVATE_HEX,
  BRAIN_PUBLIC_HEX,
} from "../web/fixture.mjs";
import { createCompanionSession } from "../../../transport/companion-noise-session.mjs";

async function pair({ lease = 1800000900, signal } = {}) {
  const SnowSession = await loadWasm(),
    binding = await receiptBinding(FIXED_CLAIMS);
  let wall = 1800000000,
    mono = 0,
    invalidated,
    fireTimer;
  const store = {
    onInvalidate(fn) {
      invalidated = fn;
      return () => {};
    },
    async sessionMaterial() {
      return {
        privateKey: fromHex(AGENT_PRIVATE_HEX),
        peerKey: fromHex(BRAIN_PUBLIC_HEX),
        binding,
        offlineNotAfter: lease,
      };
    },
  };
  const a = await createCompanionSession({
    store,
    SnowSession,
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
    prologue: spikePrologue(binding),
  });
  return {
    a,
    b,
    setTime(w, m) {
      wall = w;
      mono = m;
    },
    revoke: () => invalidated(),
    timer: () => fireTimer(),
  };
}
function ready(a, b) {
  b.readHandshake(a.writeHandshake());
  a.readHandshake(b.writeHandshake());
  b.finishHandshake();
  b.readConfirmation(a.writeConfirmation());
  a.readConfirmation(b.writeConfirmation());
}
test("Agent adapter exchanges actual Snow records after mutual confirmation", async () => {
  const { a, b } = await pair();
  try {
    ready(a, b);
    const message = new Uint8Array([1, 2, 3]);
    assert.deepEqual(b.open(a.seal(message)), message);
    assert.deepEqual(a.open(b.seal(message)), message);
  } finally {
    a.close();
    b.close();
  }
});
test("active deadline, offline lease, clock rollback, revocation and cancellation close keys", async () => {
  for (const scenario of [
    "active",
    "offline",
    "rollback",
    "revoke",
    "cancel",
    "timer",
  ]) {
    const controller = new AbortController(),
      p = await pair({
        lease: scenario === "offline" ? 1800000010 : 1800009900,
        signal: controller.signal,
      });
    try {
      ready(p.a, p.b);
      if (scenario === "active") p.setTime(1800000900, 900);
      if (scenario === "offline") p.setTime(1800000010, 10);
      if (scenario === "rollback") p.setTime(1799999999, 1);
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
      if (scenario === "early")
        assert.throws(() => p.a.seal(new Uint8Array([1])));
      else if (scenario === "deadline") {
        p.setTime(1800000002, 2);
        assert.throws(() => p.a.writeHandshake());
      } else {
        ready(p.a, p.b);
        if (scenario === "text") assert.throws(() => p.a.open("secret"));
        if (scenario === "oversize")
          assert.throws(() => p.a.open(new Uint8Array(4097)));
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
        binding: new Uint8Array(32),
        offlineNotAfter: Date.now() / 1000 + 900,
      };
    },
  };
  await assert.rejects(createCompanionSession({ store, SnowSession }));
});
