import { openPairingStore } from './companion-pairing-store.mjs';
import { loadPackagedSnow } from './packaged-snow.mjs';
import { signAgentSessionProof, snowKeypairGenerator } from './companion-agent-identity.mjs';
import { openCompanionChannel, openLoopbackSocket, CompanionChannelError } from '../transport/companion-channel.mjs';
import { parseMessage } from '../transport/pairing-contract.mjs';
import { loadGrantTrustSet } from '../transport/grant-verifier.mjs';
import { LOCAL_SERVICE_WS, LOCAL_PAIRING_WS } from '../transport/local-service-endpoints.mjs';

export const PAIRING_PORT_NAME = 'ofca.companion.pairing';
const INSTALLATION_KEY = 'agent_installation_id';
const ACTIVE_PARTITION_KEY = 'active_account_partition_v5';
const RECONCILE_ALARM = 'ofca-agent-reconcile';
const ANALYSIS_READINESS_SCHEMA = 'ofca-analysis-readiness/v1';
const failure = () => new CompanionChannelError();
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
    || !['required', 'active', 'unavailable'].includes(value.commercial_authority)
    || !['blocked', 'admitted'].includes(value.analysis_admission)
    || (value.analysis_admission === 'admitted' && value.commercial_authority !== 'active')) throw failure();
  return Object.freeze({
    commercial_authority: value.commercial_authority,
    analysis_admission: value.analysis_admission,
  });
}

export function createCompanionClient({
  chromeApi = globalThis.chrome, allowsFull, detectedAccountId,
  accountDatabaseName,
  storeFactory = openPairingStore, loadSnow = loadPackagedSnow,
  channelFactory = openCompanionChannel, wireFactory = openLoopbackSocket,
  loadTrust = async () => loadGrantTrustSet(await (await fetch(chromeApi.runtime.getURL('companion-grant-trust.json'))).json()),
} = {}) {
  let storePromise, trustPromise, installationPromise, connecting = null, connectingAccount = null, connectionAbort = null, active = null;
  let generation = 0, pairingAbort = null, state = { state: 'unpaired', comparison_code: null };
  const subscribers = new Set();
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
    generation++;
    pairingAbort?.abort();
    connectionAbort?.abort();
    active?.channel.close(); active = null;
    connecting = null;
  }
  async function permitted(expected = null) {
    if (!allowsFull?.()) throw failure();
    const account = await detectedAccountId();
    if (typeof account !== 'string' || !account || (expected !== null && account !== expected)) throw failure();
    return account;
  }
  async function connect(controls = {}, requestId) {
    const controller = new AbortController();
    const signal = controls.signal ? AbortSignal.any([controller.signal, controls.signal]) : controller.signal;
    const timer = setTimeout(() => controller.abort(), 10_000);
    try { return await abortable(connectCurrent({ ...controls, signal, connectionAbort: controller }, requestId), signal); }
    finally { clearTimeout(timer); }
  }
  async function connectCurrent(controls, requestId) {
    controls.signal?.throwIfAborted(); controls.assertCurrent?.();
    const accountId = await permitted();
    controls.signal.throwIfAborted(); controls.assertCurrent?.();
    if (active && active.accountId !== accountId) invalidate();
    if (active && !active.channel.closed && active.accountId === accountId) return active;
    if (connecting) {
      if (connectingAccount !== accountId) { invalidate(); throw failure(); }
      const connected = await connecting;
      controls.signal.throwIfAborted(); controls.assertCurrent?.();
      await permitted(accountId);
      return connected;
    }
    const version = generation;
    connectionAbort = controls.connectionAbort;
    connectingAccount = accountId;
    const current = () => {
      controls.signal?.throwIfAborted(); controls.assertCurrent?.();
      if (generation !== version || !allowsFull?.()) throw failure();
    };
    const operation = (async () => {
      const pairingStore = await store();
      const snow = await loadSnow();
      current(); await permitted(accountId);
      const channel = await channelFactory({
        url: LOCAL_SERVICE_WS, store: pairingStore, SnowSession: snow.SnowSession,
        accountId, requestId, trust: await trust(), signal: controls.signal,
      });
      try {
        current(); await permitted(accountId);
        if (channel.identity.creator_account_id !== accountId) throw failure();
        const agentInstallationId = await installationId();
        const challenge = await channel.rpc('agent.challenge', {}, controls);
        if (!exact(challenge, ['challenge_id', 'challenge', 'session_id', 'expires_at'])
          || typeof challenge.challenge_id !== 'string' || challenge.challenge_id.length > 128
          || typeof challenge.expires_at !== 'string' || challenge.expires_at.length > 40
          || !Number.isFinite(Date.parse(challenge.expires_at))) throw failure();
        const signature = await signAgentSessionProof(await pairingStore.identity(), challenge, channel.identity, agentInstallationId);
        current(); await permitted(accountId);
        const authorized = await channel.rpc('agent.authenticate', { challenge_id: challenge.challenge_id, signature }, controls);
        if (!exact(authorized, ['creator_account_id', 'auth_ticket', 'storage_bootstrap'])
          || authorized.creator_account_id !== accountId || !secret(authorized.auth_ticket)
          || !secret(authorized.storage_bootstrap)) throw failure();
        const unlocked = await channel.rpc('agent.storage.unseal', { storage_bootstrap: authorized.storage_bootstrap }, controls);
        if (!exact(unlocked, ['schema', 'creator_account_id', 'credential_kind', 'auth_ticket', 'storage_key_base64'])
          || unlocked.schema !== 'ofca-extension-storage-unlock/v1' || unlocked.creator_account_id !== accountId
          || !['pairing', 'reconnect'].includes(unlocked.credential_kind)
          || unlocked.auth_ticket !== authorized.auth_ticket || typeof unlocked.storage_key_base64 !== 'string'
          || !/^[A-Za-z0-9+/]{43}=$/u.test(unlocked.storage_key_base64)
          || atob(unlocked.storage_key_base64).length !== 32
          || btoa(atob(unlocked.storage_key_base64)) !== unlocked.storage_key_base64) throw failure();
        current(); await permitted(accountId);
        active = { channel, accountId, authTicket: authorized.auth_ticket, storageKey: unlocked.storage_key_base64,
          storageBootstrap: authorized.storage_bootstrap, agentInstallationId };
        channel.onClose(() => { if (active?.channel === channel) active = null; });
        return active;
      } catch { channel.close(); throw failure(); }
    })();
    connecting = operation;
    try { return await operation; } finally {
      if (connecting === operation) { connecting = null; connectingAccount = null; connectionAbort = null; }
    }
  }
  async function status() {
    if (!allowsFull?.()) return { state: 'unavailable', comparison_code: null };
    const saved = await (await store()).status();
    if (saved.paired) return { state: 'paired', comparison_code: null };
    if (['pairing', 'compare', 'pairing_failed'].includes(state.state)) return { ...state };
    let account = null;
    try { account = await detectedAccountId(); } catch {}
    if (typeof account !== 'string' || account.length === 0) {
      return { state: 'setup_incomplete', comparison_code: null };
    }
    return { ...state };
  }
  async function analysisReadiness(controls = {}) {
    const connected = await connect(controls);
    controls.signal?.throwIfAborted(); controls.assertCurrent?.();
    return validatedAnalysisReadiness(await connected.channel.rpc('agent.analysis.readiness', {}, controls));
  }
  async function pair({ signal } = {}) {
    if (pairingAbort !== null) throw failure();
    const controller = new AbortController();
    pairingAbort = controller;
    const combined = signal ? AbortSignal.any([controller.signal, signal]) : controller.signal;
    const timer = setTimeout(() => controller.abort(), 300_000);
    const wait = (operation) => abortable(operation, combined);
    const version = generation;
    let wire, pending, committed = false;
    const check = async (account) => {
      combined.throwIfAborted();
      if (generation !== version) throw failure();
      await permitted(account);
    };
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
      announce({ state: 'pairing', comparison_code: null });
      const accepted = await wait(pairingStore.acceptOffer(pending.requestId, await wire.receive(), {
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
      announce({ state: 'pairing_failed', comparison_code: null });
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
    const facade = { readyState: 0, onopen: null, onmessage: null, onclose: null, onerror: null, authTicket: null };
    const controller = new AbortController();
    let channel = null, stopped = false, unsubscribe;
    facade.close = () => {
      if (stopped) return;
      stopped = true; facade.readyState = 3;
      controller.abort();
      unsubscribe?.(); channel?.close();
      queueMicrotask(() => facade.onclose?.({ code: 4008 }));
    };
    facade.send = (text) => {
      if (stopped || facade.readyState !== 1 || channel === null) throw failure();
      let message; try { message = JSON.parse(text); } catch { facade.close(); throw failure(); }
      void channel.send(message).catch(() => facade.close());
    };
    void connect({ signal: controller.signal }).then((connected) => {
      if (stopped) return;
      channel = connected.channel;
      facade.authTicket = connected.authTicket;
      unsubscribe = channel.onMessage((message) => { if (!stopped) facade.onmessage?.({ data: JSON.stringify(message) }); });
      channel.onClose(() => facade.close());
      facade.readyState = 1;
      facade.onopen?.();
    }).catch(() => facade.close());
    return facade;
  }
  const adapter = Object.freeze({
    invalidate,
    loadAgentInstallationId: installationId,
    async loadAgentIdentity() { return { agentInstallationId: await installationId() }; },
    async loadBrainBinding(controls = {}) {
      const bound = await connect(controls);
      controls.signal?.throwIfAborted(); controls.assertCurrent?.();
      await chromeApi.storage.session.set({ [ACTIVE_PARTITION_KEY]: await accountDatabaseName(bound.accountId) });
      controls.signal?.throwIfAborted(); controls.assertCurrent?.();
      return { creatorAccountId: bound.accountId, authTicket: bound.authTicket, storageKey: bound.storageKey };
    },
    async loadReconnectAuthTicket() { return null; },
    async saveReconnectAuthTicket(credential, controls = {}) {
      const bound = active;
      if (!bound || bound.channel.closed || bound.accountId !== credential.creatorAccountId) throw failure();
      const rotated = await bound.channel.rpc('agent.storage.rotate', {
        protocol_version: '2', creator_account_id: credential.creatorAccountId,
        agent_installation_id: credential.agentInstallationId, reconnect_auth_ticket: credential.authTicket,
        config_auth_ticket: credential.configAuthTicket, storage_bootstrap: bound.storageBootstrap,
      }, controls);
      if (!exact(rotated, ['schema', 'storage_bootstrap'])
        || rotated.schema !== 'ofca-extension-storage-rotation/v1' || !secret(rotated.storage_bootstrap)) throw failure();
      if (active !== bound) throw failure();
      bound.storageBootstrap = rotated.storage_bootstrap;
    },
    async clearBrainBinding() {
      await forget();
      await chromeApi.storage.session.remove([ACTIVE_PARTITION_KEY]);
      (await store()).close(); storePromise = null; installationPromise = null;
    },
    onWake(listener) {
      const events = [chromeApi.runtime.onStartup, chromeApi.runtime.onInstalled, chromeApi.runtime.onMessage, chromeApi.tabs?.onUpdated].filter(Boolean);
      for (const event of events) event.addListener(listener);
      const alarm = (value) => { if (value?.name === RECONCILE_ALARM) listener(); };
      chromeApi.alarms?.onAlarm?.addListener(alarm);
      void chromeApi.alarms?.create(RECONCILE_ALARM, { delayInMinutes: 1, periodInMinutes: 1 });
      return () => { for (const event of events) event.removeListener?.(listener); chromeApi.alarms?.onAlarm?.removeListener?.(alarm); };
    },
  });
  const configAdapter = {
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
  function registerPopup({ onPaired = async () => {}, onForget = async () => {} } = {}) {
    chromeApi.runtime.onConnect.addListener((port) => {
      const pairingWindow = port.sender?.url === chromeApi.runtime.getURL('popup.html#pairing');
      if (port.name !== PAIRING_PORT_NAME || port.sender?.id !== chromeApi.runtime.id
        || (!pairingWindow && port.sender.url !== chromeApi.runtime.getURL('popup.html'))) return;
      const controller = new AbortController();
      const notify = (value) => { try { port.postMessage(value); } catch {} };
      const notifyReadiness = (value) => notify({ type: 'analysis_readiness', ...value });
      subscribers.add(notify);
      port.onDisconnect.addListener(() => { subscribers.delete(notify); controller.abort(); });
      port.onMessage.addListener((message) => {
        if (!message || Object.keys(message).length !== 1
          || !['pair', 'status', 'forget', 'cancel', 'readiness'].includes(message.type)) return;
        void (async () => {
          if (message.type === 'pair') {
            if (!pairingWindow) throw failure();
            const existing = await status();
            if (existing.state === 'paired') { notify(existing); return; }
            await pair({ signal: controller.signal }); await onPaired();
          }
          else if (message.type === 'forget') { await forget(); await onForget(); }
          else if (message.type === 'cancel') pairingAbort?.abort();
          else if (message.type === 'readiness') {
            notifyReadiness(await analysisReadiness({ signal: controller.signal }));
            return;
          }
          notify(await status());
        })().catch(() => {
          if (message.type === 'readiness') {
            notifyReadiness({ commercial_authority: 'unavailable', analysis_admission: 'blocked' });
          } else {
            notify({ state: 'pairing_failed', comparison_code: null });
          }
        });
      });
      void status().then(notify).catch(() => notify({ state: 'unavailable', comparison_code: null }));
    });
  }
  return Object.freeze({ adapter, configAdapter, webSocketFactory, invalidate, pair, forget, status, analysisReadiness, registerPopup,
    get connected() { return active !== null && !active.channel.closed; } });
}
