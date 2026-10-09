import { PREVIEW_METRICS_STORAGE_KEY } from './preview-metrics.mjs';
export const ONBOARDING_PORT = 'ofca.onboarding.v1';
const OPERATIONS_KEY = 'onboarding_operations_v1';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
function isCommand(value) {
  return exact(value, ['profile', 'journey_id', 'operation_id', 'owner', 'action', 'account_generation', 'consent_generation'])
    && value.profile === 'local-onboarding-command.v1' && value.owner === 'extension'
    && UUID.test(value.journey_id) && UUID.test(value.operation_id) && ['pause', 'resume', 'reopen_helper'].includes(value.action)
    && [value.account_generation, value.consent_generation].every((entry) => Number.isSafeInteger(entry) && entry >= 0);
}
export function registerOnboardingPort({ chromeApi, workspace, consentController, legalActivationController, identityBridge, companion, expectedCreator = () => null }) {
  const epoch = crypto.randomUUID(); let revision = 0, accountGeneration = 0, consentGeneration = 0;
  let consentEpoch = null, accountScope = null, lastFacts = null, pending = null;
  let queue = Promise.resolve(), scheduled = false;
  const ports = new Set();
  const serial = (work) => { const run = queue.then(work); queue = run.catch(() => undefined); return run; };
  async function evidence() {
    const [status, observed, saved] = await Promise.all([consentController.status(), identityBridge.currentAccountId(),
      chromeApi.storage.local.get([PREVIEW_METRICS_STORAGE_KEY])]);
    const token = saved[PREVIEW_METRICS_STORAGE_KEY]?.active_account;
    return { status, scope: { consent_epoch: status.consent.consent_epoch, mode: status.consent.mode,
      account_id: observed, expected_creator: expectedCreator(), preview_account: /^[a-f0-9]{64}$/u.test(token ?? '') ? token : null } };
  }
  async function project(kind = 'snapshot') {
    const record = await workspace.read(); if (!record) return null;
    const [{ status, scope }, legal] = await Promise.all([evidence(), legalActivationController.status()]);
    const observed = scope.account_id;
    if (consentEpoch !== status.consent.consent_epoch) { consentEpoch = status.consent.consent_epoch; consentGeneration++; }
    const account = JSON.stringify([scope.account_id, scope.expected_creator, scope.preview_account]);
    if (accountScope !== account) { accountScope = account; accountGeneration++; }
    const mode = status.consent.mode === 'paused' ? status.consent.resume_mode : status.consent.mode;
    const facts = { mode: ['preview', 'full'].includes(mode) ? mode : 'off',
      consent: legal.requires_reauthorization ? 'review_required' : ['preview', 'full', 'paused'].includes(status.consent.mode) ? 'valid' : 'missing',
      site_access: status.onlyfans_permission ? 'granted' : 'missing', account: 'unknown',
      attachment: status.observer?.attachment ?? 'checking',
      capture: status.consent.mode === 'paused' ? 'paused' : ['preview', 'full'].includes(status.phase) && status.observer?.attachment === 'ready' ? 'active' : 'off' };
    // An observed account without an intended account is not a match or mismatch.
    const bound = scope.expected_creator;
    if (bound && observed) facts.account = bound === observed ? 'matching' : 'mismatch';
    // An attached document is not evidence that Full capture is authorized for
    // its current account. Identity changes invalidate that claim immediately.
    if (mode === 'full' && facts.capture === 'active' && facts.account !== 'matching') facts.capture = 'off';
    const reason = facts.consent !== 'valid' ? 'consent_required' : facts.site_access === 'missing' ? 'permission_required'
      : status.consent.mode === 'paused' ? 'paused' : status.observer?.helper === 'closed' ? 'helper_closed'
        : facts.account === 'mismatch' ? 'wrong_account' : 'none';
    const committed = JSON.stringify({ facts, reason, pending, accountGeneration, consentGeneration });
    if (committed !== lastFacts) { revision++; lastFacts = committed; }
    return { profile: 'local-onboarding-state.v1', kind, journey_id: record.journey_id, source: 'extension', epoch, revision,
      account_generation: accountGeneration, consent_generation: consentGeneration, facts, pending_operation: pending, reason };
  }
  function send(entry, value) { if (!value || entry.closed) return; try { entry.port.postMessage(value); } catch { entry.closed = true; ports.delete(entry); } }
  async function publish() {
    scheduled = false; const state = await project('event'); if (!state) return;
    for (const entry of ports) {
      if (!entry.initialized || entry.lastRevision === state.revision) continue;
      try {
        const admitted = await workspace.admit(entry.port.sender);
        if (admitted.route === 'hosted' || admitted.record.journey_id !== state.journey_id) throw Error('local_only');
        send(entry, state); entry.lastRevision = state.revision;
      } catch { entry.closed = true; ports.delete(entry); entry.port.disconnect(); }
    }
  }
  function changed() {
    if (scheduled) return; scheduled = true;
    queueMicrotask(() => { void serial(publish).catch(() => { scheduled = false; }); });
  }
  async function commands() {
    const stored = (await chromeApi.storage.local.get([OPERATIONS_KEY]))[OPERATIONS_KEY];
    return Array.isArray(stored) ? stored.filter((value) => value.expires > Date.now()) : [];
  }
  const result = (value, commandEpoch, status, reason) => ({ profile: 'local-onboarding-result.v2', journey_id: value.journey_id,
    operation_id: value.operation_id, source: 'extension', epoch, revision, command_epoch: commandEpoch,
    command_account_generation: value.account_generation, command_consent_generation: value.consent_generation,
    account_generation: accountGeneration, consent_generation: consentGeneration, status, reason });
  async function lookup(entry, operationId) {
    const admitted = await workspace.admit(entry.port.sender);
    const records = await commands();
    const record = records.find((item) => item.command.operation_id === operationId && item.command.journey_id === admitted.record.journey_id);
    if (!record || !UUID.test(record.command_epoch ?? '')) {
      send(entry, { type: 'operation', operation_id: operationId, status: 'unavailable' }); return;
    }
    await publish();
    const current = await evidence();
    const helperMatches = record.command.action !== 'reopen_helper' || (record.command_epoch === epoch
      && current.status.observer?.helper === 'open' && consentController.observer.helper?.tab_id === record.helper_tab);
    const sameOwnerScope = record.command_epoch !== epoch || record.command.account_generation === accountGeneration;
    const confirmed = sameOwnerScope && record.result?.status === 'confirmed' && helperMatches && record.effect
      && JSON.stringify(record.effect) === JSON.stringify(current.scope);
    const response = result(record.command, record.command_epoch, confirmed ? 'confirmed' : 'unknown', confirmed ? 'none' : 'unconfirmed');
    // The original receipt stays immutable. Save current response ordering so
    // reconnect lookup cannot pretend an older worker revision is current.
    record.last_result = response;
    await chromeApi.storage.local.set({ [OPERATIONS_KEY]: records }); send(entry, response);
  }
  async function command(entry, value, commandEpoch) {
    const admitted = await workspace.admit(entry.port.sender);
    const state = await project();
    if (!state || value.journey_id !== admitted.record.journey_id) throw Error('command_scope');
    const reply = (status, reason) => result(value, commandEpoch, status, reason);
    if (commandEpoch !== epoch) { send(entry, reply('rejected', 'stale_scope')); return; }
    const records = await commands(); const existing = records.find((item) => item.command.operation_id === value.operation_id);
    if (existing) {
      if (existing.command_epoch !== commandEpoch || Object.keys(value).some((key) => existing.command[key] !== value[key])) {
        send(entry, reply('rejected', 'stale_scope')); return;
      }
      // Interrupted owner operations are reconciled, never blindly replayed.
      await lookup(entry, value.operation_id); return;
    }
    if (value.account_generation !== accountGeneration || value.consent_generation !== consentGeneration) {
      send(entry, reply('rejected', 'stale_scope')); return;
    }
    if (records.length >= 64) { send(entry, reply('rejected', 'not_ready')); return; }
    const record = { command: value, command_epoch: commandEpoch, result: null, effect: null, expires: Date.now() + 30 * 60 * 1000 };
    records.push(record); await chromeApi.storage.local.set({ [OPERATIONS_KEY]: records });
    pending = { operation_id: value.operation_id, status: 'pending' }; await publish();
    try {
      if (value.action === 'reopen_helper') await consentController.observer.reopen();
      else await consentController.setMode(value.action);
      pending = null; await publish();
      const { status: committed, scope } = await evidence();
      const observedEffect = value.action === 'reopen_helper' ? committed.observer?.helper === 'open'
        : value.action === 'pause' ? committed.consent.mode === 'paused' : ['preview', 'full'].includes(committed.consent.mode);
      const confirmed = value.account_generation === accountGeneration && observedEffect
        && JSON.stringify([scope.account_id, scope.expected_creator, scope.preview_account]) === accountScope
        && scope.consent_epoch === consentEpoch;
      record.effect = confirmed ? scope : null;
      record.helper_tab = confirmed && value.action === 'reopen_helper' ? consentController.observer.helper?.tab_id ?? null : null;
      record.result = confirmed ? reply('confirmed', 'none') : reply('unknown', 'unconfirmed');
    } catch {
      // A controller can commit consent before reconciliation fails. A thrown
      // call does not establish that the action had no effect.
      pending = null; await publish(); record.result = reply('unknown', 'unconfirmed');
    }
    await chromeApi.storage.local.set({ [OPERATIONS_KEY]: records }); send(entry, record.result);
  }
  function connect(port) {
    if (port.name !== ONBOARDING_PORT) return;
    const entry = { port, closed: false, initialized: false, lastRevision: null };
    // Subscribe first. Commits that happen during admission/snapshot are queued
    // behind that read, then published as the next revision.
    ports.add(entry);
    port.onDisconnect.addListener(() => { entry.closed = true; ports.delete(entry); });
    port.onMessage.addListener((value) => {
      void serial(async () => {
        if (!entry.initialized || entry.closed) return;
        const admitted = await workspace.admit(port.sender);
        if (admitted.route === 'hosted') throw Error('local_only');
        if (exact(value, ['type']) && value.type === 'snapshot') {
          const snapshot = await project(); send(entry, snapshot); entry.lastRevision = snapshot?.revision;
        } else if (exact(value, ['type', 'route']) && value.type === 'navigate') {
          await workspace.navigate(port.sender, { route: value.route });
        } else if (exact(value, ['type']) && value.type === 'focus') {
          try { await workspace.focus(port.sender); send(entry, { type: 'focus', focused: true }); }
          catch { send(entry, { type: 'focus', focused: false }); }
        } else if (exact(value, ['type', 'operation_id']) && value.type === 'operation' && UUID.test(value.operation_id)) {
          await lookup(entry, value.operation_id);
        } else if (exact(value, ['type', 'epoch', 'command']) && value.type === 'command' && UUID.test(value.epoch) && isCommand(value.command)) {
          await command(entry, value.command, value.epoch);
        } else throw Error('onboarding_message_invalid');
      }).catch(() => { entry.closed = true; ports.delete(entry); port.disconnect(); });
    });
    void serial(async () => {
      const admitted = await workspace.admit(port.sender);
      if (admitted.route === 'hosted') throw Error('local_only');
      send(entry, { type: 'capabilities', capabilities: ['local-onboarding.v1', 'persistent-workspace.v1', 'local-onboarding.command-result.v2'] });
      const snapshot = await project(); send(entry, snapshot); entry.lastRevision = snapshot?.revision; entry.initialized = true;
    }).catch(() => { entry.closed = true; ports.delete(entry); port.disconnect(); });
  }
  chromeApi.runtime.onConnect?.addListener(connect); chromeApi.runtime.onConnectExternal?.addListener(connect);
  consentController.subscribe(changed); companion?.subscribe(changed); identityBridge.onAccountChange?.(changed);
  chromeApi.storage.onChanged.addListener((_changes, area) => { if (area === 'local') changed(); });
  return Object.freeze({ changed });
}
