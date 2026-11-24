import { uiSurface } from './ui-surfaces.mjs';
import { isReadiness, isReadinessRequest, READINESS_RESPONSE } from './surface-readiness.mjs';
import { openPairingStore } from './companion-pairing-store.mjs';
import { loadPackagedSnow } from './packaged-snow.mjs';
import { signAgentSessionProof, snowKeypairGenerator } from './companion-agent-identity.mjs';
import { openCompanionChannel, openLoopbackSocket, CompanionChannelError, safeCompanionCloseReason, safeCompanionChannelDiagnostic } from '../transport/companion-channel.mjs';
import { parseMessage } from '../transport/pairing-contract.mjs';
import { loadGrantTrustSet } from '../transport/grant-verifier.mjs';
import { LOCAL_SERVICE_WS, LOCAL_PAIRING_WS } from '../transport/local-service-endpoints.mjs';
import { createConnectionRecovery, CONNECTION_STABLE_MS, CONNECTION_RECONCILE_ALARM } from './recovery-backoff.mjs';
import { SIGNING_PARAMETER_METHOD, validateParameterRequest } from '../protocol/signing-parameters.mjs';
import { PARAMETER_ALARM } from './signing-parameters.mjs';

export const PAIRING_PORT_NAME = 'ofca.companion.pairing';
const INSTALLATION_KEY = 'agent_installation_id';
const ACTIVE_PARTITION_KEY = 'active_account_partition_v5';
const ANALYSIS_READINESS_SCHEMA = 'ofca-analysis-readiness/v1';
const failure = () => new CompanionChannelError();
class ReadinessSuperseded extends Error {}
const exact = (value, fields) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === fields.length && fields.every((field) => Object.hasOwn(value, field));
const secret = (value) => typeof value === 'string' && value.length > 0 && value.length <= 16_384
  && /^[\x21-\x7e]+$/u.test(value);
function abortable(operation, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => reject(failure());
    signal.addEventListener('abort', abort, { once: true });
    Promise.resolve(operation).then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
    if (signal.aborted) abort();
  });
}
function validatedAnalysisReadiness(value) {
  if (!exact(value, ['schema', 'commercial_authority', 'analysis_admission'])
    || value.schema !== ANALYSIS_READINESS_SCHEMA
    || !isReadiness(value)) throw failure();
  return Object.freeze({
    commercial_authority: value.commercial_authority,
    analysis_admission: value.analysis_admission,
  });
}

export function createCompanionClient({
  chromeApi = globalThis.chrome, allowsFull, detectedAccountId,
  // Paused Full keeps a control-only session so the desktop app can resume it.
  allowsControl = () => false, onControl = async () => {}, onBrowserTask = null, onBrowserTaskEnd = null,
  onBrowserTaskWaiting = null, readFullPresentationIntent = null,
  readCaptureControlStatus = null, onCaptureControl = null,
  readinessConsentEpoch = () => null,
  accountDatabaseName,
  storeFactory = openPairingStore, loadSnow = loadPackagedSnow,
  channelFactory = openCompanionChannel, wireFactory = openLoopbackSocket,
  now = Date.now, random = Math.random, scheduler = null,
  loadTrust = async () => loadGrantTrustSet(await (await fetch(chromeApi.runtime.getURL('companion-grant-trust.json'))).json()),
} = {}) {
  let storePromise, trustPromise, installationPromise, connecting = null, connectingAccount = null, connectionAbort = null, active = null;
  let generation = 0, pairingAbort = null, state = { state: 'unpaired', comparison_code: null };
  const recovery = createConnectionRecovery({ storage: chromeApi.storage.local, now, random });
  const timers = scheduler ?? globalThis;
  const subscribers = new Set();
  const diagnosticEvents = [];
  const credentialRotation = { attempts: 0, completed: 0, failure: null };
  const record = (event, detail = {}) => {
    diagnosticEvents.push({ at: now(), event, ...detail });
    if (diagnosticEvents.length > 24) diagnosticEvents.shift();
  };
  let pairingOwner = null;
  const store = () => {
    if (!storePromise) {
      const opening = Promise.resolve().then(storeFactory);
      storePromise = opening;
      void opening.catch(() => { if (storePromise === opening) storePromise = null; });
    }
    return storePromise;
  };
  const trust = () => trustPromise ??= loadTrust();
  const installationId = () => installationPromise ??= (async () => {
    const saved = await chromeApi.storage.local.get([INSTALLATION_KEY]);
    const id = saved[INSTALLATION_KEY] ?? crypto.randomUUID();
    if (typeof id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/u.test(id)) throw failure();
    await chromeApi.storage.local.set({ [INSTALLATION_KEY]: id });
    return id;
  })();
  function announce(next) { state = next; for (const notify of subscribers) notify({ ...state }); }
  function invalidate() {
    record('invalidate');
    generation++;
    closeControl();
    pairingAbort?.abort();
    connectionAbort?.abort();
    if (active && active.stableTimer !== null) timers.clearTimeout(active.stableTimer);
    active?.channel.close(); active = null;
    connecting = null;
  }
  async function permitted(expected = null) {
    if (!allowsFull?.()) throw failure();
    const account = await detectedAccountId();
    if (typeof account !== 'string' || !account || (expected !== null && account !== expected)) throw failure();
    return account;
  }
  async function connect(controls = {}, requestId, onConnectionStart) {
    const controller = new AbortController();
    const signal = controls.signal ? AbortSignal.any([controller.signal, controls.signal]) : controller.signal;
    const timer = setTimeout(() => controller.abort(), 10_000);
    try {
      const connected = await abortable(connectCurrent({ ...controls, signal }, requestId, onConnectionStart), signal);
      signal.throwIfAborted(); controls.assertCurrent?.();
      return connected;
    }
    catch (error) {
      record('connect-failed', { reason: safeCompanionCloseReason(error?.code ?? 'other') });
      throw error;
    }
    finally { clearTimeout(timer); }
  }
  async function connectCurrent(controls, requestId, onConnectionStart) {
    controls.signal?.throwIfAborted(); controls.assertCurrent?.();
    const accountId = await permitted();
    controls.signal.throwIfAborted(); controls.assertCurrent?.();
    if (active && active.accountId !== accountId) invalidate();
    if (active && !active.channel.closed && active.accountId === accountId) {
      return active;
    }
    if (connecting) {
      if (connectingAccount !== accountId) { invalidate(); throw failure(); }
      const connected = await connecting;
      controls.signal.throwIfAborted(); controls.assertCurrent?.();
      await permitted(accountId);
      return connected;
    }
    const version = generation;
    const controller = new AbortController();
    connectionAbort = controller;
    onConnectionStart?.(controller);
    const timer = setTimeout(() => controller.abort(), 10_000);
    connectingAccount = accountId;
    const current = () => {
      controller.signal.throwIfAborted();
      if (generation !== version || !allowsFull?.()) throw failure();
    };
    const handshakeControls = { signal: controller.signal, assertCurrent: current };
    const operation = (async () => {
      record('connect-start');
      // The persisted circuit limits automatic reconnects. A freshly confirmed
      // pairing is already a single user-owned, deadline-bounded attempt and
      // must not be rejected by a cooldown earned before a pin existed.
      if (requestId === undefined) await recovery.reserve();
      current();
      const pairingStore = await store();
      const snow = await loadSnow();
      current(); await permitted(accountId);
      const channel = await channelFactory({
        url: LOCAL_SERVICE_WS, store: pairingStore, SnowSession: snow.SnowSession,
        accountId, requestId, trust: await trust(), signal: controller.signal,
      });
      try {
        current(); await permitted(accountId);
        if (channel.identity.creator_account_id !== accountId) throw failure();
        const agentInstallationId = await installationId();
        const authorized = await authenticateChannel(channel, pairingStore, accountId, agentInstallationId, handshakeControls,
          async () => { current(); await permitted(accountId); });
        const unlocked = await channel.rpc('agent.storage.unseal', { storage_bootstrap: authorized.storage_bootstrap }, handshakeControls);
        if (!exact(unlocked, ['schema', 'creator_account_id', 'credential_kind', 'auth_ticket', 'storage_key_base64'])
          || unlocked.schema !== 'ofca-extension-storage-unlock/v1' || unlocked.creator_account_id !== accountId
          || !['pairing', 'reconnect'].includes(unlocked.credential_kind)
          || unlocked.auth_ticket !== authorized.auth_ticket || typeof unlocked.storage_key_base64 !== 'string'
          || !/^[A-Za-z0-9+/]{43}=$/u.test(unlocked.storage_key_base64)
          || atob(unlocked.storage_key_base64).length !== 32
          || btoa(atob(unlocked.storage_key_base64)) !== unlocked.storage_key_base64) throw failure();
        current(); await permitted(accountId);
        active = { channel, accountId, openedAt: now(), stableTimer: null,
          authTicket: authorized.auth_ticket, storageKey: unlocked.storage_key_base64,
          storageBootstrap: authorized.storage_bootstrap, agentInstallationId };
        const admitted = active;
        record('connect-admitted');
        closeControl();
        attachControls(channel);
        notifySurfaces();
        void sendSurface();
        scheduleStable(admitted);
        channel.onClose(() => {
          if (active?.channel !== channel) return;
          const wasStable = !active.rotationPending && now() - active.openedAt >= CONNECTION_STABLE_MS;
          const resetOnClose = wasStable && active.stableTimer !== null;
          record('channel-close', { code: channel.closeCode ?? null,
            reason: safeCompanionCloseReason(channel.closeReason), wasStable,
            channel: safeCompanionChannelDiagnostic(channel.closeDiagnostic) });
          if (active.stableTimer !== null) timers.clearTimeout(active.stableTimer);
          active = null;
          notifySurfaces();
          // Queue the reset before any subsequent reserve; a quiet healthy
          // connection need not have been polled by an extension UI.
          if (wasStable) {
            if (resetOnClose) record('circuit-reset');
            void recovery.stable().catch(() => undefined);
          }
        });
        return active;
      } catch { channel.close(); throw failure(); }
    })();
    const bounded = abortable(operation, controller.signal);
    connecting = bounded;
    try { return await bounded; } finally {
      clearTimeout(timer);
      if (connecting === bounded) { connecting = null; connectingAccount = null; connectionAbort = null; }
    }
  }
  function scheduleStable(bound) {
    bound.stableTimer = timers.setTimeout(() => {
      bound.stableTimer = null;
      if (active !== bound || bound.channel.closed || bound.rotationPending) return;
      record('circuit-reset');
      void recovery.stable().catch(() => undefined);
    }, CONNECTION_STABLE_MS);
  }
  // Prove the pinned Agent identity to Brain on a newly opened channel.
  async function authenticateChannel(channel, pairingStore, accountId, agentInstallationId, controls = {}, check = async () => {}) {
    const challenge = await channel.rpc('agent.challenge', {}, controls);
    if (!exact(challenge, ['challenge_id', 'challenge', 'session_id', 'expires_at'])
      || typeof challenge.challenge_id !== 'string' || challenge.challenge_id.length > 128
      || typeof challenge.expires_at !== 'string' || challenge.expires_at.length > 40
      || !Number.isFinite(Date.parse(challenge.expires_at))) throw failure();
    const signature = await signAgentSessionProof(await pairingStore.identity(), challenge, channel.identity, agentInstallationId);
    await check();
    const authorized = await channel.rpc('agent.authenticate', { challenge_id: challenge.challenge_id, signature }, controls);
    if (!exact(authorized, ['creator_account_id', 'auth_ticket', 'storage_bootstrap'])
      || authorized.creator_account_id !== accountId || !secret(authorized.auth_ticket)
      || !secret(authorized.storage_bootstrap)) throw failure();
    return authorized;
  }
  // Session controls and browser state reports (ADR 0045). Controls change
  // nothing here: the consent and companion controllers apply them.
  const seenControls = new Set();
  const advertised = new WeakSet();
  let lastSurface = null, lastSurfaceEncoded = null, reportedSurfaceChannel = null, surfaceMeaning = null;
  function attachControls(channel) {
    const lifetime = new AbortController();
    channel.onClose(() => lifetime.abort());
    channel.onControl?.(({ id, action, payload, end, waiting }) => {
      if (action === 'browser.task.waiting' || action === 'browser.task.waiting.end') {
        if (!lifetime.signal.aborted && onBrowserTaskWaiting && channel === controlChannel() && (allowsFull?.() || allowsControl())) {
          void Promise.resolve().then(() => onBrowserTaskWaiting(action === 'browser.task.waiting' ? 'open' : 'end', waiting))
            .catch(() => undefined);
        }
        return;
      }
      if (action === 'browser.task.end') {
        // A notification only: the receiver matches it to an active task or ignores it.
        if (!lifetime.signal.aborted && onBrowserTaskEnd) void Promise.resolve().then(() => onBrowserTaskEnd(end)).catch(() => undefined);
        return;
      }
      if (action === 'browser.task') {
        const guard = () => {
          if (lifetime.signal.aborted || channel !== controlChannel() || !(allowsFull?.() || allowsControl())
            || payload?.creator_account_id !== channel.identity.creator_account_id) throw failure();
        };
        void Promise.resolve().then(() => {
          guard();
          if (!onBrowserTask) throw failure();
          return onBrowserTask(payload, { guard, signal: lifetime.signal, creatorAccountId: channel.identity.creator_account_id,
            notify: async status => {
              guard();
              await channel.rpc('agent.browser-task.report', { task_id: payload.task.task_id,
                admission_generation: payload.task.admission_generation, ...status });
            } });
        }).catch(() => undefined);
        return;
      }
      if (action === 'capture.control') {
        const guard = () => {
          if (lifetime.signal.aborted || channel !== controlChannel() || !(allowsFull?.() || allowsControl())) throw failure();
        };
        void Promise.resolve().then(() => {
          guard();
          return onCaptureControl?.(payload, channel.identity, guard);
        }).catch(() => undefined).finally(() => notifySurfaces());
        return;
      }
      if (seenControls.has(id)) return;
      seenControls.add(id);
      if (seenControls.size > 256) seenControls.delete(seenControls.values().next().value);
      if (action === 'companion.revoked') { void confirmRevocation(channel); return; }
      void Promise.resolve().then(() => onControl(action)).catch(() => undefined);
    });
  }
  // Brain announces a revocation just before committing it. Forget the pin
  // only if Brain then closes this session; a failed revocation keeps both sides paired.
  async function confirmRevocation(channel) {
    const closed = channel.closed || await new Promise((resolve) => {
      const timer = timers.setTimeout(() => { stop(); resolve(false); }, 3_000);
      const stop = channel.onClose(() => { timers.clearTimeout(timer); stop(); resolve(true); });
    });
    if (!closed) return;
    await forget(); await revocationListener();
  }
  let revocationListener = async () => {};
  function controlChannel() {
    if (active && !active.channel.closed) return active.channel;
    if (control && !control.channel.closed) return control.channel;
    return null;
  }
  function controlReady() {
    const channel = controlChannel();
    return channel !== null && lastSurface !== null && reportedSurfaceChannel === channel
      && (!readCaptureControlStatus || unsupportedCaptureStatus.has(channel)
        || sentCaptureStatus.get(channel) !== 'null');
  }
  // Presentation travels separately from the identifier-free status report.
  // Old local runtimes can reject the optional method without losing controls.
  let intentPublishing = false, nextIntentChannel = null;
  const sentIntents = new WeakMap();
  const unsupportedIntent = new WeakSet();
  function sendIntent(channel) {
    if ((!readFullPresentationIntent && !readCaptureControlStatus) || !channel) return;
    nextIntentChannel = channel;
    if (intentPublishing) return;
    intentPublishing = true;
    void (async () => {
      while (nextIntentChannel !== null) {
        const next = nextIntentChannel;
        nextIntentChannel = null;
        await publishCaptureStatus(next);
        await publishIntent(next);
      }
    })().catch(() => undefined).finally(() => {
      intentPublishing = false;
      if (nextIntentChannel !== null) sendIntent(nextIntentChannel);
    });
  }
  const sentCaptureStatus = new WeakMap();
  const unsupportedCaptureStatus = new WeakSet();
  async function publishCaptureStatus(channel) {
    if (!readCaptureControlStatus || channel !== controlChannel() || reportedSurfaceChannel !== channel
      || unsupportedCaptureStatus.has(channel)) return;
    const surface = lastSurface;
    const status = await captureStatusFor(channel, surface);
    if (channel !== controlChannel() || reportedSurfaceChannel !== channel || surface !== lastSurface) return;
    const encoded = JSON.stringify(status);
    if (sentCaptureStatus.get(channel) === encoded) return;
    try {
      await channel.rpc('agent.capture-control.status', { status });
      const wasReady = controlReady();
      sentCaptureStatus.set(channel, encoded);
      if (wasReady !== controlReady()) notifySurfaces();
    } catch (error) {
      if (error?.code === 'unknown_method') unsupportedCaptureStatus.add(channel);
    }
  }
  async function captureStatusFor(channel, surface) {
    let status;
    try { status = await readCaptureControlStatus(channel.identity); } catch { status = null; }
    return status?.capture === surface?.capture ? status : null;
  }
  async function publishIntent(channel) {
      if (!readFullPresentationIntent || channel !== controlChannel() || reportedSurfaceChannel !== channel || unsupportedIntent.has(channel)) return;
      let intent = null;
      try {
        if (!lastSurface?.legal_review_required)
          intent = await readFullPresentationIntent(channel.identity.creator_account_id);
      } catch { /* An unavailable scope is not intent. */ }
      if (channel !== controlChannel() || reportedSurfaceChannel !== channel) return;
      // A changed legal report supersedes any choice read before it.
      if (lastSurface?.legal_review_required) intent = null;
      const encoded = JSON.stringify(intent);
      if ((sentIntents.get(channel) ?? 'null') === encoded) return;
      try {
        await channel.rpc('agent.presentation.intent', { full_intent: intent });
        sentIntents.set(channel, encoded);
      } catch (error) {
        if (error?.code === 'unknown_method') unsupportedIntent.add(channel);
      }
  }
  async function sendSurface() {
    const channel = controlChannel();
    if (channel === null || lastSurface === null) return false;
    const surface = lastSurface, encoded = lastSurfaceEncoded, meaning = surfaceMeaning;
    try {
      if (readCaptureControlStatus && !unsupportedCaptureStatus.has(channel)) {
        const status = await captureStatusFor(channel, surface);
        if (channel !== controlChannel() || encoded !== lastSurfaceEncoded || meaning !== surfaceMeaning) return false;
        try {
          await channel.rpc('agent.capture-control.status', { surface, status });
          sentCaptureStatus.set(channel, JSON.stringify(status));
        } catch (error) {
          if (error?.code !== 'unknown_method') throw error;
          unsupportedCaptureStatus.add(channel);
          await channel.rpc('agent.surface.report', surface);
        }
      } else {
        await channel.rpc('agent.surface.report', surface);
      }
      if (onBrowserTaskEnd && channel.advertiseBrowserTasks && !advertised.has(channel)) {
        advertised.add(channel);
        void Promise.resolve(channel.advertiseBrowserTasks([])).then(ended => {
          if (ended === true && onBrowserTaskWaiting && channel.advertiseBrowserTaskWaiting) return channel.advertiseBrowserTaskWaiting();
          return false;
        }).catch(() => undefined);
      }
      if (channel === controlChannel() && encoded === lastSurfaceEncoded && meaning === surfaceMeaning) {
        const wasReady = controlReady();
        reportedSurfaceChannel = channel;
        if (!wasReady) notifySurfaces();
        sendIntent(channel);
      }
      return true;
    } catch {
      if (channel === controlChannel() && encoded === lastSurfaceEncoded && meaning === surfaceMeaning
        && reportedSurfaceChannel === channel) {
        reportedSurfaceChannel = null;
        notifySurfaces();
      }
      return false;
    }
  }
  function reportSurface(surface) {
    const encoded = JSON.stringify(surface);
    const changed = encoded !== lastSurfaceEncoded;
    const wasReady = controlReady();
    lastSurface = surface; lastSurfaceEncoded = encoded;
    const channel = controlChannel();
    if (changed) {
      surfaceMeaning = {};
      reportedSurfaceChannel = null;
      if (wasReady) notifySurfaces();
    }
    if (!changed && channel !== null && reportedSurfaceChannel === channel) { sendIntent(channel); return; }
    void sendSurface();
  }
  let control = null, controlOpening = null, controlRetryTimer = null;
  function closeControl() {
    if (controlRetryTimer !== null) { timers.clearTimeout(controlRetryTimer); controlRetryTimer = null; }
    const wasReady = controlReady();
    const current = control; control = null;
    current?.controller.abort(); current?.channel.close();
    if (wasReady && !controlReady()) notifySurfaces();
  }
  function scheduleControl(delay) {
    if (controlRetryTimer !== null) return;
    controlRetryTimer = timers.setTimeout(() => { controlRetryTimer = null; void ensureControl(); }, Math.max(1_000, delay));
  }
  // While paused from Full, hold a control-only session: it authenticates the
  // pinned identity, reports browser state, and accepts controls. It unlocks no
  // storage, starts no runtime, and carries no activity.
  function ensureControl() {
    if (!allowsControl() || allowsFull?.()) { closeControl(); return Promise.resolve(); }
    if (control || controlOpening || controlRetryTimer !== null) return controlOpening ?? Promise.resolve();
    const version = generation;
    const controller = new AbortController();
    const opening = (async () => {
      const pairingStore = await store();
      const saved = await pairingStore.status();
      const accountId = saved.identity?.creator_account_id;
      if (!saved.paired || typeof accountId !== 'string' || !accountId) return;
      await recovery.reserve();
      const current = () => { if (generation !== version || !allowsControl() || allowsFull?.()) throw failure(); };
      current();
      const snow = await loadSnow();
      const channel = await channelFactory({ url: LOCAL_SERVICE_WS, store: pairingStore, SnowSession: snow.SnowSession,
        accountId, trust: await trust(), signal: controller.signal });
      try {
        current();
        if (channel.identity.creator_account_id !== accountId) throw failure();
        await authenticateChannel(channel, pairingStore, accountId, await installationId(), { signal: controller.signal }, async () => current());
        current();
      } catch { channel.close(); throw failure(); }
      const opened = { channel, controller, openedAt: now() };
      control = opened;
      record('control-admitted');
      attachControls(channel);
      channel.onClose(() => {
        if (control !== opened) return;
        control = null;
        record('control-close', { code: channel.closeCode ?? null, reason: channel.closeReason ?? null });
        if (now() - opened.openedAt >= CONNECTION_STABLE_MS) void recovery.stable().catch(() => undefined);
        notifySurfaces();
        if (allowsControl() && !allowsFull?.()) void recovery.retryAfterMs().then(scheduleControl, () => scheduleControl(60_000));
      });
      notifySurfaces();
      await sendSurface();
    })().catch(async (error) => {
      controller.abort();
      if (!allowsControl() || allowsFull?.() || generation !== version) return;
      scheduleControl(error?.retryAfterMs ?? await recovery.retryAfterMs().catch(() => 60_000));
    }).finally(() => { if (controlOpening === opening) controlOpening = null; });
    controlOpening = opening;
    return opening;
  }
  async function hasSavedPairing() {
    if (!allowsFull?.()) return false;
    return (await (await store()).status()).paired === true;
  }
  async function status() {
    if (!allowsFull?.()) return { state: 'unavailable', comparison_code: null };
    if (await hasSavedPairing()) return { state: 'paired', comparison_code: null };
    if (['pairing', 'compare', 'pairing_failed', 'desktop_not_ready'].includes(state.state)) return { ...state };
    let account = null;
    try { account = await detectedAccountId(); } catch {}
    if (typeof account !== 'string' || account.length === 0) {
      return { state: 'setup_incomplete', comparison_code: null };
    }
    return { ...state };
  }
  async function analysisReadiness(controls = {}) {
    const epoch = readinessConsentEpoch();
    const connected = await connect(controls);
    const assertCurrent = () => {
      controls.signal?.throwIfAborted(); controls.assertCurrent?.();
      if (!allowsFull() || readinessConsentEpoch() !== epoch || active !== connected || connected.channel.closed) {
        throw new ReadinessSuperseded();
      }
    };
    assertCurrent();
    const reply = await connected.channel.rpc('agent.analysis.readiness', {}, { ...controls, assertCurrent })
      .then(value => ({ value }), error => ({ error }));
    assertCurrent();
    await permitted(connected.accountId).catch(() => { throw new ReadinessSuperseded(); });
    assertCurrent();
    if (Object.hasOwn(reply, 'error')) throw reply.error;
    return validatedAnalysisReadiness(reply.value);
  }
  async function pair({ signal } = {}) {
    if (pairingAbort !== null) throw failure();
    const controller = new AbortController();
    pairingAbort = controller;
    const combined = signal ? AbortSignal.any([controller.signal, signal]) : controller.signal;
    let expired = false;
    const timer = setTimeout(() => { expired = true; controller.abort(); }, 300_000);
    const wait = (operation) => abortable(operation, combined);
    const version = generation;
    let wire, pending, committed = false, offered = false;
    const check = async (account) => {
      combined.throwIfAborted();
      if (generation !== version) throw failure();
      await permitted(account);
    };
    announce({ state: 'pairing', comparison_code: null });
    try {
      const account = await wait(permitted());
      const pairingStore = await wait(store());
      const snow = await wait(loadSnow());
      await check(account);
      pending = await wait(pairingStore.begin({ agentInstallationId: await wait(installationId()),
        generateNoiseKeypair: snowKeypairGenerator(snow.generateStaticKeypair),
        deadline: Math.floor(Date.now() / 1000) + 300, signal: combined }));
      wire = await wait(wireFactory(LOCAL_PAIRING_WS, { text: true, signal: combined }));
      await wire.send(JSON.stringify(pending.request));
      const offer = await wire.receive();
      offered = true;
      const accepted = await wait(pairingStore.acceptOffer(pending.requestId, offer, {
        trust: await wait(trust()), detectedAccountId: account, signal: combined,
      }));
      await check(account);
      await wire.send(JSON.stringify(accepted.confirm));
      announce({ state: 'compare', comparison_code: accepted.comparisonCode });
      const remaining = Math.max(0, pending.deadline * 1000 - Date.now());
      const result = parseMessage(await wire.receive(remaining), 'pair.result');
      if (result.pairing_id !== accepted.confirm.pairing_id || result.outcome !== 'confirmed') throw failure();
      await check(account);
      await connect({ signal: combined }, pending.requestId);
      committed = true;
      announce({ state: 'paired', comparison_code: null });
      return { ...state };
    } catch {
      // A state refusal before any offer means Brain has no open pairing window.
      const notReady = !offered && !combined.aborted && wire?.closeReason === 'pairing_state_refused';
      const cancelled = combined.aborted && !expired;
      announce({ state: notReady ? 'desktop_not_ready' : cancelled ? 'unpaired' : 'pairing_failed', comparison_code: null });
      throw failure();
    } finally {
      clearTimeout(timer);
      wire?.close();
      if (!committed && pending) await (await store()).cancel();
      if (pairingAbort === controller) pairingAbort = null;
    }
  }
  async function forget() {
    invalidate();
    await (await store()).forget();
    announce({ state: 'unpaired', comparison_code: null });
  }
  function webSocketFactory() {
    const facade = { readyState: 0, onopen: null, onmessage: null, onclose: null,
      onerror: null, authTicket: null, retryAfterMs: 0 };
    const controller = new AbortController();
    let channel = null, openingAbort = null, stopped = false, unsubscribe;
    facade.close = (code = null, reason = null) => {
      if (stopped) return;
      record('facade-close', { code: Number.isInteger(code) && code >= 1000 && code <= 4999 ? code : null,
        reason: safeCompanionCloseReason(reason) });
      stopped = true; facade.readyState = 3;
      controller.abort();
      unsubscribe?.(); channel?.close();
      // A pending facade owns only the handshake it started. Another caller
      // may have established or begun the current shared connection meanwhile.
      openingAbort?.abort(); openingAbort = null;
      void recovery.retryAfterMs().then((delay) => { facade.retryAfterMs = delay; })
        .catch(() => { facade.retryAfterMs = 60_000; })
        .finally(() => facade.onclose?.({ code: 4008 }));
    };
    facade.send = (text) => {
      if (stopped || facade.readyState !== 1 || channel === null) throw failure();
      let message; try { message = JSON.parse(text); } catch { facade.close(); throw failure(); }
      void channel.send(message).catch(() => facade.close());
    };
    void connect({ signal: controller.signal }, undefined, owner => { openingAbort = owner; }).then((connected) => {
      if (stopped) return;
      channel = connected.channel;
      openingAbort = null;
      facade.authTicket = connected.authTicket;
      unsubscribe = channel.onMessage((message) => { if (!stopped) facade.onmessage?.({ data: JSON.stringify(message) }); });
      channel.onClose(() => facade.close());
      facade.readyState = 1;
      facade.onopen?.();
    }).catch(() => facade.close());
    return facade;
  }
  webSocketFactory.retryAfterMs = () => active !== null && !active.channel.closed
    ? Promise.resolve(0) : recovery.retryAfterMs();
  const adapter = Object.freeze({
    invalidate,
    loadAgentInstallationId: installationId,
    async loadAgentIdentity() { return { agentInstallationId: await installationId() }; },
    async loadBrainBinding(controls = {}) {
      const bound = await connect(controls);
      controls.signal?.throwIfAborted(); controls.assertCurrent?.();
      const installationId = bound.channel.identity.installation_id;
      await chromeApi.storage.session.set({ [ACTIVE_PARTITION_KEY]: await accountDatabaseName(bound.accountId, installationId) });
      controls.signal?.throwIfAborted(); controls.assertCurrent?.();
      return { creatorAccountId: bound.accountId, installationId, authTicket: bound.authTicket, storageKey: bound.storageKey };
    },
    async loadReconnectAuthTicket() { return null; },
    async saveReconnectAuthTicket(credential, controls = {}) {
      const bound = active;
      credentialRotation.attempts++;
      let phase = 'binding', cause = 'binding_unavailable';
      try {
        if (!bound) throw failure();
        cause = 'channel_closed';
        if (bound.channel.closed) throw failure();
        cause = 'account_mismatch';
        if (bound.accountId !== credential.creatorAccountId) throw failure();
        bound.rotationPending = true;
        if (bound.stableTimer !== null) timers.clearTimeout(bound.stableTimer);
        bound.stableTimer = null;
        phase = 'rpc'; cause = null;
        const params = {
          protocol_version: '2', creator_account_id: credential.creatorAccountId,
          agent_installation_id: credential.agentInstallationId, reconnect_auth_ticket: credential.authTicket,
          config_auth_ticket: credential.configAuthTicket, storage_bootstrap: bound.storageBootstrap,
        };
        const deadline = performance.now() + 10_000;
        let rotated;
        for (;;) {
          controls.signal?.throwIfAborted(); controls.assertCurrent?.();
          if (active !== bound || bound.channel.closed) throw failure();
          try {
            rotated = await bound.channel.rpc('agent.storage.rotate', params, controls);
            break;
          } catch (error) {
            // Local capacity refusal happens before the request is sent.
            if (error?.code !== 'companion_session_refused' || error?.diagnostic?.cause !== 'rpc_capacity'
              || performance.now() >= deadline) throw error;
            await new Promise(resolve => timers.setTimeout(resolve, 25));
            if (performance.now() >= deadline) throw error;
          }
        }
        phase = 'response'; cause = 'rotation_response_invalid';
        if (!exact(rotated, ['schema', 'storage_bootstrap'])
          || rotated.schema !== 'ofca-extension-storage-rotation/v1' || !secret(rotated.storage_bootstrap)) throw failure();
        phase = 'commit'; cause = 'binding_changed';
        if (active !== bound) throw failure();
        bound.storageBootstrap = rotated.storage_bootstrap;
        bound.rotationPending = false;
        bound.openedAt = now();
        scheduleStable(bound);
        credentialRotation.completed++;
      } catch (error) {
        credentialRotation.failure = {
          phase,
          cause: cause ?? (['session_request_refused', 'companion_session_refused', 'companion_recovery_backoff',
            'unknown_method'].includes(error?.code) ? error.code : 'other'),
          errorName: ['Error', 'TypeError', 'CompanionChannelError', 'AbortError', 'TimeoutError',
            'InvalidStateError', 'QuotaExceededError', 'TransactionInactiveError'].includes(error?.name) ? error.name : 'other',
          signalAborted: controls.signal?.aborted === true,
          channelClosed: bound?.channel.closed ?? null,
          bindingCurrent: bound !== null && active === bound,
          channel: safeCompanionChannelDiagnostic(error?.diagnostic ?? bound?.channel.closeDiagnostic),
        };
        record('credential-rotation-failed');
        throw error;
      }
    },
    async clearBrainBinding() {
      await forget();
      await chromeApi.storage.session.remove([ACTIVE_PARTITION_KEY]);
      (await store()).close(); storePromise = null; installationPromise = null;
    },
    onWake(listener) {
      const events = [chromeApi.runtime.onStartup, chromeApi.runtime.onInstalled, chromeApi.runtime.onMessage,
        chromeApi.tabs?.onUpdated].filter(Boolean);
      for (const event of events) event.addListener(listener);
      const alarm = (value) => { if ([CONNECTION_RECONCILE_ALARM, PARAMETER_ALARM].includes(value?.name)) listener(); };
      chromeApi.alarms?.onAlarm?.addListener(alarm);
      void chromeApi.alarms?.create(CONNECTION_RECONCILE_ALARM, { delayInMinutes: 1, periodInMinutes: 1 });
      return () => { for (const event of events) event.removeListener?.(listener); chromeApi.alarms?.onAlarm?.removeListener?.(alarm); };
    },
  });
  const configAdapter = {
    async resolveSigningParameters(request, { signal, context }) {
      validateParameterRequest(request);
      const bound = active;
      const guard = () => {
        signal?.throwIfAborted(); context.signal?.throwIfAborted(); context.assertCurrent();
        if (!allowsFull() || !bound || active !== bound || bound.channel.closed
          || bound.accountId !== context.creatorAccountId || bound.channel.identity.installation_id !== context.installationId) throw failure();
      };
      guard();
      try {
        const result = await bound.channel.rpc(SIGNING_PARAMETER_METHOD, {
          operation: SIGNING_PARAMETER_METHOD, protocol_version: '2', auth_ticket: context.configAuthTicket(),
          creator_account_id: context.creatorAccountId, agent_installation_id: context.agentInstallationId, request,
        }, { signal });
        guard();
        return result;
      } catch (error) {
        guard();
        if (error?.code === 'unknown_method') return { schema: 'signing-parameter-resolution/v1',
          status: 'unavailable', reason: 'verification', keyset: null, catalog: null, retry_after_ms: 300_000 };
        throw error;
      }
    },
    async catchupRpc(operation, payload, controls = {}) {
      if (!active || active.channel.closed || active.accountId !== payload.creator_account_id) throw failure();
      return active.channel.rpc(operation, payload, controls);
    },
    async fetchConfig(context) {
      if (!active || active.channel.closed || active.accountId !== context.creatorAccountId) throw failure();
      return active.channel.rpc('agent.config.get', {
        operation: 'agent.config.get', protocol_version: '2', auth_ticket: context.authTicket,
        agent_installation_id: context.agentInstallationId, creator_account_id: context.creatorAccountId,
        current_etag: context.currentEtag, current_config_revision: context.currentConfigRevision,
        supported_config_schema_versions: context.supportedSchemaVersions,
      }, context);
    },
  };
  // One attempt at a time. Its owner is a setup page port or a desktop port;
  // closing the owner cancels the attempt it started.
  async function savedPairing() { return (await store()).savedPairing(); }
  async function pairFor(owner, { signal, onPaired = async () => {}, replacement = null,
    authorize = async () => {}, assertCurrent = () => {} } = {}) {
    if (pairingOwner !== null) return false;
    pairingOwner = owner;
    try {
      const existing = await status();
      if (replacement !== null) {
        const account = await permitted();
        const version = generation;
        await authorize();
        signal?.throwIfAborted();
        await permitted(account);
        if (version !== generation || pairingOwner !== owner) throw failure();
        const pairingStore = await store();
        await pairingStore.forget({ expectedToken: replacement, signal, assertCurrent() {
          assertCurrent();
          if (version !== generation || pairingOwner !== owner || !allowsFull?.()) throw failure();
        } });
        invalidate();
        announce({ state: 'unpaired', comparison_code: null });
        await authorize();
        signal?.throwIfAborted();
      } else if (existing.state === 'paired') {
        if (owner?.desktop) throw failure();
        return true;
      }
      await pair({ signal }); await onPaired();
      return true;
    } finally {
      if (pairingOwner === owner) pairingOwner = null;
    }
  }
  function cancelFor(owner) { if (pairingOwner !== null && pairingOwner === owner) pairingAbort?.abort(); }
  function owns(owner) { return pairingOwner !== null && pairingOwner === owner; }
  function subscribe(notify) { subscribers.add(notify); return () => { subscribers.delete(notify); }; }
  // Tell every open surface to re-read its state. It carries no state itself.
  function notifySurfaces() { for (const notify of subscribers) notify({ type: 'surface_changed' }); }
  function registerPopup({ onPaired = async () => {}, onForget = async () => {} } = {}) {
    chromeApi.runtime.onConnect.addListener((port) => {
      const surface = uiSurface(port.sender, chromeApi);
      if (port.name !== PAIRING_PORT_NAME || surface === null) return;
      const controller = new AbortController();
      const notify = (value) => {
        if (controller.signal.aborted) return;
        const projection = value?.state ? {
          ...value,
          comparison_code: surface === 'setup' ? value.comparison_code : null,
          owns_attempt: surface === 'setup' && owns(port),
          desktop_attempt: pairingOwner?.desktop === true,
          desktop_control: controlReady(),
        } : value;
        try { port.postMessage(projection); } catch {}
      };
      let readinessRead = null;
      async function readReadiness(requestId) {
        readinessRead?.abort();
        const reading = new AbortController();
        readinessRead = reading;
        const signal = AbortSignal.any([controller.signal, reading.signal]);
        const epoch = readinessConsentEpoch();
        const current = () => !signal.aborted && readinessRead === reading
          && readinessConsentEpoch() === epoch && allowsFull();
        const respond = value => {
          if (current()) notify({ type: READINESS_RESPONSE, request_id: requestId, ...value });
        };
        try { respond(await analysisReadiness({ signal })); }
        catch (error) {
          if (!(error instanceof ReadinessSuperseded)) {
            respond({ commercial_authority: 'unavailable', analysis_admission: 'blocked' });
          }
        } finally {
          if (readinessRead === reading) readinessRead = null;
        }
      }
      subscribers.add(notify);
      port.onDisconnect.addListener(() => { subscribers.delete(notify); controller.abort(); });
      port.onMessage.addListener((message) => {
        if (isReadinessRequest(message)) { void readReadiness(message.request_id); return; }
        if (!message || Object.keys(message).length !== 1
          || !['pair', 'status', 'forget', 'cancel'].includes(message.type)) return;
        void (async () => {
          if (message.type === 'pair') {
            if (surface !== 'setup') return;
            if (!await pairFor(port, { signal: controller.signal, onPaired })) { notify(await status()); return; }
          }
          else if (message.type === 'forget') {
            if (surface !== 'options') return;
            await forget(); await onForget();
            notify({ type: 'pairing_command_result', command: 'forget', ok: true });
          }
          else if (message.type === 'cancel') {
            if (surface === 'setup') cancelFor(port);
          }
          notify(await status());
        })().catch(() => {
          if (message.type === 'forget') {
            notify({ type: 'pairing_command_result', command: 'forget', ok: false });
          } else if (message.type === 'pair') {
            void status().then(notify, () => notify({ state: 'pairing_failed', comparison_code: null }));
          } else {
            notify({ state: 'pairing_failed', comparison_code: null });
          }
        });
      });
      void status().then(notify).catch(() => notify({ state: 'unavailable', comparison_code: null }));
    });
  }
  return Object.freeze({ adapter, configAdapter, webSocketFactory, invalidate, pair, forget, hasSavedPairing, status, analysisReadiness, registerPopup,
    pairFor, savedPairing, cancelFor, owns, subscribe, notifySurfaces, reportSurface, ensureControl, controlReady,
    onRevoked(listener) { revocationListener = listener; },
    get diagnosticEvents() { return diagnosticEvents.map((entry) => ({ ...entry })); },
    get credentialRotation() { return structuredClone(credentialRotation); },
    get connected() { return active !== null && !active.channel.closed; } });
}
