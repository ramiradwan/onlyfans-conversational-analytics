import assert from 'node:assert/strict';
import test from 'node:test';
import { registerOnboardingPort, ONBOARDING_PORT } from '../runtime/onboarding-port.mjs';
const journey = '10000000-0000-4000-8000-000000000001';
const operation = '10000000-0000-4000-8000-000000000002';
const event = () => { const callbacks = []; return { addListener(fn) { callbacks.push(fn); }, emit(...args) { callbacks.forEach((fn) => fn(...args)); } }; };
const flush = async () => { for (let i = 0; i < 15; i++) await new Promise(setImmediate); };
async function fixture({ route = 'extension', initialRecords = [], unavailable = false, failAfterCommit = false,
  initialMode = 'preview', initialConsentEpoch = 'initial', previewAccount = null, expectedCreator = null, observedCreator = null } = {}) {
  const data = { onboarding_operations_v1: initialRecords, ofca_preview_metrics_v2: { active_account: previewAccount } };
  let mode = initialMode, consentEpoch = initialConsentEpoch, changes, transitions = 0, focused = 0;
  const chromeApi = { runtime: { onConnect: event(), onConnectExternal: event() }, storage: { onChanged: event(), local: {
    async get(keys) { return Object.fromEntries(keys.map((key) => [key, structuredClone(data[key])])); },
    async set(values) { Object.assign(data, structuredClone(values)); },
  } } };
  const controller = { subscribe(fn) { changes = fn; }, observer: { async reopen() {} }, async status() {
    return { consent: { mode, resume_mode: mode === 'paused' ? 'preview' : null, consent_epoch: consentEpoch }, phase: mode,
      onlyfans_permission: true, observer: { attachment: 'ready', helper: 'none' } };
  }, async setMode(action) { transitions++; if (unavailable) throw Error('refused'); mode = action === 'pause' ? 'paused' : 'preview'; consentEpoch = crypto.randomUUID(); changes(); if (failAfterCommit) throw Error('reconcile_failed'); } };
  const workspace = { async read() { return { journey_id: journey }; }, async admit() { return { route, record: { journey_id: journey } }; },
    async navigate() {}, async focus() { focused++; } };
  registerOnboardingPort({ chromeApi, workspace, consentController: controller,
    legalActivationController: { async status() { return { requires_reauthorization: false }; } },
    identityBridge: { async currentAccountId() { return observedCreator; }, onAccountChange() {} },
    expectedCreator: () => expectedCreator, companion: { subscribe() {} } });
  const messages = []; let closed = false;
  const port = { name: ONBOARDING_PORT, sender: {}, onMessage: event(), onDisconnect: event(),
    postMessage(value) { messages.push(structuredClone(value)); }, disconnect() { closed = true; this.onDisconnect.emit(); } };
  chromeApi.runtime.onConnect.emit(port); await flush();
  const command = (extra = {}) => { const state = messages.find((value) => value.kind === 'snapshot');
    return { profile: 'local-onboarding-command.v1', owner: 'extension', action: 'pause', journey_id: journey, operation_id: operation,
      account_generation: state.account_generation, consent_generation: state.consent_generation, ...extra }; };
  return { port, messages, data, command, transitions: () => transitions, closed: () => closed,
    focused: () => focused,
    account(value) { observedCreator = value; changes(); },
    sendCommand(value, epoch = messages.find((value) => value.kind === 'snapshot').epoch) { port.onMessage.emit({ type: 'command', epoch, command: value }); },
    retire() { workspace.admit = async () => { throw Error('stale_document'); }; changes(); } };
}
test('admitted local channel negotiates capabilities before a closed extension snapshot', async () => {
  const f = await fixture();
  assert.deepEqual(f.messages[0], { type: 'capabilities', capabilities: ['local-onboarding.v1', 'persistent-workspace.v1', 'local-onboarding.command-result.v2'] });
  assert.equal(f.messages[1].source, 'extension'); assert.equal(f.messages[1].kind, 'snapshot');
  assert.equal(f.messages[1].facts.account, 'unknown'); assert.equal(f.messages[1].reason, 'none');
  assert.equal(JSON.stringify(f.messages).includes('creator_id'), false);
});
test('hosted documents never receive local facts or commands', async () => {
  const f = await fixture({ route: 'hosted' }); assert.equal(f.closed(), true); assert.deepEqual(f.messages, []);
});

test('Full capture stops being reported active as soon as account evidence changes', async () => {
  const f = await fixture({ initialMode: 'full', expectedCreator: 'creator-a', observedCreator: 'creator-a' });
  const first = f.messages.at(-1);
  assert.equal(first.facts.account, 'matching'); assert.equal(first.facts.capture, 'active');
  f.account('creator-b'); await flush();
  const mismatch = f.messages.at(-1);
  assert.equal(mismatch.facts.account, 'mismatch'); assert.equal(mismatch.facts.capture, 'off');
  assert.equal(mismatch.reason, 'wrong_account'); assert.ok(mismatch.account_generation > first.account_generation);
  f.account(null); await flush();
  assert.equal(f.messages.at(-1).facts.account, 'unknown'); assert.equal(f.messages.at(-1).facts.capture, 'off');
  f.account('creator-a'); await flush();
  assert.equal(f.messages.at(-1).facts.account, 'matching'); assert.equal(f.messages.at(-1).facts.capture, 'active');
});
test('pause result names both requested and committed consent generations and duplicate cannot repeat', async () => {
  const f = await fixture(); const command = f.command(); f.sendCommand(command); await flush();
  const result = f.messages.find((value) => value.profile === 'local-onboarding-result.v2');
  assert.equal(result.status, 'confirmed'); assert.equal(result.command_consent_generation, command.consent_generation);
  assert.equal(result.consent_generation, command.consent_generation + 1);
  assert.equal(f.messages.at(-2).facts.capture, 'paused');
  f.sendCommand(command); await flush(); assert.equal(f.transitions(), 1);
});
test('stale scope and unauthorized transitions do not claim completion', async () => {
  const f = await fixture({ unavailable: true });
  f.sendCommand(f.command({ account_generation: 8 })); await flush();
  assert.equal(f.messages.at(-1).reason, 'stale_scope'); assert.equal(f.transitions(), 0);
  f.sendCommand(f.command()); await flush(); assert.equal(f.messages.at(-1).status, 'unknown');
});
test('owner restart with an interrupted operation returns unknown without replay', async () => {
  const command = { profile: 'local-onboarding-command.v1', owner: 'extension', action: 'pause', journey_id: journey,
    operation_id: operation, account_generation: 0, consent_generation: 1 };
  const f = await fixture({ initialRecords: [{ command, command_epoch: crypto.randomUUID(), result: null, expires: Date.now() + 100000 }] });
  f.port.onMessage.emit({ type: 'operation', operation_id: operation }); await flush();
  assert.equal(f.messages.at(-1).status, 'unknown'); assert.equal(f.transitions(), 0);
});
test('a full live operation ledger refuses new commands without forgetting prior IDs', async () => {
  const records = Array.from({ length: 64 }, () => ({ command: { operation_id: crypto.randomUUID() }, result: null, expires: Date.now() + 100000 }));
  const f = await fixture({ initialRecords: records }); f.sendCommand(f.command()); await flush();
  assert.equal(f.messages.at(-1).status, 'rejected'); assert.equal(f.transitions(), 0);
  assert.equal(f.data.onboarding_operations_v1.length, 64);
});

test('a post-commit reconciliation failure publishes the commit but leaves the outcome unknown', async () => {
  const f = await fixture({ failAfterCommit: true }); const command = f.command();
  f.sendCommand(command); await flush();
  assert.equal(f.messages.at(-1).status, 'unknown'); assert.equal(f.messages.at(-1).reason, 'unconfirmed');
  assert.equal(f.messages.at(-2).facts.capture, 'paused');
  f.sendCommand(command); await flush(); assert.equal(f.transitions(), 1);
});

test('a retired workspace document cannot receive a later committed event', async () => {
  const f = await fixture(); const count = f.messages.length;
  f.retire(); f.port.onMessage.emit({ type: 'snapshot' }); await flush();
  assert.equal(f.closed(), true); assert.equal(f.messages.length, count);
});

test('raw commands and stale owner-epoch wrappers cannot mutate the current owner', async () => {
  const f = await fixture(); f.sendCommand(f.command(), crypto.randomUUID()); await flush();
  assert.equal(f.messages.at(-1).reason, 'stale_scope'); assert.equal(f.transitions(), 0);
  f.port.onMessage.emit(f.command()); await flush(); assert.equal(f.closed(), true); assert.equal(f.transitions(), 0);
});

test('read-only lookup after restart confirms only matching durable consent and account evidence', async () => {
  const previous = await fixture({ previewAccount: 'a'.repeat(64) }); previous.sendCommand(previous.command()); await flush();
  const receipt = previous.data.onboarding_operations_v1[0]; assert.equal(receipt.result.status, 'confirmed');
  for (const [account, expected] of [['a'.repeat(64), 'confirmed'], ['b'.repeat(64), 'unknown']]) {
    const f = await fixture({ initialRecords: [receipt], initialMode: 'paused', initialConsentEpoch: receipt.effect.consent_epoch, previewAccount: account });
    f.port.onMessage.emit({ type: 'operation', operation_id: operation }); await flush();
    const result = f.messages.at(-1); assert.equal(result.status, expected); assert.equal(f.transitions(), 0);
    assert.equal(result.command_epoch, receipt.command_epoch); assert.notEqual(result.epoch, receipt.command_epoch);
    assert.equal(result.command_account_generation, receipt.command.account_generation);
    assert.equal(result.consent_generation, f.messages.find((value) => value.kind === 'snapshot').consent_generation);
    assert.equal(f.data.onboarding_operations_v1[0].last_result.epoch, result.epoch);
  }
});

test('missing lookup is explicit unavailable and never invents the original command scope', async () => {
  const f = await fixture(); f.port.onMessage.emit({ type: 'operation', operation_id: operation }); await flush();
  assert.deepEqual(f.messages.at(-1), { type: 'operation', operation_id: operation, status: 'unavailable' });
  assert.equal(f.transitions(), 0);
});

test('admitted focus request focuses the workspace without issuing a command', async () => {
  const f = await fixture(); f.port.onMessage.emit({ type: 'focus' }); await flush();
  assert.equal(f.focused(), 1); assert.equal(f.transitions(), 0);
  assert.deepEqual(f.messages.at(-1), { type: 'focus', focused: true });
});
