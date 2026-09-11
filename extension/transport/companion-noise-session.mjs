import { PairingFailure, requirePairing } from "./pairing-contract.mjs";

const enc = new TextEncoder(),
  MAX_FRAME = 4096,
  MAX_RECORDS = 1048576;
const ready = enc.encode("client-ready"),
  peerReady = enc.encode("server-ready");

// Agent-only record adapter. The composition layer supplies a packaged Snow
// constructor, admitted store material, and authenticated revocation delivery.
// No socket, hosted client, shipping CSP change, or transport fallback is installed.
export async function createCompanionSession({
  store,
  SnowSession,
  accountId,
  trustSequence,
  signal,
  maxActiveSessionSeconds = 900,
  clock = () => Date.now() / 1000,
  monotonic = () => performance.now() / 1000,
  schedule = setTimeout,
  unschedule = clearTimeout,
}) {
  requirePairing(
    Number.isSafeInteger(maxActiveSessionSeconds) &&
      maxActiveSessionSeconds > 0 &&
      maxActiveSessionSeconds <= 900,
  );
  let state = "handshake",
    inner,
    timer,
    unsubscribe,
    count = 0,
    sent = false;
  const start = clock(),
    monoStart = monotonic();
  requirePairing(Number.isFinite(start) && Number.isFinite(monoStart));
  let deadline,
    phaseDeadline = monoStart + 2,
    lastWall = start,
    lastMono = monoStart;
  const failure = (code) => {
    close();
    throw new PairingFailure(code);
  };
  const abort = () => close();
  function close() {
    if (state === "closed") return;
    state = "closed";
    unschedule(timer);
    unsubscribe?.();
    signal?.removeEventListener("abort", abort);
    try {
      inner?.close();
    } catch {
    } finally {
      try {
        inner?.free();
      } catch {}
      inner = null;
    }
  }
  function check(expected) {
    const wall = clock(),
      mono = monotonic();
    if (
      state === "closed" ||
      signal?.aborted ||
      !Number.isFinite(wall) ||
      !Number.isFinite(mono) ||
      wall < lastWall ||
      mono < lastMono ||
      wall >= deadline ||
      mono >= phaseDeadline
    )
      failure("session_expired");
    lastWall = wall;
    lastMono = mono;
    if (state !== expected) failure("session_state_refused");
  }
  function guarded(fn) {
    try {
      return fn();
    } catch {
      return failure("session_authentication_failed");
    }
  }
  function frame(value) {
    if (
      !(value instanceof Uint8Array) ||
      value.length < 1 ||
      value.length > MAX_FRAME
    )
      failure("session_frame_refused");
    return value;
  }
  function arm() {
    unschedule(timer);
    timer = schedule(
      close,
      Math.max(
        0,
        Math.min(deadline - clock(), phaseDeadline - monotonic()) * 1000,
      ),
    );
  }
  unsubscribe = store.onInvalidate(close);
  let material;
  try {
    material = await store.sessionMaterial({
      accountId,
      trustSequence,
      signal,
    });
  } catch {
    close();
    throw new PairingFailure("session_initialization_refused");
  }
  try {
    requirePairing(state !== "closed" && !signal?.aborted);
    deadline = Math.min(
      start + maxActiveSessionSeconds,
      material.offlineNotAfter,
    );
    requirePairing(clock() < deadline && monotonic() < phaseDeadline);
    const prefix = enc.encode(
      "ofca-session-spike/v1;agent-to-brain;no-early-data\0",
    );
    const prologue = new Uint8Array(prefix.length + 32);
    prologue.set(prefix);
    prologue.set(material.binding, prefix.length);
    inner = new SnowSession(
      true,
      material.privateKey,
      material.peerKey,
      prologue,
    );
  } catch {
    close();
    throw new PairingFailure("session_initialization_refused");
  } finally {
    material.privateKey.fill(0);
  }
  signal?.addEventListener("abort", abort, { once: true });
  arm();
  return Object.freeze({
    get state() {
      return state;
    },
    close,
    writeHandshake() {
      check("handshake");
      if (sent) failure("session_state_refused");
      sent = true;
      return frame(guarded(() => inner.write_handshake()));
    },
    readHandshake(value) {
      check("handshake");
      if (!sent) failure("session_state_refused");
      guarded(() => inner.read_handshake(frame(value)));
      if (!inner.handshake_finished()) failure("session_authentication_failed");
      guarded(() => inner.enter_transport());
      state = "confirming";
      sent = false;
    },
    writeConfirmation() {
      check("confirming");
      if (sent) failure("session_state_refused");
      sent = true;
      const record = new Uint8Array(ready.length + 1);
      record.set(ready, 1);
      return frame(guarded(() => inner.encrypt_transport(record)));
    },
    readConfirmation(value) {
      check("confirming");
      if (!sent) failure("session_state_refused");
      const plain = guarded(() => inner.decrypt_transport(frame(value)));
      if (
        plain.length !== peerReady.length + 1 ||
        plain[0] !== 0 ||
        !peerReady.every((v, i) => plain[i + 1] === v)
      )
        failure("session_authentication_failed");
      state = "ready";
      phaseDeadline =
        monoStart +
        Math.min(maxActiveSessionSeconds, material.offlineNotAfter - start);
      arm();
    },
    seal(value) {
      check("ready");
      if (
        !(value instanceof Uint8Array) ||
        value.length > MAX_FRAME - 17 ||
        ++count > MAX_RECORDS
      )
        failure("session_frame_refused");
      const record = new Uint8Array(value.length + 1);
      record[0] = 1;
      record.set(value, 1);
      return frame(guarded(() => inner.encrypt_transport(record)));
    },
    open(value) {
      check("ready");
      if (++count > MAX_RECORDS) failure("session_frame_refused");
      const plain = guarded(() => inner.decrypt_transport(frame(value)));
      if (plain[0] !== 1) failure("session_authentication_failed");
      return plain.slice(1);
    },
  });
}
