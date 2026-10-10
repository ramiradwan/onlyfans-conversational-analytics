import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import { build } from 'esbuild';
import { createFullReviewPersistence, fullReviewIntent, FULL_REVIEW_INTENT_KEY } from '../runtime/onboarding-full-intent.mjs';

const record = { journey_id: '11111111-1111-4111-8111-111111111111',
  draft_scope: { scope_id: '22222222-2222-4222-8222-222222222222', disclosure_bundle_id: 'a'.repeat(64) },
  draft: { terms_checked: true, risk_checked: true, full_checked: false } };
const model = { status: { consent: { mode: 'preview' }, phase: 'preview' },
  legal: { configured: true, flow: { terms_event_id: 'terms', risk_event_id: 'risk' } },
  pairing: { state: 'unpaired' }, config: {}, desktopLinked: false };

// Execute the production entry point, including its ordering of initialization,
// workspace read, storage notifications and rendering. Only platform/UI ports
// are replaced; Full-review state and intent code stay in the production bundle.
const stubs = {
  'document-observer.mjs': 'export const OBSERVER_REOPEN_TYPE="observer";',
  'onboarding-entry.mjs': 'export const WORKSPACE_MESSAGE_TYPE="workspace";',
  'onboarding-workspace.mjs': 'export const WORKSPACE_RECORD_KEY="workspace-record";',
  'legal-activation-controller.mjs': 'export const LEGAL_ACCEPT_TERMS_MESSAGE_TYPE="terms", LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE="risk", LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE="activate", LEGAL_CHOOSE_MODE_MESSAGE_TYPE="choose";',
  'surface-client.mjs': `export function createSurfaceClient(render) { return { model: fixture.model, async start() { render(this.model); } }; }
    export const send=fixture.send, openSurface=()=>{}, secureExternalUrl=()=>null;
    export class NoticeError extends Error {}`,
  'presentation.mjs': `export const customerJourney=()=>fixture.journey,
    needsAgreement=()=>false, modeChoiceAvailable=()=>false;`,
  'dom.mjs': `export const element=fixture.element, show=fixture.show, text=(id,value)=>{element(id).textContent=value};
    export const renderLoading=()=>{},renderJourney=()=>{},renderReadiness=()=>{},renderLegalLinks=()=>{};
    export const createPageActions=()=>({busy:false,lock(){},bind(id,fn){fixture.actions[id]=fn}});`,
  'actions.mjs': 'export const chooseMode=async()=>{},transition=()=>{},restoreAccess=()=>{},openCreatorAccount=()=>{};',
  'handoff.mjs': 'export const createHandoffFinisher=()=>()=>{},returnToDesktop=()=>{};',
  'setup-transfer.mjs': 'export const SETUP_TRANSFER_MESSAGE="transfer",bindSetupTransfer=()=>null,renderReceivingContext=()=>{};',
};
const bundle = await build({ entryPoints: [fileURLToPath(new URL('../setup.js', import.meta.url))],
  bundle: true, write: false, format: 'iife', plugins: [{ name: 'setup-platform-ports', setup(bundler) {
    bundler.onResolve({ filter: /\.mjs$/ }, ({ path }) => {
      const name = path.split('/').at(-1);
      return stubs[name] ? { path: name, namespace: 'fixture' } : undefined;
    });
    bundler.onLoad({ filter: /.*/, namespace: 'fixture' }, ({ path }) => ({ contents: stubs[path], loader: 'js' }));
  } }] });
const settle = () => new Promise((resolve) => setImmediate(resolve));

function storage() {
  const values = new Map();
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key) };
}
async function initialize(saved, admitted = record, intent = null, options = {}) {
  const nodes = new Map(), actions = {}, visibility = [], calls = [];
  const element = (id) => {
    if (!nodes.has(id)) nodes.set(id, { dataset: {}, classList: { toggle() {} },
      setAttribute() {}, removeAttribute() {}, addEventListener() {} });
    return nodes.get(id);
  };
  let admit;
  const admittedRead = new Promise((resolve) => { admit = resolve; });
  const listeners = [];
  const state = { intent };
  const fixture = { model: structuredClone(options.model ?? model), element, actions,
    journey: options.journey ?? { id: 'preview_available', title: 'Preview is ready' },
    show(id, visible) { element(id).hidden = !visible; visibility.push([id, visible]); },
    async send(message) {
      calls.push(message);
      if (message.action === 'desktop_handoff') return options.handoff ?? null;
      if (message.action === 'return_desktop_handoff') return { status: 'returned' };
      return message.type === 'workspace' ? admittedRead : null;
    } };
  runInNewContext(bundle.outputFiles[0].text, { fixture, sessionStorage: saved,
    location: { hash: `#journey=${admitted.journey_id}` },
    document: { querySelector: element, querySelectorAll: () => [] }, window: { addEventListener() {} },
    chrome: { storage: { onChanged: { addListener: (listener) => listeners.push(listener) },
      session: { get: async () => ({ [FULL_REVIEW_INTENT_KEY]: state.intent }) } } },
    setTimeout, clearTimeout, console });
  await settle();
  assert.equal(element('full-disclosure').hidden, true, 'nothing is restored before worker admission');
  admit(structuredClone(admitted));
  await settle(); await settle();
  return { fixture, calls, element, visibility, async review(value) {
    state.intent = value;
    for (const listener of listeners) listener({ [FULL_REVIEW_INTENT_KEY]: { newValue: value } }, 'session');
    await settle(); await settle();
  } };
}

for (const id of ['pairing_required', 'full_ready']) test(`the persistent extension step returns automatically at ${id} without granting consent`, async () => {
  const full = structuredClone(model); full.status.consent.mode = 'full';
  const run = await initialize(storage(), record, null, { model: full, journey: { id, title: 'Connect' },
    handoff: { expires_at: Date.now() + 60_000 } });
  assert.deepEqual(run.calls.map((call) => call.action), ['read', 'desktop_handoff', 'return_desktop_handoff', 'context']);
  assert.equal(run.element('journey-body').textContent, 'Returning to setup…');
});
test('persistent handoff preserves Preview without activating Full or returning early', async () => {
  const run = await initialize(storage(), record, null, { handoff: { expires_at: Date.now() + 60_000 } });
  assert.deepEqual(run.calls.map((call) => call.action), ['read', 'desktop_handoff', 'context']);
});

test('desktop handoff returns before authentication so the desktop can start pairing', async () => {
  const full = structuredClone(model); full.status.consent.mode = 'full';
  full.desktopRuntimeReachable = false;
  const run = await initialize(storage(), record, null, { model: full,
    journey: { id: 'desktop_app_needed', title: 'Connect' },
    handoff: { expires_at: Date.now() + 60_000 } });
  assert.equal(run.calls.filter((call) => call.action === 'return_desktop_handoff').length, 1);
});

for (const prerequisite of ['permission', 'account', 'agreement', 'paused']) {
  test(`desktop handoff waits for ${prerequisite}`, async () => {
    const full = structuredClone(model); full.status.consent.mode = 'full';
    if (prerequisite === 'permission') full.status.phase = 'permission_required';
    if (prerequisite === 'account') full.pairing.state = 'setup_incomplete';
    if (prerequisite === 'agreement') full.legal.requires_reauthorization = true;
    if (prerequisite === 'paused') full.status.consent.mode = 'paused';
    const run = await initialize(storage(), record, null, { model: full,
      journey: { id: 'desktop_app_needed', title: 'Connect' },
      handoff: { expires_at: Date.now() + 60_000 } });
    assert.equal(run.calls.filter((call) => call.action === 'return_desktop_handoff').length, 0);
  });
}

for (const mismatch of ['legacy', 'journey', 'creator', 'disclosure']) {
  test(`setup initialization rejects ${mismatch} Full-review persistence and intent`, async () => {
    const saved = storage();
    saved.setItem('full-review', 'true');
    const prior = structuredClone(record);
    if (mismatch === 'journey') prior.journey_id = crypto.randomUUID();
    if (mismatch === 'creator') prior.draft_scope.scope_id = crypto.randomUUID();
    if (mismatch === 'disclosure') prior.draft_scope.disclosure_bundle_id = 'b'.repeat(64);
    if (mismatch !== 'legacy') createFullReviewPersistence(saved).write(prior, true);
    const run = await initialize(saved, record, mismatch === 'legacy' ? null : fullReviewIntent(prior.journey_id, prior.draft_scope));
    assert.equal(run.element('full-disclosure').hidden, true);
    assert.equal(run.element('journey-card').hidden, false);
    assert.ok(!run.visibility.some(([id, visible]) => id === 'full-disclosure' && visible));
    assert.equal(saved.getItem('full-review'), null);
    assert.deepEqual(run.calls.map((call) => call.action), ['read', 'desktop_handoff', 'context'], 'initialization cannot grant consent');
  });
}
test('same-scope review survives reload, dismissal survives reload, and a new live request opens it once', async () => {
  const saved = storage();
  createFullReviewPersistence(saved).write(record, true);
  const first = await initialize(saved);
  assert.equal(first.element('full-disclosure').hidden, false);
  first.fixture.actions['full-secondary']();
  const second = await initialize(saved);
  assert.equal(second.element('full-disclosure').hidden, true);
  const intent = fullReviewIntent(record.journey_id, record.draft_scope);
  await second.review(intent);
  assert.equal(second.element('full-disclosure').hidden, false);
  second.fixture.actions['full-secondary']();
  await second.review(intent);
  assert.equal(second.element('full-disclosure').hidden, true);
  assert.equal(second.fixture.model.status.consent.mode, 'preview');
  assert.equal(second.element('terms-accepted').checked, true);
  assert.equal(second.element('risk-acknowledged').checked, true);
});
